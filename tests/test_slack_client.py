import hashlib
import hmac
import time

import httpx
import pytest
import respx

from app.services.slack_client import SlackClient, SlackError, verify_slack_signature

SECRET = "slack-signing-secret"


def sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> tuple[str, str]:
    ts = ts or int(time.time())
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return str(ts), sig


def test_signature_roundtrip_and_rejects():
    body = b'{"type":"event_callback"}'
    ts, sig = sign(body)
    assert verify_slack_signature(SECRET, ts, body, sig) is True
    assert verify_slack_signature(SECRET, ts, body + b"x", sig) is False
    assert verify_slack_signature("other", ts, body, sig) is False
    old_ts, old_sig = sign(body, ts=int(time.time()) - 3600)
    assert verify_slack_signature(SECRET, old_ts, body, old_sig) is False
    assert verify_slack_signature(SECRET, "not-a-number", body, sig) is False
    assert verify_slack_signature(SECRET, ts, body, None) is False


@respx.mock
async def test_post_message_returns_ts():
    route = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "123.456"}))
    client = SlackClient("xoxb-test", rps=10_000, backoff_base=0)
    ts = await client.post_message("C1", text="hi", thread_ts="111.222")
    assert ts == "123.456"
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer xoxb-test"
    assert b'"thread_ts"' in req.read()


@respx.mock
async def test_slack_ok_false_raises():
    respx.post("https://slack.com/api/chat.update").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    client = SlackClient("xoxb-test", rps=10_000, backoff_base=0)
    with pytest.raises(SlackError, match="channel_not_found"):
        await client.update_message("C1", "123.456", text="edit")


@respx.mock
async def test_retries_429():
    route = respx.post("https://slack.com/api/chat.postMessage").mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json={"ok": True, "ts": "1.2"})])
    client = SlackClient("xoxb-test", rps=10_000, backoff_base=0)
    assert await client.post_message("C1", text="hi") == "1.2"
    assert route.call_count == 2
