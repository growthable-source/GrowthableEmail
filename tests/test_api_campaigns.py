import httpx
import respx

RESEND_API = "https://api.resend.com/emails"


async def create_campaign(client) -> str:
    resp = await client.post("/campaigns", json={
        "name": "July Launch",
        "subject": "The July launch is here",
        "template_ref": "welcome",
        "template_version": "v1",
        "audience_filter": [{"field": "tags", "operator": "eq", "value": "newsletter"}],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_create_campaign(client, pool):
    campaign_id = await create_campaign(client)
    row = await pool.fetchrow("select name, status, subject from campaigns")
    assert str(await pool.fetchval("select id from campaigns")) == campaign_id
    assert row["status"] == "draft" and row["subject"] == "The July launch is here"


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_campaign_routes_require_api_key(client):
    for headers in ({}, {"x-api-key": "wrong"}):
        resp = await client.post("/campaigns", json={}, headers={"x-api-key": "", **headers})
        assert resp.status_code == 401, headers


@respx.mock
async def test_test_send_goes_to_seed_list_only(client, pool):
    route = respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_t"}))
    campaign_id = await create_campaign(client)
    resp = await client.post(f"/campaigns/{campaign_id}/test")
    assert resp.status_code == 200
    assert resp.json() == {"sent_to": ["seed@growthable.io"]}
    body = route.calls[0].request.read().decode()
    assert "[TEST]" in body and "seed@growthable.io" in body
    assert (await pool.fetchval("select count(*) from sends")) == 0  # no real sends recorded
    assert (await pool.fetchval("select seed_tested_at from campaigns")) is not None


@respx.mock
async def test_dispatch_and_report_flow(client, pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    campaign_id = await create_campaign(client)
    # audience present (as if sync-audience already ran)
    for i in range(2):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email) values ($1, $2)",
            f"c{i}", f"u{i}@x.co")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) "
            "values ($1::uuid, $2)", campaign_id, f"c{i}")
    resp = await client.post(f"/campaigns/{campaign_id}/dispatch")
    assert resp.status_code == 200 and resp.json() == {"queued": 2}
    # simulate a worker pass + one delivered event
    from app.services.dispatch import process_send_queue
    from app.services.resend_client import ResendClient
    from tests.helpers import make_settings
    await process_send_queue(pool, make_settings(), ResendClient("re", rps=10_000, backoff_base=0))
    send_id = await pool.fetchval("select id from sends limit 1")
    await pool.execute(
        "insert into events (send_id, type) values ($1, 'email.delivered')", send_id)
    resp = await client.get(f"/campaigns/{campaign_id}/report")
    report = resp.json()
    assert report["sends"]["sent"] == 2
    assert report["events"]["email.delivered"] == 1


async def test_sync_audience_endpoint_calls_ghl(client, pool, monkeypatch):
    calls = {}

    async def fake_sync(pool_, ghl, campaign_id):
        calls["campaign_id"] = str(campaign_id)
        return {"kept": 5, "dropped": 1}

    import app.routers.campaigns as campaigns_router
    monkeypatch.setattr(campaigns_router, "sync_audience", fake_sync)
    campaign_id = await create_campaign(client)
    resp = await client.post(f"/campaigns/{campaign_id}/sync-audience")
    assert resp.status_code == 200 and resp.json() == {"kept": 5, "dropped": 1}
    assert calls["campaign_id"] == campaign_id
