import hashlib
import html as html_lib
import json
import logging
import re
from collections import defaultdict

from app.config import Settings
from app.services.renderer import RenderError, Rendered, render_batch
from app.services.resend_client import HardSendError, ResendClient, TransientSendError
from app.services.suppressions import suppressed_subset
from app.services.unsub_tokens import make_token

log = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_SEND_RETRIES = 3
RETRY_BASE_SECONDS = 120


async def enqueue_campaign_sends(pool, campaign_id) -> int:
    """Fill the send queue for a campaign. Idempotent; marks campaign dispatching."""
    inserted = await pool.fetchval(
        """with ins as (
               insert into sends (campaign_id, ghl_contact_id, email)
               select cc.campaign_id, cc.ghl_contact_id, c.email
               from campaign_contacts cc
               join contacts_cache c using (ghl_contact_id)
               where cc.campaign_id = $1
                 and c.dnd = false
                 and not exists (select 1 from suppressions s where s.email = c.email)
               on conflict (campaign_id, ghl_contact_id) do nothing
               returning 1)
           select count(*) from ins""",
        campaign_id,
    )
    await pool.execute(
        "update campaigns set status='dispatching' where id=$1 and status in ('draft','ready')",
        campaign_id,
    )
    return inserted


async def send_seed(pool, settings: Settings, resend: ResendClient, campaign) -> list[str]:
    """Render with the campaign's stored content and send to the seed list only."""
    if not settings.seed_list:
        raise ValueError("SEED_EMAILS is not configured")
    content = json.loads(campaign["content"]) if "content" in campaign.keys() else {}
    for email in settings.seed_list:
        unsub = _unsub_url(settings, email, campaign["id"])
        if campaign["template_ref"] == "custom":
            rendered = render_full_document(content, "Seed", None, unsub)
        else:
            props = build_props(content, "Seed", None, {}, unsub)
            rendered = (await render_batch(campaign["template_ref"], [props]))[0]
        await resend.send_email({
            "from": settings.from_email,
            "to": [email],
            "subject": f"[TEST] {campaign['subject']}",
            "html": rendered.html,
            "text": rendered.text,
            "headers": build_headers(settings, unsub),
        })
    await pool.execute(
        "update campaigns set seed_tested_at=now() where id=$1", campaign["id"])
    return settings.seed_list


async def requeue_stale(pool, stale_minutes: int = 10) -> int:
    """Return crashed 'sending' claims to the queue (worker restart recovery)."""
    result = await pool.execute(
        "update sends set status='queued' where status='sending' "
        "and next_attempt_at < now() - make_interval(mins => $1)", stale_minutes)
    return int(result.split()[-1])


async def promote_scheduled(pool) -> list:
    """Activate scheduled campaigns whose time has arrived. Returns the campaign ids
    that just transitioned, so callers can announce them exactly once."""
    rows = await pool.fetch(
        "update campaigns set status='dispatching' "
        "where status='scheduled' and scheduled_at <= now() returning id")
    return [r["id"] for r in rows]


def _unsub_url(settings: Settings, email: str, campaign_id) -> str:
    token = make_token(email, str(campaign_id), settings.unsub_signing_secret)
    return f"{settings.public_base_url}/u/{token}"


def build_props(content: dict, first_name: str | None, last_name: str | None,
                custom_fields: dict, unsub_url: str) -> dict:
    """React-template props for one recipient: content + contact fields (contact wins)."""
    return {
        **content,
        "firstName": first_name or None,
        "lastName": last_name or None,
        **custom_fields,
        "unsubUrl": unsub_url,
    }


def personalize_full_html(html: str, first_name: str | None, last_name: str | None,
                          unsub_url: str) -> str:
    """Merge tokens into a bot-authored full HTML document ('custom' template)."""
    return (html.replace("{{first_name}}", first_name or "there")
                .replace("{{firstName}}", first_name or "there")
                .replace("{{last_name}}", last_name or "")
                .replace("{{lastName}}", last_name or "")
                .replace("{{unsubscribe_url}}", unsub_url)
                .replace("{{preferences_url}}", unsub_url))


def html_to_text(html: str) -> str:
    """Plain-text part for full-document emails (spec §4: always send a text part)."""
    text = re.sub(r"<(head|style|script)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|tr|td|h1|h2|h3|div)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def render_full_document(content: dict, first_name: str | None, last_name: str | None,
                         unsub_url: str) -> Rendered:
    raw = content.get("html_body") or ""
    if "{{unsubscribe_url}}" not in raw:
        # compliance backstop (spec §12) — bot tools validate, this catches manual rows
        raise RenderError("custom html_body missing {{unsubscribe_url}}")
    html = personalize_full_html(raw, first_name, last_name, unsub_url)
    return Rendered(html=html, text=html_to_text(html),
                    hash=hashlib.sha256(html.encode()).hexdigest())


def render_outbound(content: dict, unsub_url: str) -> Rendered:
    """Plain-text outbound email (Xovera engine) -> minimal HTML + text parts.
    These are one-to-one cold emails, so the styling stays deliberately bare."""
    text = (content.get("text_body") or "").strip()
    if not text:
        raise RenderError("outbound send has empty text_body")
    paras = "".join(
        f'<p style="margin:0 0 1em">{html_lib.escape(p).replace(chr(10), "<br>")}</p>'
        for p in re.split(r"\n{2,}", text)
    )
    html = (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;color:#1c1917;line-height:1.6;max-width:600px">{paras}'
        f'<p style="color:#a8a29e;font-size:12px">&mdash;<br>'
        f'<a href="{unsub_url}" style="color:#a8a29e">Unsubscribe</a></p></body></html>'
    )
    return Rendered(html=html, text=f"{text}\n\nUnsubscribe: {unsub_url}",
                    hash=hashlib.sha256(html.encode()).hexdigest())


def build_headers(settings: Settings, unsub_url: str) -> dict:
    return {
        "List-Unsubscribe": f"<mailto:unsubscribe@{settings.from_domain}>, <{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


async def _claim_batch(pool, limit: int) -> list[dict]:
    rows = await pool.fetch(
        """update sends set status='sending', next_attempt_at=now()
           where id in (
               select s.id from sends s
               join campaigns c on c.id = s.campaign_id
               where s.status='queued' and s.next_attempt_at <= now()
                 and c.status='dispatching'
               order by s.created_at
               limit $1
               for update of s skip locked)
           returning id, campaign_id, ghl_contact_id, email, retry_count,
                     subject_override, content_override""",
        limit,
    )
    return [dict(r) for r in rows]


async def _mark_transient_failure(pool, send_id, retry_count: int, error: str) -> None:
    if retry_count + 1 >= MAX_SEND_RETRIES:
        await pool.execute(
            "update sends set status='failed', error=$2 where id=$1", send_id, error[:500])
    else:
        await pool.execute(
            """update sends set status='queued', retry_count=retry_count+1,
                   next_attempt_at=now() + make_interval(secs => $2 * power(2, retry_count)),
                   error=$3
               where id=$1""",
            send_id, RETRY_BASE_SECONDS, error[:500],
        )


async def process_send_queue(pool, settings: Settings, resend: ResendClient) -> int:
    """One worker pass. Returns number of emails successfully handed to Resend."""
    sent_today = await pool.fetchval(
        "select count(*) from sends where sent_at >= date_trunc('day', now())")
    remaining = settings.daily_send_cap - sent_today
    if remaining <= 0:
        log.info("daily cap %s reached; dispatch resumes tomorrow", settings.daily_send_cap)
        return 0

    claimed = await _claim_batch(pool, min(BATCH_SIZE, remaining))
    if not claimed:
        return 0

    # Dispatch-time suppression re-check (spec §5)
    suppressed = await suppressed_subset(pool, [s["email"] for s in claimed])
    to_send = []
    for send in claimed:
        if send["email"] in suppressed:
            await pool.execute(
                "update sends set status='suppressed' where id=$1", send["id"])
        else:
            to_send.append(send)

    sent_count = 0

    # Per-send personalized emails (outbound engine) render individually;
    # everything else renders per campaign below.
    overrides = [s for s in to_send if s["content_override"]]
    to_send = [s for s in to_send if not s["content_override"]]
    for send in overrides:
        unsub = _unsub_url(settings, send["email"], send["campaign_id"])
        try:
            r = render_outbound(json.loads(send["content_override"]), unsub)
        except RenderError as exc:
            await _mark_transient_failure(pool, send["id"], send["retry_count"], str(exc))
            continue
        from app.services.domains import pick_from
        from_email, from_domain = await pick_from(pool, settings)
        payload = {
            "from": from_email,
            "to": [send["email"]],
            "subject": send["subject_override"] or "Quick question",
            "html": r.html,
            "text": r.text,
            "headers": build_headers(settings, unsub),
        }
        try:
            email_id = await resend.send_email(payload)
        except TransientSendError as exc:
            await _mark_transient_failure(pool, send["id"], send["retry_count"], str(exc))
            continue
        except HardSendError as exc:
            await pool.execute(
                "update sends set status='failed', error=$2 where id=$1",
                send["id"], str(exc)[:500])
            continue
        await pool.execute(
            "update sends set status='sent', resend_email_id=$2, rendered_hash=$3, "
            "from_domain=$4, sent_at=now() where id=$1",
            send["id"], email_id, r.hash, from_domain)
        sent_count += 1

    # Group by campaign so each group is one render subprocess call
    by_campaign: dict = defaultdict(list)
    for send in to_send:
        by_campaign[send["campaign_id"]].append(send)
    for campaign_id, sends in by_campaign.items():
        campaign = await pool.fetchrow(
            "select subject, template_ref, content from campaigns where id=$1", campaign_id)
        content = json.loads(campaign["content"])
        contacts, unsub_urls = [], []
        for send in sends:
            contact = await pool.fetchrow(
                "select first_name, last_name, custom from contacts_cache "
                "where ghl_contact_id=$1", send["ghl_contact_id"])
            contacts.append(contact)
            unsub_urls.append(_unsub_url(settings, send["email"], campaign_id))
        try:
            if campaign["template_ref"] == "custom":
                rendered = [
                    render_full_document(
                        content,
                        contact["first_name"] if contact else None,
                        contact["last_name"] if contact else None,
                        unsub)
                    for contact, unsub in zip(contacts, unsub_urls)
                ]
            else:
                props_list = [
                    build_props(
                        content,
                        contact["first_name"] if contact else None,
                        contact["last_name"] if contact else None,
                        json.loads(contact["custom"]) if contact else {}, unsub)
                    for contact, unsub in zip(contacts, unsub_urls)
                ]
                rendered = await render_batch(campaign["template_ref"], props_list)
        except RenderError as exc:
            log.error("render failed for campaign %s: %s", campaign_id, exc)
            for send in sends:
                await _mark_transient_failure(pool, send["id"], send["retry_count"], str(exc))
            continue

        for send, unsub, r in zip(sends, unsub_urls, rendered):
            payload = {
                "from": settings.from_email,
                "to": [send["email"]],
                "subject": campaign["subject"],
                "html": r.html,
                "text": r.text,
                "headers": build_headers(settings, unsub),
            }
            try:
                email_id = await resend.send_email(payload)
            except TransientSendError as exc:
                await _mark_transient_failure(pool, send["id"], send["retry_count"], str(exc))
                continue
            except HardSendError as exc:
                await pool.execute(
                    "update sends set status='failed', error=$2 where id=$1",
                    send["id"], str(exc)[:500])
                continue
            await pool.execute(
                "update sends set status='sent', resend_email_id=$2, rendered_hash=$3, "
                "sent_at=now() where id=$1",
                send["id"], email_id, r.hash)
            sent_count += 1

    # Close out campaigns whose queues drained
    await pool.execute(
        """update campaigns set status='completed'
           where status='dispatching'
             and not exists (select 1 from sends
                             where campaign_id = campaigns.id
                               and status in ('queued', 'sending'))""")
    return sent_count
