import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from svix.webhooks import Webhook, WebhookVerificationError

from app.services.jobs import enqueue
from app.services.suppressions import add_suppression, is_suppressed, normalize

log = logging.getLogger(__name__)
router = APIRouter()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


ENGAGEMENT_TAG_PREFIX = {
    "email.delivered": "emailed",
    "email.opened": "opened",
    "email.clicked": "clicked",
}


@router.post("/webhooks/resend")
async def resend_webhook(request: Request):
    payload = await request.body()
    settings = request.app.state.settings
    try:
        event = Webhook(settings.resend_webhook_secret).verify(
            payload, dict(request.headers))
    except WebhookVerificationError:
        raise HTTPException(401, "invalid signature")

    pool = request.app.state.pool
    event_type = event.get("type", "")
    data = event.get("data") or {}

    # Broadcast unsubscribes are handled by Resend (hosted link) and surface as
    # contact.updated with unsubscribed=true — mirror them into suppressions.
    if event_type.startswith("contact."):
        email = normalize(data.get("email") or "")
        if event_type in ("contact.updated", "contact.created") and \
                data.get("unsubscribed") and email:
            await add_suppression(pool, email, reason="unsubscribe", source="resend")
        await pool.execute(
            "insert into events (send_id, type, payload) values (null, $1, $2)",
            event_type, json.dumps(event))
        return {"ok": True}

    email_id = data.get("email_id")
    send = None
    if email_id:
        send = await pool.fetchrow(
            """select s.id, s.ghl_contact_id, s.email, c.name as campaign_name
               from sends s join campaigns c on c.id = s.campaign_id
               where s.resend_email_id = $1""", email_id)
    if send is None and data.get("broadcast_id"):
        # broadcast recipients have no per-send email id until the first event
        # arrives — match on (campaign, recipient) and backfill the email id
        to = data.get("to")
        recipient = to[0] if isinstance(to, list) and to else to
        if recipient:
            send = await pool.fetchrow(
                """select s.id, s.ghl_contact_id, s.email, c.name as campaign_name
                   from sends s join campaigns c on c.id = s.campaign_id
                   where c.resend_broadcast_id = $1 and lower(s.email) = lower($2)""",
                data["broadcast_id"], recipient)
            if send is not None and email_id:
                await pool.execute(
                    "update sends set resend_email_id=$2 where id=$1 "
                    "and resend_email_id is null", send["id"], email_id)

    await pool.execute(
        "insert into events (send_id, type, payload) values ($1, $2, $3)",
        send["id"] if send else None, event_type, json.dumps(event))

    if send is None:
        log.warning("resend event %s for unknown email_id %s", event_type, email_id)
        return {"ok": True}

    slug = slugify(send["campaign_name"])
    if event_type in ENGAGEMENT_TAG_PREFIX:
        await enqueue(pool, "ghl_writeback", {
            "kind": "add_tags", "contact_id": send["ghl_contact_id"],
            "tags": [f"{ENGAGEMENT_TAG_PREFIX[event_type]}-{slug}"]})
    elif event_type == "email.bounced":
        bounce_type = (data.get("bounce") or {}).get("type", "Permanent")
        if bounce_type != "Transient":  # treat unknown as hard (conservative)
            await add_suppression(pool, send["email"], reason="hard_bounce",
                                  source="resend", ghl_contact_id=send["ghl_contact_id"])
            await enqueue(pool, "ghl_writeback",
                          {"kind": "set_dnd", "contact_id": send["ghl_contact_id"]})
    elif event_type == "email.complained":
        await add_suppression(pool, send["email"], reason="complaint",
                              source="resend", ghl_contact_id=send["ghl_contact_id"])
        await enqueue(pool, "ghl_writeback",
                      {"kind": "set_dnd", "contact_id": send["ghl_contact_id"]})
        await enqueue(pool, "ghl_writeback", {
            "kind": "add_tags", "contact_id": send["ghl_contact_id"],
            "tags": ["complained"]})
    return {"ok": True}


def _check_ghl_secret(request: Request) -> None:
    if request.headers.get("x-webhook-secret") != request.app.state.settings.ghl_webhook_secret:
        raise HTTPException(403, "bad webhook secret")


class EnrollIn(BaseModel):
    campaign_id: str
    contact_id: str
    email: str
    first_name: str = ""
    last_name: str = ""
    custom: dict = {}


@router.post("/webhooks/ghl/enroll")
async def ghl_enroll(request: Request, body: EnrollIn):
    _check_ghl_secret(request)
    pool = request.app.state.pool
    email = normalize(body.email)
    if await is_suppressed(pool, email):
        return {"enrolled": False, "reason": "suppressed"}
    campaign = await pool.fetchrow(
        "select id, status from campaigns where id=$1::uuid", body.campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    if campaign["status"] == "paused":
        return {"enrolled": False, "reason": "campaign paused"}
    await pool.execute(
        """insert into contacts_cache (ghl_contact_id, email, first_name, last_name, custom)
           values ($1, $2, $3, $4, $5)
           on conflict (ghl_contact_id) do update set
               email=excluded.email, first_name=excluded.first_name,
               last_name=excluded.last_name, custom=excluded.custom, synced_at=now()""",
        body.contact_id, email, body.first_name, body.last_name, json.dumps(body.custom))
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2) "
        "on conflict do nothing", campaign["id"], body.contact_id)
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email) values ($1, $2, $3) "
        "on conflict (campaign_id, ghl_contact_id) do nothing",
        campaign["id"], body.contact_id, email)
    await pool.execute(
        "update campaigns set status='dispatching' where id=$1 and status in ('draft','ready')",
        campaign["id"])
    return {"enrolled": True}


class DndIn(BaseModel):
    email: str
    contact_id: str | None = None


@router.post("/webhooks/ghl/dnd")
async def ghl_dnd(request: Request, body: DndIn):
    _check_ghl_secret(request)
    await add_suppression(request.app.state.pool, body.email, reason="ghl_dnd",
                          source="ghl", ghl_contact_id=body.contact_id)
    return {"ok": True}
