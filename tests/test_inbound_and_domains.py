import json

import httpx
import respx

from app.services.dispatch import process_send_queue
from app.services.domains import adjust_and_guard, pick_from
from app.services.inbound import handle_received, process_reply_jobs
from app.services.resend_client import ResendClient
from tests.helpers import make_settings, verify_all_contacts
from tests.test_outbound_enroll import enroll_body, seed_outbound_campaign

RESEND_API = "https://api.resend.com/emails"


async def test_received_webhook_stores_reply_and_links_send(pool, client):
    cid = await seed_outbound_campaign(pool)
    await client.post("/outbound/enroll", json=enroll_body(cid))
    await handle_received(pool, {
        "email_id": "recv_1", "from": "Front.Desk@DesertGlow.com",
        "to": ["ryan@mail.tryxovera.com"], "subject": "Re: Missed calls"})
    reply = await pool.fetchrow("select * from replies")
    assert reply["from_email"] == "front.desk@desertglow.com"
    assert reply["campaign_id"] is not None          # linked to the send
    assert await pool.fetchval(
        "select count(*) from jobs where name='classify_reply'") == 1
    # duplicate webhook delivery is a no-op
    await handle_received(pool, {"email_id": "recv_1", "from": "x@y.z"})
    assert await pool.fetchval("select count(*) from replies") == 1


@respx.mock
async def test_reply_classification_unsubscribe_suppresses(pool, client):
    cid = await seed_outbound_campaign(pool)
    await client.post("/outbound/enroll", json=enroll_body(cid))
    await handle_received(pool, {
        "email_id": "recv_2", "from": "front.desk@desertglow.com",
        "to": ["ryan@mail.tryxovera.com"], "subject": "stop"})
    respx.get("https://api.resend.com/emails/receiving/recv_2").mock(
        return_value=httpx.Response(200, json={"text": "Please remove me from your list."}))
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "id": "m1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text":
                '{"classification":"unsubscribe","summary":"asked to be removed"}'}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    settings = make_settings(anthropic_api_key="sk-ant-test")
    handled = await process_reply_jobs(pool, settings)
    assert handled == 1
    assert (await pool.fetchval("select classification from replies")) == "unsubscribe"
    assert await pool.fetchval(
        "select count(*) from suppressions where email='front.desk@desertglow.com'") == 1


async def test_activity_endpoint_returns_engagement_and_reply(pool, client):
    cid = await seed_outbound_campaign(pool)
    await client.post("/outbound/enroll", json=enroll_body(cid))
    send_id = await pool.fetchval("select id from sends")
    await pool.execute("update sends set status='sent', sent_at=now() where id=$1", send_id)
    await pool.execute(
        "insert into events (send_id, type, payload) values ($1,'email.opened','{}')", send_id)
    await pool.execute(
        """insert into replies (resend_email_id, from_email, subject, classification,
                                summary, processed)
           values ('recv_3','front.desk@desertglow.com','Re:', 'interested','wants a call', true)""")
    resp = await client.get("/outbound/activity")
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["opened_at"] is not None
    assert rows[0]["reply_classification"] == "interested"


@respx.mock
async def test_domain_pool_rotation_and_attribution(pool, client):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    await client.post("/outbound/domains", json={"domain": "mail.tryxovera.com"})
    await client.post("/outbound/domains", json={"domain": "mail.xoverahq.com"})
    cid = await seed_outbound_campaign(pool)
    await client.post("/outbound/enroll", json=enroll_body(cid))
    await verify_all_contacts(pool)   # dispatch gate needs a 'valid' verdict
    settings = make_settings()
    sent = await process_send_queue(pool, settings, ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 1
    row = await pool.fetchrow("select from_domain from sends")
    assert row["from_domain"] in ("mail.tryxovera.com", "mail.xoverahq.com")
    listing = (await client.get("/outbound/domains")).json()["data"]
    assert sum(d["sent_today"] for d in listing) == 1


async def test_empty_domain_pool_falls_back_to_settings(pool, client):
    settings = make_settings()
    from_email, from_domain = await pick_from(pool, settings)
    assert from_email == settings.from_email and from_domain is None


async def test_domain_guard_pauses_on_bounces(pool, client):
    await client.post("/outbound/domains", json={"domain": "mail.bad.com"})
    cid = await seed_outbound_campaign(pool)
    for i in range(12):
        sid = await pool.fetchval(
            """insert into sends (campaign_id, ghl_contact_id, email, status,
                                  from_domain, sent_at)
               values ($1, $2, $3, 'sent', 'mail.bad.com', now()) returning id""",
            cid, f"c{i}", f"u{i}@x.co")
        if i < 6:
            await pool.execute(
                "insert into events (send_id, type, payload) values ($1,'email.bounced','{}')", sid)
    await adjust_and_guard(pool, make_settings(alert_webhook_url=None))
    row = await pool.fetchrow("select active, paused_reason from sending_domains")
    assert row["active"] is False and "bounce" in row["paused_reason"]
