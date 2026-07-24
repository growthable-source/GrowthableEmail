import json
import uuid

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.services.audience import sync_audience
from app.services.dispatch import enqueue_campaign_sends, send_seed
from app.services.ghl import GHLClient
from app.services.reports import campaign_report as campaign_report_data
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
    resend = ResendClient(settings.resend_api_key, rps=settings.send_rps)
    try:
        sent_to = await send_seed(request.app.state.pool, settings, resend, campaign)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"sent_to": sent_to}


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
    return await campaign_report_data(request.app.state.pool, campaign["id"])


# ---- Xovera outbound engine integration -----------------------------------------
# The outbound engine sends one hand-approved, per-prospect email at a time.
# It enrolls sends here (with subject/body overrides) instead of the GHL
# webhook path, and checks suppressions before it even personalizes.

class SuppressionCheckIn(BaseModel):
    emails: list[str]


@router.post("/suppressions/check")
async def check_suppressions(request: Request, body: SuppressionCheckIn):
    from app.services.suppressions import suppressed_subset
    suppressed = await suppressed_subset(request.app.state.pool, body.emails)
    return {"suppressed": sorted(suppressed)}


class OutboundEnrollIn(BaseModel):
    campaign_id: str
    contact_id: str          # GHL contact id (engine syncs to GHL before sending)
    email: str
    subject: str
    text_body: str
    first_name: str = ""
    last_name: str = ""


@router.post("/outbound/enroll")
async def outbound_enroll(request: Request, body: OutboundEnrollIn):
    from app.services.suppressions import is_suppressed, normalize
    pool = request.app.state.pool
    email = normalize(body.email)
    if await is_suppressed(pool, email):
        return {"enrolled": False, "reason": "suppressed"}
    campaign = await _get_campaign(request, body.campaign_id)
    if campaign["status"] == "paused":
        return {"enrolled": False, "reason": "campaign paused"}
    await pool.execute(
        """insert into contacts_cache (ghl_contact_id, email, first_name, last_name, custom)
           values ($1, $2, $3, $4, '{}')
           on conflict (ghl_contact_id) do update set
               email=excluded.email, first_name=excluded.first_name,
               last_name=excluded.last_name, synced_at=now()""",
        body.contact_id, email, body.first_name, body.last_name)
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2) "
        "on conflict do nothing", campaign["id"], body.contact_id)
    inserted = await pool.fetchrow(
        """insert into sends (campaign_id, ghl_contact_id, email,
                              subject_override, content_override)
           values ($1, $2, $3, $4, $5)
           on conflict (campaign_id, ghl_contact_id) do nothing
           returning id""",
        campaign["id"], body.contact_id, email,
        body.subject, json.dumps({"text_body": body.text_body}))
    # 'completed' revives too: outbound campaigns drain and refill as the
    # sales team approves more batches
    await pool.execute(
        "update campaigns set status='dispatching' "
        "where id=$1 and status in ('draft','ready','completed')",
        campaign["id"])

    # The dispatch claim gate only releases sends whose email has a 'valid'
    # verdict (fail-safe by exclusion). The GHL flow requests verification at
    # campaign start; the outbound engine enrolls one send at a time, so the
    # request happens here. Drip volumes sit far below the approval threshold,
    # so this always auto-submits; already-verified emails are never re-billed
    # (verdicts are permanent). Without this call, outbound sends would queue
    # forever.
    from app.services.verification import request_verification
    await request_verification(pool, request.app.state.settings, campaign["id"])

    if inserted is None:
        return {"enrolled": False, "reason": "already enrolled"}
    return {"enrolled": True, "send_id": str(inserted["id"])}


@router.get("/outbound/activity")
async def outbound_activity(request: Request, since: str | None = None, limit: int = 1000):
    """Engagement + replies feed the outbound engine polls to drive sequences.
    One row per send with event timestamps; replies joined by address."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """select s.email, s.ghl_contact_id, s.campaign_id::text, s.status,
                  s.subject_override, s.sent_at,
                  min(e.occurred_at) filter (where e.type='email.opened')  as opened_at,
                  min(e.occurred_at) filter (where e.type='email.clicked') as clicked_at,
                  min(e.occurred_at) filter (where e.type='email.bounced') as bounced_at,
                  r.classification as reply_classification,
                  r.summary as reply_summary,
                  r.created_at as replied_at
           from sends s
           left join events e on e.send_id = s.id
           left join lateral (select classification, summary, created_at from replies
                              where from_email = s.email and processed
                              order by created_at desc limit 1) r on true
           where s.content_override is not null
             and ($1::timestamptz is null or
                  greatest(coalesce(s.sent_at, s.created_at),
                           coalesce(r.created_at, s.created_at)) >= $1::timestamptz)
           group by s.id, r.classification, r.summary, r.created_at
           order by s.created_at desc
           limit $2""",
        since, limit)
    return {"data": [dict(r) for r in rows], "count": len(rows)}


class DomainIn(BaseModel):
    domain: str
    from_user: str = "ryan"
    from_name: str = "Ryan at Xovera"
    daily_cap: int = 30


@router.post("/outbound/domains")
async def add_sending_domain(request: Request, body: DomainIn):
    pool = request.app.state.pool
    row = await pool.fetchrow(
        """insert into sending_domains (domain, from_user, from_name, daily_cap)
           values ($1, $2, $3, $4)
           on conflict (domain) do update set active = true, paused_reason = null
           returning *""",
        body.domain.lower().strip(), body.from_user, body.from_name, body.daily_cap)
    return dict(row)


@router.get("/outbound/domains")
async def list_sending_domains(request: Request):
    pool = request.app.state.pool
    rows = await pool.fetch(
        """select d.*, count(s.id) filter (where s.sent_at >= date_trunc('day', now()))
                  as sent_today
           from sending_domains d left join sends s on s.from_domain = d.domain
           group by d.id order by d.created_at""")
    return {"data": [dict(r) for r in rows]}
