import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response

from app.services.dispatch import enqueue_campaign_sends
from app.services.ghl import GHLClient
from app.services.jobs import enqueue
from app.services.slack_client import SlackClient, verify_slack_signature

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
    configured_channels = {c for c in (settings.slack_channel_id,
                                       settings.slack_social_channel_id) if c}
    if event.get("channel") not in configured_channels:
        return {"ok": True}

    etype = event.get("type")
    thread_ts = event.get("thread_ts") or event.get("ts")
    if etype == "message":
        # continue threads the bot owns — session exists OR an opening turn is still
        # queued (covers quick follow-ups sent before the worker's first reply)
        known = await pool.fetchval(
            """select exists(select 1 from bot_sessions where thread_ts=$1)
                   or exists(select 1 from jobs where name='bot_turn'
                             and data->>'thread_ts' = $1)""", thread_ts)
        if not known:
            return {"ok": True}
    elif etype != "app_mention":
        return {"ok": True}

    # a tagged reply arrives twice (app_mention + message copy) under different
    # event_ids — dedupe on the message itself right before enqueueing
    message_key = f"msg:{event.get('channel')}:{event.get('ts')}"
    fresh_message = await pool.fetchval(
        "insert into slack_events (event_id) values ($1) "
        "on conflict do nothing returning event_id", message_key)
    if fresh_message is None:
        return {"ok": True}

    await enqueue(pool, "bot_turn", {
        "channel": event["channel"], "thread_ts": thread_ts,
        "user": event.get("user", ""), "text": event.get("text", "")})
    return {"ok": True}


@router.post("/slack/interactions")
async def slack_interactions(request: Request):
    body = await verified_body(request)
    payload = json.loads(parse_qs(body.decode())["payload"][0])
    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return Response(status_code=200)
    action = payload["actions"][0]
    value = json.loads(action["value"])
    channel = payload["channel"]["id"]
    message_ts = payload["container"]["message_ts"]
    user = payload["user"]["id"]

    pool = request.app.state.pool
    settings = request.app.state.settings
    slack = SlackClient(settings.slack_bot_token)

    if action["action_id"] in ("approve_post", "cancel_post"):
        return await _handle_post_action(pool, settings, slack, action, value,
                                         channel, message_ts, user)

    campaign_id = uuid.UUID(value["campaign_id"])
    campaign = await pool.fetchrow("select status from campaigns where id=$1", campaign_id)
    if campaign is None:
        await slack.update_message(channel, message_ts, text="Campaign no longer exists.")
        return Response(status_code=200)
    if campaign["status"] not in ("draft", "ready"):
        await slack.update_message(
            channel, message_ts,
            text=f"Already handled (status: {campaign['status']}).")
        return Response(status_code=200)

    if action["action_id"] == "cancel_send":
        await slack.update_message(channel, message_ts, text=f"❌ Cancelled by <@{user}>.")
        return Response(status_code=200)

    if action["action_id"] == "approve_send":
        queued = await enqueue_campaign_sends(pool, campaign_id)
        when = value.get("when")
        scheduled_note = "sending now"
        if when:
            when_dt = datetime.fromisoformat(when)
            if when_dt > datetime.now(timezone.utc):
                await pool.execute(
                    "update campaigns set status='scheduled', scheduled_at=$2 where id=$1",
                    campaign_id, when_dt)
                scheduled_note = f"scheduled for {when}"
        await slack.update_message(
            channel, message_ts,
            text=f"✅ Approved by <@{user}> — {queued} contacts queued, {scheduled_note}.")
    return Response(status_code=200)


async def _handle_post_action(pool, settings, slack, action, value, channel,
                              message_ts, user) -> Response:
    post_id = uuid.UUID(value["post_id"])
    post = await pool.fetchrow("select * from social_posts where id=$1", post_id)
    if post is None:
        await slack.update_message(channel, message_ts, text="Post no longer exists.")
        return Response(status_code=200)
    if post["status"] != "draft":
        await slack.update_message(
            channel, message_ts, text=f"Already handled (status: {post['status']}).")
        return Response(status_code=200)

    if action["action_id"] == "cancel_post":
        await pool.execute(
            "update social_posts set status='cancelled' where id=$1", post_id)
        await slack.update_message(channel, message_ts, text=f"❌ Cancelled by <@{user}>.")
        return Response(status_code=200)

    content = json.loads(post["content"])
    when = value.get("when")
    schedule_iso = None
    if when:
        when_dt = datetime.fromisoformat(when)
        if when_dt > datetime.now(timezone.utc):
            schedule_iso = when_dt.astimezone(timezone.utc).isoformat()
    ghl = GHLClient(settings.ghl_pi_token, settings.ghl_location_id)
    try:
        ghl_post_id = await ghl.create_social_post(
            post["account_ids"], content["text"], content.get("media") or [],
            schedule_at_iso=schedule_iso)
    except Exception as exc:
        log.exception("social publish failed for post %s", post_id)
        await slack.update_message(
            channel, message_ts,
            text=f"⚠️ Publish failed: {str(exc)[:200]} — buttons are still live, try again.")
        return Response(status_code=200)
    new_status = "scheduled" if schedule_iso else "published"
    await pool.execute(
        "update social_posts set status=$2, schedule_at=$3, ghl_post_id=$4 where id=$1",
        post_id, new_status, datetime.fromisoformat(when) if when else None, ghl_post_id)
    note = f"scheduled for {when}" if schedule_iso else "publishing now"
    await slack.update_message(
        channel, message_ts, text=f"✅ Approved by <@{user}> — {note} (GHL post {ghl_post_id}).")
    return Response(status_code=200)
