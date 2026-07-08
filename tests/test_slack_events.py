import json

from tests.helpers import make_settings
from tests.test_slack_client import sign

SECRET = make_settings().slack_signing_secret


async def post_event(client, payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    ts, sig = sign(body, secret=secret)
    return await client.post("/slack/events", content=body, headers={
        "x-slack-request-timestamp": ts, "x-slack-signature": sig,
        "content-type": "application/json"})


def mention_event(event_id="Ev1", channel="C0TEST", text="<@UBOT> hello", ts="100.1",
                  thread_ts=None, etype="app_mention", **extra):
    event = {"type": etype, "channel": channel, "user": "URYAN", "text": text, "ts": ts, **extra}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event_id": event_id, "event": event}


async def test_url_verification_handshake(client):
    resp = await post_event(client, {"type": "url_verification", "challenge": "abc123"})
    assert resp.status_code == 200 and resp.json() == {"challenge": "abc123"}


async def test_bad_signature_401(client):
    resp = await post_event(client, mention_event(), secret="wrong-secret")
    assert resp.status_code == 401


async def test_app_mention_enqueues_bot_turn(client, pool):
    resp = await post_event(client, mention_event())
    assert resp.status_code == 200
    job = await pool.fetchrow("select data from jobs where name='bot_turn'")
    data = json.loads(job["data"])
    assert data == {"channel": "C0TEST", "thread_ts": "100.1", "user": "URYAN",
                    "text": "<@UBOT> hello"}


async def test_duplicate_event_id_enqueued_once(client, pool):
    await post_event(client, mention_event(event_id="EvDup"))
    await post_event(client, mention_event(event_id="EvDup"))
    assert (await pool.fetchval("select count(*) from jobs where name='bot_turn'")) == 1


async def test_wrong_channel_and_bot_messages_ignored(client, pool):
    await post_event(client, mention_event(event_id="Ev2", channel="C_OTHER"))
    await post_event(client, mention_event(event_id="Ev3", bot_id="B123"))
    await post_event(client, mention_event(event_id="Ev4", subtype="message_changed"))
    assert (await pool.fetchval("select count(*) from jobs")) == 0


async def test_thread_reply_continues_known_session_only(client, pool):
    unknown = mention_event(event_id="Ev5", etype="message", text="follow up",
                            ts="200.2", thread_ts="100.1")
    await post_event(client, unknown)
    assert (await pool.fetchval("select count(*) from jobs")) == 0
    await pool.execute(
        "insert into bot_sessions (thread_ts, channel) values ('100.1', 'C0TEST')")
    await post_event(client, mention_event(event_id="Ev6", etype="message", text="follow up",
                                           ts="200.3", thread_ts="100.1"))
    job = await pool.fetchrow("select data from jobs where name='bot_turn'")
    assert json.loads(job["data"])["thread_ts"] == "100.1"
    # a reply mentioning a teammate still goes through
    await post_event(client, mention_event(event_id="Ev7", etype="message",
                                           text="cc <@UTEAMMATE>", ts="200.4", thread_ts="100.1"))
    assert (await pool.fetchval("select count(*) from jobs")) == 2


async def test_tagged_thread_reply_processed_once(client, pool):
    """A tagged reply arrives as BOTH app_mention and message copy (distinct event_ids,
    same message ts) — only one turn may be enqueued."""
    await pool.execute(
        "insert into bot_sessions (thread_ts, channel) values ('100.1', 'C0TEST')")
    await post_event(client, mention_event(event_id="EvA", etype="app_mention",
                                           text="<@UBOT> again", ts="300.1", thread_ts="100.1"))
    await post_event(client, mention_event(event_id="EvB", etype="message",
                                           text="<@UBOT> again", ts="300.1", thread_ts="100.1"))
    assert (await pool.fetchval("select count(*) from jobs where name='bot_turn'")) == 1


async def test_quick_followup_before_first_turn_processed(client, pool):
    """Replies sent before the worker creates the session continue the thread because
    the opening bot_turn job is still queued."""
    await post_event(client, mention_event(event_id="EvC", text="<@UBOT> start", ts="400.1"))
    assert (await pool.fetchval("select count(*) from jobs")) == 1
    # no bot_session yet — but a queued job for the thread exists
    await post_event(client, mention_event(event_id="EvD", etype="message",
                                           text="oh and make it navy", ts="400.2",
                                           thread_ts="400.1"))
    assert (await pool.fetchval("select count(*) from jobs")) == 2
