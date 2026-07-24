"""Campaign dispatch via Resend Broadcasts: the whole audience goes out in one
API call instead of dripping through the per-email queue (which stays for seed
tests and GHL enrollments, and keeps the daily cap).

Two-stage state machine, driven from the worker tick:
  1. import — push the synced, non-suppressed audience into a per-campaign
     Resend segment with the bulk CSV import API (one call for 50k+ contacts).
     Starts as soon as the campaign is approved, even for scheduled sends.
  2. send — once the import completes and the campaign is dispatching, create
     the broadcast against that segment with send=true. Resend substitutes the
     merge tags per recipient and hosts/handles the unsubscribe flow.
"""
import csv
import io
import json
import logging

from app.config import Settings
from app.services.dispatch import build_props
from app.services.renderer import RenderError, render_batch
from app.services.resend_client import HardSendError, ResendClient

log = logging.getLogger(__name__)

# Resend broadcast merge tags (substituted server-side per recipient)
FIRST_NAME_TAG = "{{{contact.first_name|there}}}"
LAST_NAME_TAG = "{{{contact.last_name}}}"
UNSUB_TAG = "{{{RESEND_UNSUBSCRIBE_URL}}}"

COLUMN_MAP = {"email": "email", "first_name": "first_name", "last_name": "last_name"}

# Same drop rules as the queue path (spec §5): dnd + suppression re-check at dispatch,
# plus a verified-valid verdict
AUDIENCE_SQL = """
    select cc.ghl_contact_id, c.email, c.first_name, c.last_name
    from campaign_contacts cc
    join contacts_cache c using (ghl_contact_id)
    where cc.campaign_id = $1
      and c.dnd = false
      and not exists (select 1 from suppressions s where s.email = c.email)
      and exists (select 1 from email_verifications v
                  where v.email = c.email and v.verdict = 'valid')
"""


def broadcast_full_html(raw: str) -> str:
    """Translate our per-recipient tokens into Resend broadcast merge tags."""
    return (raw.replace("{{first_name}}", FIRST_NAME_TAG)
               .replace("{{firstName}}", FIRST_NAME_TAG)
               .replace("{{last_name}}", LAST_NAME_TAG)
               .replace("{{lastName}}", LAST_NAME_TAG)
               .replace("{{unsubscribe_url}}", UNSUB_TAG)
               .replace("{{preferences_url}}", UNSUB_TAG))


async def render_broadcast_html(campaign) -> str:
    """One HTML body for the whole audience; personalization via merge tags."""
    content = json.loads(campaign["content"])
    if campaign["template_ref"] == "custom":
        raw = content.get("html_body") or ""
        if "{{unsubscribe_url}}" not in raw:
            raise RenderError("custom html_body missing {{unsubscribe_url}}")
        return broadcast_full_html(raw)
    props = build_props(content, FIRST_NAME_TAG, LAST_NAME_TAG, {}, UNSUB_TAG)
    return (await render_batch(campaign["template_ref"], [props]))[0].html


async def _audience_csv(pool, campaign_id) -> tuple[bytes, int]:
    rows = await pool.fetch(AUDIENCE_SQL, campaign_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "first_name", "last_name"])
    for r in rows:
        writer.writerow([r["email"], r["first_name"], r["last_name"]])
    return buf.getvalue().encode(), len(rows)


async def _fail(pool, slack, campaign, reason: str) -> None:
    log.error("broadcast for campaign %s paused: %s", campaign["id"], reason)
    await pool.execute("update campaigns set status='paused' where id=$1", campaign["id"])
    if slack is not None and campaign["channel"]:
        await slack.post_message(
            campaign["channel"],
            text=f"⚠️ *{campaign['name']}* paused: {reason}",
            thread_ts=campaign["thread_ts"])


async def _start_import(pool, resend: ResendClient, slack, campaign) -> None:
    csv_bytes, count = await _audience_csv(pool, campaign["id"])
    if count == 0:
        await _fail(pool, slack, campaign,
                    "audience is empty — run sync_audience (and finish "
                    "verification), then approve again")
        return
    segment_id = campaign["resend_segment_id"]
    if segment_id is None:
        segment_id = await resend.create_segment(
            f"campaign-{str(campaign['id'])[:8]}-{campaign['name'][:60]}")
        await pool.execute("update campaigns set resend_segment_id=$2 where id=$1",
                           campaign["id"], segment_id)
    import_id = await resend.create_contact_import(csv_bytes, COLUMN_MAP, segment_id)
    await pool.execute("update campaigns set resend_import_id=$2 where id=$1",
                       campaign["id"], import_id)
    log.info("broadcast import %s started for campaign %s (%s contacts)",
             import_id, campaign["id"], count)


async def _send_if_imported(pool, settings: Settings, resend: ResendClient,
                            slack, campaign) -> int:
    imp = await resend.get_contact_import(campaign["resend_import_id"])
    status = imp.get("status")
    if status in ("failed", "cancelled", "canceled"):
        await _fail(pool, slack, campaign, f"Resend contact import {status}")
        return 0
    if status not in ("completed", "complete"):
        return 0  # still processing — poll again next tick

    html = await render_broadcast_html(campaign)
    broadcast_id = await resend.create_broadcast({
        "segment_id": campaign["resend_segment_id"],
        "from": settings.from_email,
        "subject": campaign["subject"],
        "name": campaign["name"],
        "html": html,
        "send": True,
    })
    await pool.execute(
        "update campaigns set resend_broadcast_id=$2, status='completed' where id=$1",
        campaign["id"], broadcast_id)
    # Mirror the audience into sends so reports, guardrails and webhook events
    # keep working; via='broadcast' keeps these out of the queue's daily cap.
    recipients = await pool.fetchval(
        """with ins as (
               insert into sends (campaign_id, ghl_contact_id, email, status, via, sent_at)
               select cc.campaign_id, cc.ghl_contact_id, c.email, 'sent', 'broadcast', now()
               from campaign_contacts cc
               join contacts_cache c using (ghl_contact_id)
               where cc.campaign_id = $1
                 and c.dnd = false
                 and not exists (select 1 from suppressions s where s.email = c.email)
                 and exists (select 1 from email_verifications v
                             where v.email = c.email and v.verdict = 'valid')
               on conflict (campaign_id, ghl_contact_id) do nothing
               returning 1)
           select count(*) from ins""",
        campaign["id"])
    log.info("broadcast %s created for campaign %s (%s recipients)",
             broadcast_id, campaign["id"], recipients)
    if slack is not None and campaign["channel"]:
        await slack.post_message(
            campaign["channel"],
            text=f"📬 *{campaign['name']}* handed to Resend as one broadcast — "
                 f"{recipients} recipients.",
            thread_ts=campaign["thread_ts"])
    return 1


async def process_broadcast_campaigns(pool, settings: Settings, resend: ResendClient,
                                      slack=None) -> int:
    """One worker pass over broadcast-mode campaigns. Returns broadcasts created."""
    to_import = await pool.fetch(
        "select * from campaigns where send_via='broadcast' and resend_import_id is null "
        "and status in ('scheduled', 'dispatching')")
    for campaign in to_import:
        try:
            await _start_import(pool, resend, slack, campaign)
        except HardSendError as exc:
            await _fail(pool, slack, campaign, f"audience import rejected: {exc}")
        except Exception:  # transient (network/5xx) — retry next tick
            log.exception("broadcast import start failed for %s", campaign["id"])

    created = 0
    to_send = await pool.fetch(
        "select * from campaigns where send_via='broadcast' and status='dispatching' "
        "and resend_import_id is not null and resend_broadcast_id is null")
    for campaign in to_send:
        try:
            created += await _send_if_imported(pool, settings, resend, slack, campaign)
        except HardSendError as exc:
            await _fail(pool, slack, campaign, f"broadcast rejected: {exc}")
        except RenderError as exc:
            await _fail(pool, slack, campaign, f"render failed: {exc}")
        except Exception:  # transient — retry next tick
            log.exception("broadcast send failed for %s", campaign["id"])
    return created
