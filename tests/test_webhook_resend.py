import base64
import json

from tests.helpers import make_settings, svix_headers

SECRET = make_settings().resend_webhook_secret
WRONG_SECRET = "whsec_" + base64.b64encode(b"1" * 32).decode()


async def seed_send(pool, email="u@x.co", resend_id="em_1"):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('July Launch', 's', 'welcome', 'v1') returning id")
    return await pool.fetchval(
        "insert into sends (campaign_id, ghl_contact_id, email, status, resend_email_id, sent_at) "
        "values ($1, 'c1', $2, 'sent', $3, now()) returning id", cid, email, resend_id)


def event_payload(event_type: str, email_id: str = "em_1", extra: dict | None = None) -> str:
    data = {"email_id": email_id, "to": ["u@x.co"], **(extra or {})}
    return json.dumps({"type": event_type, "created_at": "2026-07-07T00:00:00Z", "data": data})


async def post_event(client, payload: str, headers=None):
    return await client.post("/webhooks/resend", content=payload,
                             headers=headers or svix_headers(SECRET, payload))


async def test_rejects_bad_signature(client, pool):
    payload = event_payload("email.delivered")
    resp = await post_event(client, payload, headers=svix_headers(WRONG_SECRET, payload))
    assert resp.status_code == 401
    assert (await pool.fetchval("select count(*) from events")) == 0


async def test_delivered_persists_event_and_enqueues_tag_job(client, pool):
    send_id = await seed_send(pool)
    resp = await post_event(client, event_payload("email.delivered"))
    assert resp.status_code == 200
    event = await pool.fetchrow("select send_id, type from events")
    assert event["send_id"] == send_id and event["type"] == "email.delivered"
    job = await pool.fetchrow("select data from jobs where name='ghl_writeback'")
    data = json.loads(job["data"])
    assert data == {"kind": "add_tags", "contact_id": "c1", "tags": ["emailed-july-launch"]}


async def test_opened_and_clicked_tag_prefixes(client, pool):
    await seed_send(pool)
    await post_event(client, event_payload("email.opened"))
    await post_event(client, event_payload("email.clicked"))
    rows = await pool.fetch("select data from jobs order by created_at")
    tags = [json.loads(r["data"])["tags"][0] for r in rows]
    assert tags == ["opened-july-launch", "clicked-july-launch"]


async def test_hard_bounce_suppresses_and_dnds(client, pool):
    await seed_send(pool)
    payload = event_payload("email.bounced", extra={"bounce": {"type": "Permanent"}})
    await post_event(client, payload)
    row = await pool.fetchrow("select reason, source, ghl_contact_id from suppressions")
    assert row["reason"] == "hard_bounce" and row["source"] == "resend"
    assert row["ghl_contact_id"] == "c1"
    kinds = {json.loads(r["data"])["kind"] for r in await pool.fetch("select data from jobs")}
    assert kinds == {"set_dnd"}


async def test_soft_bounce_records_event_only(client, pool):
    await seed_send(pool)
    await post_event(client, event_payload("email.bounced", extra={"bounce": {"type": "Transient"}}))
    assert (await pool.fetchval("select count(*) from suppressions")) == 0
    assert (await pool.fetchval("select count(*) from events")) == 1


async def test_complaint_suppresses_dnds_and_tags(client, pool):
    await seed_send(pool)
    await post_event(client, event_payload("email.complained"))
    assert (await pool.fetchval("select reason from suppressions")) == "complaint"
    jobs = [json.loads(r["data"]) for r in await pool.fetch("select data from jobs order by created_at")]
    kinds = {j["kind"] for j in jobs}
    assert kinds == {"set_dnd", "add_tags"}
    tag_job = next(j for j in jobs if j["kind"] == "add_tags")
    assert tag_job["tags"] == ["complained"]


async def test_unknown_email_id_stores_orphan_event(client, pool):
    resp = await post_event(client, event_payload("email.delivered", email_id="em_unknown"))
    assert resp.status_code == 200
    assert (await pool.fetchval("select send_id from events")) is None
