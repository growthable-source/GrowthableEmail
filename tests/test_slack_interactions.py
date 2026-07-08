import json
from urllib.parse import urlencode

import httpx
import respx

from tests.test_slack_client import sign

SLACK_API = "https://slack.com/api"


async def seed_ready_campaign(pool, n_contacts=2, channel=None, thread_ts=None):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status, "
        "seed_tested_at, channel, thread_ts) values "
        "('July', 'Big', 'newsletter', 'v1', 'ready', now(), $1, $2) returning id",
        channel, thread_ts)
    for i in range(n_contacts):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email) values ($1, $2)",
            f"c{i}", f"u{i}@x.co")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
            cid, f"c{i}")
    return cid


async def post_interaction(client, action_id, value: dict):
    payload = {
        "type": "block_actions",
        "user": {"id": "URYAN"},
        "channel": {"id": "C0TEST"},
        "container": {"message_ts": "555.001"},
        "actions": [{"action_id": action_id, "value": json.dumps(value)}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts, sig = sign(body)
    return await client.post("/slack/interactions", content=body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        "content-type": "application/x-www-form-urlencoded"})


@respx.mock
async def test_approve_now_fills_queue_and_updates_message(client, pool):
    update = respx.post(f"{SLACK_API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    cid = await seed_ready_campaign(pool)
    resp = await post_interaction(client, "approve_send", {"campaign_id": str(cid), "when": None})
    assert resp.status_code == 200
    assert (await pool.fetchval("select count(*) from sends")) == 2
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "dispatching"
    assert b"URYAN" in update.calls[0].request.read()


@respx.mock
async def test_approve_future_schedules(client, pool):
    respx.post(f"{SLACK_API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    cid = await seed_ready_campaign(pool)
    resp = await post_interaction(client, "approve_send",
                                  {"campaign_id": str(cid), "when": "2030-01-01T09:00:00+10:00"})
    assert resp.status_code == 200
    row = await pool.fetchrow("select status, scheduled_at from campaigns where id=$1", cid)
    assert row["status"] == "scheduled" and row["scheduled_at"] is not None
    assert (await pool.fetchval("select count(*) from sends")) == 2  # queue pre-filled


@respx.mock
async def test_approve_now_notifies_channel_immediately(client, pool):
    respx.post(f"{SLACK_API}/chat.update").mock(return_value=httpx.Response(200, json={"ok": True}))
    notify_route = respx.post(f"{SLACK_API}/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "1"}))
    cid = await seed_ready_campaign(pool, channel="C0TEST", thread_ts="100.1")
    await post_interaction(client, "approve_send", {"campaign_id": str(cid), "when": None})
    assert notify_route.called
    body = json.loads(notify_route.calls[0].request.read())
    assert body["channel"] == "C0TEST" and body["thread_ts"] == "100.1"
    assert "<!channel>" in body["text"] and "July" in body["text"]


@respx.mock
async def test_approve_scheduled_does_not_notify_immediately(client, pool):
    respx.post(f"{SLACK_API}/chat.update").mock(return_value=httpx.Response(200, json={"ok": True}))
    cid = await seed_ready_campaign(pool, channel="C0TEST", thread_ts="100.1")
    # no chat.postMessage route registered — a call to it would raise, proving it wasn't hit
    await post_interaction(client, "approve_send",
                           {"campaign_id": str(cid), "when": "2030-01-01T09:00:00+10:00"})


@respx.mock
async def test_cancel_and_stale_click(client, pool):
    update = respx.post(f"{SLACK_API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    cid = await seed_ready_campaign(pool)
    await post_interaction(client, "cancel_send", {"campaign_id": str(cid), "when": None})
    assert b"ancelled" in update.calls[0].request.read()
    assert (await pool.fetchval("select count(*) from sends")) == 0
    # campaign already dispatched → stale click
    await pool.execute("update campaigns set status='completed' where id=$1", cid)
    await post_interaction(client, "approve_send", {"campaign_id": str(cid), "when": None})
    assert b"already" in update.calls[1].request.read().lower()
    assert (await pool.fetchval("select count(*) from sends")) == 0


async def test_bad_signature_401(client):
    body = b"payload=%7B%7D"
    resp = await client.post("/slack/interactions", content=body, headers={
        "x-slack-request-timestamp": "1", "x-slack-signature": "v0=bad"})
    assert resp.status_code == 401


GHL_POSTS_API = "https://services.leadconnectorhq.com/social-media-posting/loc_test/posts"


async def seed_draft_post(pool, channel=None):
    return await pool.fetchval(
        "insert into social_posts (thread_ts, channel, account_ids, content) "
        "values ('500.1', $1, array['acc1'], $2) returning id",
        channel, json.dumps({"text": "Big news.", "media": ["https://x/i.png"]}))


@respx.mock
async def test_approve_post_publishes_via_ghl(client, pool):
    update = respx.post(f"{SLACK_API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    ghl_route = respx.post(GHL_POSTS_API).mock(
        return_value=httpx.Response(200, json={"results": {"post": {"_id": "ghl_p1"}}}))
    post_id = await seed_draft_post(pool)
    resp = await post_interaction(client, "approve_post", {"post_id": str(post_id), "when": None})
    assert resp.status_code == 200
    body = json.loads(ghl_route.calls[0].request.read())
    assert body["status"] == "published" and body["summary"] == "Big news."
    row = await pool.fetchrow("select status, ghl_post_id from social_posts")
    assert row["status"] == "published" and row["ghl_post_id"] == "ghl_p1"
    assert b"ghl_p1" in update.calls[0].request.read()


@respx.mock
async def test_approve_post_future_schedules(client, pool):
    respx.post(f"{SLACK_API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    ghl_route = respx.post(GHL_POSTS_API).mock(
        return_value=httpx.Response(200, json={"results": {"post": {"_id": "ghl_p2"}}}))
    post_id = await seed_draft_post(pool)
    await post_interaction(client, "approve_post",
                           {"post_id": str(post_id), "when": "2030-01-01T09:00:00+10:00"})
    body = json.loads(ghl_route.calls[0].request.read())
    assert body["status"] == "scheduled" and "scheduleDate" in body
    assert (await pool.fetchval("select status from social_posts")) == "scheduled"


@respx.mock
async def test_approve_post_now_notifies_channel_immediately(client, pool):
    respx.post(f"{SLACK_API}/chat.update").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.post(GHL_POSTS_API).mock(
        return_value=httpx.Response(200, json={"results": {"post": {"_id": "ghl_p3"}}}))
    notify_route = respx.post(f"{SLACK_API}/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "1"}))
    post_id = await seed_draft_post(pool, channel="C0SOCIAL")
    await post_interaction(client, "approve_post", {"post_id": str(post_id), "when": None})
    assert notify_route.called
    body = json.loads(notify_route.calls[0].request.read())
    assert body["channel"] == "C0SOCIAL" and "<!channel>" in body["text"]
    assert "Big news." in body["text"]


@respx.mock
async def test_approve_post_scheduled_does_not_notify_immediately(client, pool):
    respx.post(f"{SLACK_API}/chat.update").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.post(GHL_POSTS_API).mock(
        return_value=httpx.Response(200, json={"results": {"post": {"_id": "ghl_p4"}}}))
    post_id = await seed_draft_post(pool, channel="C0SOCIAL")
    # no chat.postMessage route registered — a call to it would raise, proving it wasn't hit
    await post_interaction(client, "approve_post",
                           {"post_id": str(post_id), "when": "2030-01-01T09:00:00+10:00"})


@respx.mock
async def test_cancel_post_and_stale_click(client, pool):
    update = respx.post(f"{SLACK_API}/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    post_id = await seed_draft_post(pool)
    await post_interaction(client, "cancel_post", {"post_id": str(post_id), "when": None})
    assert (await pool.fetchval("select status from social_posts")) == "cancelled"
    await post_interaction(client, "approve_post", {"post_id": str(post_id), "when": None})
    assert b"already" in update.calls[1].request.read().lower()
