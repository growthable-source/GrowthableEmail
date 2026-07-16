import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response

from app.services.ghl import GHLClient
from app.services.jobs import enqueue
from app.services.notify import notify_campaign_going_out, notify_post_going_out
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
    if etype not in ("message", "app_mention"):
        return {"ok": True}
    # channel membership is already the trust boundary (dedicated bot channels) —
    # any message there starts or continues a conversation, tagged or not
    thread_ts = event.get("thread_ts") or event.get("ts")

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
        # the worker does the heavy lifting: 'broadcast' → one Resend broadcast to
        # the whole audience; 'timed' → ramped queue targeting each contact's
        # ideal local hour, throttled by per_day/per_hour
        audience = await pool.fetchval(
            "select count(*) from campaign_contacts where campaign_id=$1", campaign_id)
        per_day, per_hour = value.get("per_day"), value.get("per_hour")
        if per_day or per_hour:
            await pool.execute(
                "update campaigns set send_via='timed', per_day=$2, per_hour=$3 "
                "where id=$1", campaign_id, per_day, per_hour)
            limits = " · ".join(filter(None, [f"{per_day}/day" if per_day else None,
                                              f"{per_hour}/hour" if per_hour else None]))
            how = f"{audience} contacts ramped ({limits}, ideal local time)"
        else:
            await pool.execute(
                "update campaigns set send_via='broadcast' where id=$1", campaign_id)
            how = f"broadcast to {audience} contacts"
        when = value.get("when")
        when_dt = datetime.fromisoformat(when) if when else None
        if when_dt and when_dt > datetime.now(timezone.utc):
            await pool.execute(
                "update campaigns set status='scheduled', scheduled_at=$2 where id=$1",
                campaign_id, when_dt)
            scheduled_note = f"scheduled for {when}"
            going_out_now = False  # the worker announces this later, when it fires
        else:
            await pool.execute(
                "update campaigns set status='dispatching' where id=$1", campaign_id)
            scheduled_note = "starting now"
            going_out_now = True
        await slack.update_message(
            channel, message_ts,
            text=f"✅ Approved by <@{user}> — {how}, {scheduled_note}.")
        if going_out_now:
            await notify_campaign_going_out(pool, slack, campaign_id)
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
    if not schedule_iso:
        await notify_post_going_out(pool, slack, post_id)
    return Response(status_code=200)
