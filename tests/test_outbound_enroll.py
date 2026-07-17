import json

import httpx
import respx

from app.services.dispatch import process_send_queue
from app.services.resend_client import ResendClient
from app.services.suppressions import add_suppression
from tests.helpers import make_settings

RESEND_API = "https://api.resend.com/emails"


async def seed_outbound_campaign(pool):
    return await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('xovera-outbound: launch', 'fallback', 'outbound', 'v1') returning id")


def enroll_body(cid, **overrides):
    body = {
        "campaign_id": str(cid),
        "contact_id": "ghl_abc",
        "email": "Front.Desk@DesertGlow.com",
        "subject": "Missed calls at Desert Glow?",
        "text_body": "Hi there,\n\nNoticed a few reviewers mention phone trouble.\n\nRyan, Xovera",
        "first_name": "Desert Glow",
    }
    body.update(overrides)
    return body


async def test_enroll_inserts_send_with_overrides(pool, client):
    cid = await seed_outbound_campaign(pool)
    resp = await client.post("/outbound/enroll", json=enroll_body(cid))
    assert resp.status_code == 200 and resp.json()["enrolled"] is True
    row = await pool.fetchrow("select * from sends")
    assert row["email"] == "front.desk@desertglow.com"          # normalized
    assert row["subject_override"] == "Missed calls at Desert Glow?"
    assert json.loads(row["content_override"])["text_body"].startswith("Hi there")
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "dispatching"


async def test_enroll_rejects_suppressed_and_duplicates(pool, client):
    cid = await seed_outbound_campaign(pool)
    await add_suppression(pool, "front.desk@desertglow.com", reason="unsubscribe", source="unsub")
    resp = await client.post("/outbound/enroll", json=enroll_body(cid))
    assert resp.json() == {"enrolled": False, "reason": "suppressed"}
    assert await pool.fetchval("select count(*) from sends") == 0

    # different address enrolls once, second attempt is a no-op
    ok = enroll_body(cid, email="owner@desertglow.com")
    assert (await client.post("/outbound/enroll", json=ok)).json()["enrolled"] is True
    assert (await client.post("/outbound/enroll", json=ok)).json() == {
        "enrolled": False, "reason": "already enrolled"}


async def test_suppression_check_endpoint(pool, client):
    await add_suppression(pool, "gone@x.co", reason="hard_bounce", source="resend")
    resp = await client.post("/suppressions/check",
                             json={"emails": ["Gone@X.co", "fine@x.co"]})
    assert resp.json() == {"suppressed": ["gone@x.co"]}


@respx.mock
async def test_dispatch_sends_override_with_personal_subject(pool, client):
    route = respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_9"}))
    cid = await seed_outbound_campaign(pool)
    await client.post("/outbound/enroll", json=enroll_body(cid))
    settings = make_settings()
    sent = await process_send_queue(pool, settings, ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 1
    body = route.calls[0].request.read().decode()
    payload = json.loads(body)
    assert payload["subject"] == "Missed calls at Desert Glow?"   # per-send, not campaign
    assert "Noticed a few reviewers" in payload["text"]
    assert "/u/" in payload["html"] and "Unsubscribe" in payload["html"]
    assert "List-Unsubscribe" in json.dumps(payload.get("headers", {}))
    row = await pool.fetchrow("select status, resend_email_id from sends")
    assert row["status"] == "sent" and row["resend_email_id"] == "em_9"


@respx.mock
async def test_dispatch_time_suppression_still_applies_to_overrides(pool, client):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_9"}))
    cid = await seed_outbound_campaign(pool)
    await client.post("/outbound/enroll", json=enroll_body(cid))
    # address gets suppressed between enroll and dispatch
    await add_suppression(pool, "front.desk@desertglow.com", reason="ghl_dnd", source="ghl")
    settings = make_settings()
    sent = await process_send_queue(pool, settings, ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 0
    assert (await pool.fetchval("select status from sends")) == "suppressed"


async def test_enroll_revives_completed_campaign(pool, client):
    cid = await seed_outbound_campaign(pool)
    await pool.execute("update campaigns set status='completed' where id=$1", cid)
    resp = await client.post("/outbound/enroll", json=enroll_body(cid))
    assert resp.json()["enrolled"] is True
    assert (await pool.fetchval(
        "select status from campaigns where id=$1", cid)) == "dispatching"
