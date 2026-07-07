import json
import uuid

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.services.audience import sync_audience
from app.services.dispatch import _unsub_url, build_headers, enqueue_campaign_sends
from app.services.ghl import GHLClient
from app.services.renderer import render_batch
from app.services.resend_client import ResendClient

async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)):
    expected = request.app.state.settings.api_key
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "invalid api key")


router = APIRouter(dependencies=[Depends(require_api_key)])


class CampaignIn(BaseModel):
    name: str
    subject: str
    template_ref: str
    template_version: str
    audience_filter: list[dict] = []
    scheduled_at: str | None = None


async def _get_campaign(request: Request, campaign_id: str):
    try:
        cid = uuid.UUID(campaign_id)
    except ValueError:
        raise HTTPException(404, "campaign not found")
    row = await request.app.state.pool.fetchrow("select * from campaigns where id=$1", cid)
    if row is None:
        raise HTTPException(404, "campaign not found")
    return row


@router.post("/campaigns", status_code=201)
async def create_campaign(request: Request, body: CampaignIn):
    row = await request.app.state.pool.fetchrow(
        "insert into campaigns (name, subject, template_ref, template_version, audience_filter) "
        "values ($1, $2, $3, $4, $5) returning id, status",
        body.name, body.subject, body.template_ref, body.template_version,
        json.dumps(body.audience_filter),
    )
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/campaigns/{campaign_id}/sync-audience")
async def sync_campaign_audience(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    settings = request.app.state.settings
    ghl = GHLClient(settings.ghl_pi_token, settings.ghl_location_id)
    return await sync_audience(request.app.state.pool, ghl, str(campaign["id"]))


@router.post("/campaigns/{campaign_id}/test")
async def test_send(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    settings = request.app.state.settings
    if not settings.seed_list:
        raise HTTPException(400, "SEED_EMAILS is not configured")
    resend = ResendClient(settings.resend_api_key, rps=settings.send_rps)
    for email in settings.seed_list:
        unsub = _unsub_url(settings, email, campaign["id"])
        rendered = (await render_batch(campaign["template_ref"], [{
            "firstName": "Seed", "unsubUrl": unsub,
        }]))[0]
        await resend.send_email({
            "from": settings.from_email,
            "to": [email],
            "subject": f"[TEST] {campaign['subject']}",
            "html": rendered.html,
            "text": rendered.text,
            "headers": build_headers(settings, unsub),
        })
    return {"sent_to": settings.seed_list}


@router.post("/campaigns/{campaign_id}/dispatch")
async def dispatch_campaign(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    if campaign["status"] == "paused":
        raise HTTPException(409, "campaign is paused by guardrails")
    queued = await enqueue_campaign_sends(request.app.state.pool, campaign["id"])
    return {"queued": queued}


@router.get("/campaigns/{campaign_id}/report")
async def campaign_report(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    pool = request.app.state.pool
    sends = await pool.fetchrow(
        """select count(*) as total,
                  count(*) filter (where status='sent') as sent,
                  count(*) filter (where status='queued') as queued,
                  count(*) filter (where status='failed') as failed,
                  count(*) filter (where status='suppressed') as suppressed
           from sends where campaign_id=$1""", campaign["id"])
    events = await pool.fetch(
        """select e.type, count(distinct e.send_id) as n
           from events e join sends s on s.id = e.send_id
           where s.campaign_id=$1 group by e.type""", campaign["id"])
    return {
        "campaign": {"id": str(campaign["id"]), "name": campaign["name"],
                     "status": campaign["status"]},
        "sends": dict(sends),
        "events": {r["type"]: r["n"] for r in events},
    }
