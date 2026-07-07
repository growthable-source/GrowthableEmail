import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.services.jobs import enqueue
from app.services.slack_client import verify_slack_signature

log = logging.getLogger(__name__)
router = APIRouter()


async def verified_body(request: Request) -> bytes:
    body = await request.body()
    settings = request.app.state.settings
    if not verify_slack_signature(
            settings.slack_signing_secret,
            request.headers.get("x-slack-request-timestamp"),
            body,
            request.headers.get("x-slack-signature")):
        raise HTTPException(401, "bad slack signature")
    return body


@router.post("/slack/events")
async def slack_events(request: Request):
    body = await verified_body(request)
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    pool = request.app.state.pool
    settings = request.app.state.settings
    event = payload.get("event") or {}
    event_id = payload.get("event_id")
    if not event_id:
        return {"ok": True}
    fresh = await pool.fetchval(
        "insert into slack_events (event_id) values ($1) "
        "on conflict do nothing returning event_id", event_id)
    if fresh is None:
        return {"ok": True}
    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True}
    if event.get("channel") != settings.slack_channel_id:
        return {"ok": True}

    etype = event.get("type")
    thread_ts = event.get("thread_ts") or event.get("ts")
    if etype == "message":
        # only continue threads the bot owns; mention-copies are handled via app_mention
        if "<@" in (event.get("text") or ""):
            return {"ok": True}
        known = await pool.fetchval(
            "select 1 from bot_sessions where thread_ts=$1", thread_ts)
        if not known:
            return {"ok": True}
    elif etype != "app_mention":
        return {"ok": True}

    await enqueue(pool, "bot_turn", {
        "channel": event["channel"], "thread_ts": thread_ts,
        "user": event.get("user", ""), "text": event.get("text", "")})
    return {"ok": True}
