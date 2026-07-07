import json

from app.services.suppressions import add_suppression
from app.services.unsub_tokens import make_token
from tests.helpers import make_settings

AUTH = {"x-webhook-secret": "hook-secret"}


async def seed_campaign(pool, status="ready"):
    return str(await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('drip', 's', 'welcome', 'v1', $1) returning id", status))


async def test_enroll_requires_secret(client, pool):
    cid = await seed_campaign(pool)
    resp = await client.post("/webhooks/ghl/enroll", json={
        "campaign_id": cid, "contact_id": "c1", "email": "a@b.co"})
    assert resp.status_code == 403


async def test_enroll_queues_send_and_activates_campaign(client, pool):
    cid = await seed_campaign(pool)
    resp = await client.post("/webhooks/ghl/enroll", headers=AUTH, json={
        "campaign_id": cid, "contact_id": "c1", "email": "Ada@B.co",
        "first_name": "Ada", "custom": {"plan": "gold"}})
    assert resp.status_code == 200 and resp.json() == {"enrolled": True}
    send = await pool.fetchrow("select email, status from sends")
    assert send["email"] == "ada@b.co" and send["status"] == "queued"
    assert (await pool.fetchval("select status from campaigns")) == "dispatching"
    cached = await pool.fetchrow("select first_name, custom from contacts_cache")
    assert cached["first_name"] == "Ada" and json.loads(cached["custom"]) == {"plan": "gold"}


async def test_enroll_rejects_suppressed(client, pool):
    cid = await seed_campaign(pool)
    await add_suppression(pool, "a@b.co", reason="complaint", source="resend")
    resp = await client.post("/webhooks/ghl/enroll", headers=AUTH, json={
        "campaign_id": cid, "contact_id": "c1", "email": "a@b.co"})
    assert resp.status_code == 200
    assert resp.json() == {"enrolled": False, "reason": "suppressed"}
    assert (await pool.fetchval("select count(*) from sends")) == 0


async def test_dnd_webhook_suppresses(client, pool):
    resp = await client.post("/webhooks/ghl/dnd", headers=AUTH,
                             json={"email": "Gone@x.co", "contact_id": "c9"})
    assert resp.status_code == 200
    row = await pool.fetchrow("select email, reason, source from suppressions")
    assert row["email"] == "gone@x.co" and row["reason"] == "ghl_dnd" and row["source"] == "ghl"


async def test_unsub_get_and_post_suppress_and_queue_dnd(client, pool):
    cid = await seed_campaign(pool)
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email) values ('c1', 'u@x.co')")
    token = make_token("u@x.co", cid, make_settings().unsub_signing_secret)
    resp = await client.post(f"/u/{token}")  # RFC 8058 one-click
    assert resp.status_code == 200
    assert (await pool.fetchval("select reason from suppressions")) == "unsubscribe"
    job = json.loads(await pool.fetchval("select data from jobs where name='ghl_writeback'"))
    assert job == {"kind": "set_dnd", "contact_id": "c1"}
    # GET (human click) is idempotent and renders confirmation HTML
    resp = await client.get(f"/u/{token}")
    assert resp.status_code == 200 and "unsubscribed" in resp.text.lower()
    assert (await pool.fetchval("select count(*) from suppressions")) == 1


async def test_unsub_bad_token_404(client):
    assert (await client.get("/u/garbage")).status_code == 404
