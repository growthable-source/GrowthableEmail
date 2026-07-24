import hashlib
import html as html_lib
import json
import logging
import re
import urllib.parse
from datetime import datetime, timezone

from app.config import Settings
from app.services.renderer import RenderError, Rendered, render_batch
from app.services.resend_client import HardSendError, ResendClient, TransientSendError
from app.services.sendtime import next_ideal_time, resolve_timezone
from app.services.suppressions import suppressed_subset
from app.services.unsub_tokens import make_token

log = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_SEND_RETRIES = 3
RETRY_BASE_SECONDS = 120
WINDOW_HOURS = 8  # timed sends missed by more than this roll to the next day's window


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
                 and exists (select 1 from email_verifications v
                             where v.email = c.email and v.verdict = 'valid')
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


async def enqueue_timed_sends(pool, settings: Settings, campaign_id) -> int:
    """Fill the queue with per-contact ideal-local-time schedules (ramped sends).
    Each contact's timezone comes from GHL (explicit tz, else country, else US)."""
    rows = await pool.fetch(
        """select cc.ghl_contact_id, c.email, c.country, c.timezone
           from campaign_contacts cc
           join contacts_cache c using (ghl_contact_id)
           where cc.campaign_id = $1
             and c.dnd = false
             and not exists (select 1 from suppressions s where s.email = c.email)
             and exists (select 1 from email_verifications v
                         where v.email = c.email and v.verdict = 'valid')""",
        campaign_id,
    )
    now = datetime.now(timezone.utc)
    records = []
    for r in rows:
        tz = resolve_timezone(r["country"], r["timezone"])
        records.append((campaign_id, r["ghl_contact_id"], r["email"], tz,
                        next_ideal_time(tz, now, settings.ideal_send_hour)))
    await pool.executemany(
        """insert into sends (campaign_id, ghl_contact_id, email, timezone, next_attempt_at)
           values ($1, $2, $3, $4, $5)
           on conflict (campaign_id, ghl_contact_id) do nothing""",
        records,
    )
    return len(records)


async def ensure_timed_queues(pool, settings: Settings, slack=None) -> None:
    """Fill the queue for approved timed campaigns (idempotent: runs once each).
    Posts the launch confirmation from HERE, not the approval click — this is the
    moment the queue physically exists, and it fires for every launch path
    (button, scheduled promotion, or a manual status flip in SQL)."""
    rows = await pool.fetch(
        """select id, name, channel, thread_ts, per_day, per_hour from campaigns c
           where status='dispatching' and send_via='timed'
             and not exists (select 1 from sends s where s.campaign_id = c.id)""")
    for r in rows:
        queued = await enqueue_timed_sends(pool, settings, r["id"])
        if queued == 0:
            # zero verified can mean verification is still in flight (verdicts
            # land minutes after launch) — pausing now would strand the campaign
            # forever, since auto-resume only touches campaigns with queued sends
            pending = await pool.fetchval(
                """select count(*) from campaign_contacts cc
                   join contacts_cache c using (ghl_contact_id)
                   where cc.campaign_id = $1 and c.dnd = false
                     and not exists (select 1 from suppressions s where s.email = c.email)
                     and not exists (select 1 from email_verifications v
                                     where v.email = c.email)""", r["id"])
            if pending:
                log.info("timed campaign %s queue empty, %s contacts awaiting "
                         "verification — retrying next tick", r["id"], pending)
                continue
            await pool.execute("update campaigns set status='paused' where id=$1", r["id"])
            log.error("timed campaign %s paused: audience empty after drop rules", r["id"])
            if slack is not None and r["channel"]:
                await slack.post_message(
                    r["channel"],
                    text=f"⏸️ *{r['name']}* paused before launch: none of its "
                         "enrolled contacts are sendable (invalid/risky verdicts, "
                         "suppressed, or DND). Fix the audience and resume, or "
                         "cancel the campaign.",
                    thread_ts=r["thread_ts"])
            continue
        log.info("timed campaign %s queued %s sends", r["id"], queued)
        if slack is not None and r["channel"]:
            first = await pool.fetchval(
                "select min(next_attempt_at) from sends where campaign_id=$1", r["id"])
            limits = " · ".join(filter(None, [
                f"{r['per_day']}/day" if r["per_day"] else None,
                f"{r['per_hour']}/hour" if r["per_hour"] else None])) or "no ramp limits"
            await slack.post_message(
                r["channel"],
                text=f"<!channel> 🚀 *{r['name']}* is LAUNCHED — {queued:,} verified "
                     f"recipients queued ({limits}), each timed to ~10am local. "
                     f"First wave: {first:%Y-%m-%d %H:%M} UTC. Daily progress lands "
                     "in the morning digest.",
                thread_ts=r["thread_ts"])


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


def slugify(name: str) -> str:
    """Kebab slug for a campaign. Kept identical to the engagement-tag slug in
    webhooks.py so utm_campaign matches the opened-/clicked- tags for the same
    campaign — one campaign, one slug across analytics and CRM."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


UTM_STATIC = {"utm_source": "newsletter", "utm_medium": "email"}


def add_utm_params(html: str, campaign_slug: str, skip_host: str) -> str:
    """Tag external http(s) links with UTM params for site-side attribution.

    Runs on the final HTML (after unsub substitution), so it deliberately skips:
    our own tracked links (same host as public_base_url — unsubscribe/preferences),
    non-http links (mailto/tel/#anchors), and links already carrying a utm_source
    (never clobber a hand-tagged URL). Existing query params and #fragments are
    preserved. Complements Resend click tracking: the redirect lands on this
    tagged URL, so GA still sees the campaign."""
    def rewrite(match: re.Match) -> str:
        url = match.group(1)
        if not re.match(r"https?://", url, re.I):
            return match.group(0)
        parts = urllib.parse.urlsplit(url)
        if skip_host and parts.netloc == skip_host:
            return match.group(0)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if "utm_source" in query:
            return match.group(0)
        query.update({k: [v] for k, v in UTM_STATIC.items()})
        query["utm_campaign"] = [campaign_slug]
        new_query = urllib.parse.urlencode(query, doseq=True)
        new_url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
        return f'href="{new_url}"'

    return re.sub(r'href="([^"]+)"', rewrite, html)


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


def render_outbound(content: dict, unsub_url: str,
                    postal_address: str = "") -> Rendered:
    """Plain-text outbound email (Xovera engine) -> minimal HTML + text parts.
    These are one-to-one cold emails, so the styling stays deliberately bare.
    CAN-SPAM: the physical postal address renders in the footer when set."""
    text = (content.get("text_body") or "").strip()
    if not text:
        raise RenderError("outbound send has empty text_body")
    paras = "".join(
        f'<p style="margin:0 0 1em">{html_lib.escape(p).replace(chr(10), "<br>")}</p>'
        for p in re.split(r"\n{2,}", text)
    )
    addr_html = (f'{html_lib.escape(postal_address)}<br>' if postal_address else "")
    html = (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;color:#1c1917;line-height:1.6;max-width:600px">{paras}'
        f'<p style="color:#a8a29e;font-size:12px">&mdash;<br>{addr_html}'
        f'<a href="{unsub_url}" style="color:#a8a29e">Unsubscribe</a></p></body></html>'
    )
    addr_text = f"{postal_address}\n" if postal_address else ""
    return Rendered(html=html, text=f"{text}\n\n{addr_text}Unsubscribe: {unsub_url}",
                    hash=hashlib.sha256(html.encode()).hexdigest())


def build_headers(settings: Settings, unsub_url: str) -> dict:
    return {
        "List-Unsubscribe": f"<mailto:unsubscribe@{settings.from_domain}>, <{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


async def _claim_batch(pool, campaign_id, limit: int) -> list[dict]:
    # Last-gate verification check (like the suppression re-check): a queued row
    # without a CURRENT valid verdict is never claimed, no matter how it got into
    # the queue — revived stale queues, pre-verification enrollments, addresses
    # that bounced since queueing. The queue's history can't override this.
    rows = await pool.fetch(
        """update sends set status='sending', next_attempt_at=now()
           where id in (
               select s.id from sends s
               where s.campaign_id = $2
                 and s.status='queued' and s.next_attempt_at <= now()
                 and exists (select 1 from email_verifications v
                             where v.email = s.email and v.verdict = 'valid')
               order by s.next_attempt_at, s.created_at
               limit $1
               for update of s skip locked)
           returning id, campaign_id, ghl_contact_id, email, retry_count,
                     subject_override, content_override""",
        limit, campaign_id,
    )
    return [dict(r) for r in rows]


async def _roll_missed_windows(pool, settings: Settings) -> int:
    """Timed sends that missed their local window (cap exhausted, downtime) get
    rescheduled to the next day's ideal hour instead of going out at 2am local."""
    rows = await pool.fetch(
        """select id, timezone from sends
           where status='queued' and timezone <> ''
             and next_attempt_at < now() - make_interval(hours => $1)""",
        WINDOW_HOURS,
    )
    now = datetime.now(timezone.utc)
    for r in rows:
        await pool.execute(
            "update sends set next_attempt_at=$2 where id=$1",
            r["id"], next_ideal_time(r["timezone"], now, settings.ideal_send_hour))
    return len(rows)


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
    """One worker pass. Returns number of emails successfully handed to Resend.

    Campaigns with their own per_day/per_hour ramp are throttled individually;
    everything else shares the global daily cap. Broadcast sends never touch
    this queue."""
    await _roll_missed_windows(pool, settings)

    campaigns = await pool.fetch(
        """select c.id, c.per_day, c.per_hour from campaigns c
           where c.status='dispatching'
             and exists (select 1 from sends s where s.campaign_id = c.id
                         and s.status='queued' and s.next_attempt_at <= now())""")

    # shared daily cap for campaigns without their own ramp (drip/enroll path)
    shared_sent = await pool.fetchval(
        """select count(*) from sends s join campaigns c on c.id = s.campaign_id
           where s.sent_at >= date_trunc('day', now()) and s.via = 'queue'
             and c.per_day is null""")
    shared_remaining = settings.daily_send_cap - shared_sent

    total_sent = 0
    for campaign in campaigns:
        if campaign["per_day"] is not None:
            sent_today = await pool.fetchval(
                "select count(*) from sends where campaign_id=$1 "
                "and sent_at >= date_trunc('day', now())", campaign["id"])
            remaining = campaign["per_day"] - sent_today
        else:
            if shared_remaining <= 0:
                log.info("daily cap %s reached; shared-queue dispatch resumes tomorrow",
                         settings.daily_send_cap)
                continue
            remaining = shared_remaining
        if campaign["per_hour"] is not None:
            sent_hour = await pool.fetchval(
                "select count(*) from sends where campaign_id=$1 "
                "and sent_at >= date_trunc('hour', now())", campaign["id"])
            remaining = min(remaining, campaign["per_hour"] - sent_hour)
        limit = min(BATCH_SIZE, remaining)
        if limit <= 0:
            continue
        claimed = await _claim_batch(pool, campaign["id"], limit)
        if not claimed:
            continue
        sent = await _dispatch_claimed(pool, settings, resend, campaign["id"], claimed)
        if campaign["per_day"] is None:
            shared_remaining -= sent
        total_sent += sent

    # Close out campaigns whose queues drained (only ones that were ever queued —
    # broadcast campaigns and not-yet-enqueued timed campaigns have no send rows)
    await pool.execute(
        """update campaigns set status='completed'
           where status='dispatching'
             and send_via <> 'broadcast'
             and exists (select 1 from sends where campaign_id = campaigns.id)
             and not exists (select 1 from sends
                             where campaign_id = campaigns.id
                               and status in ('queued', 'sending'))""")
    return total_sent


async def _dispatch_claimed(pool, settings: Settings, resend: ResendClient,
                            campaign_id, claimed: list[dict]) -> int:
    """Render and send one campaign's claimed batch. Returns emails handed off."""
    # Dispatch-time suppression re-check (spec §5)
    suppressed = await suppressed_subset(pool, [s["email"] for s in claimed])
    sends = []
    for send in claimed:
        if send["email"] in suppressed:
            await pool.execute(
                "update sends set status='suppressed' where id=$1", send["id"])
        else:
            sends.append(send)
    if not sends:
        return 0

    # Per-send personalized emails (outbound engine) render individually with
    # domain rotation and the CAN-SPAM postal footer; campaign-template sends
    # render per campaign below.
    overrides = [s for s in sends if s["content_override"]]
    sends = [s for s in sends if not s["content_override"]]

    override_sent = 0
    for send in overrides:
        unsub = _unsub_url(settings, send["email"], send["campaign_id"])
        try:
            r = render_outbound(json.loads(send["content_override"]), unsub,
                                settings.postal_address)
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
        override_sent += 1

    if not sends:
        return override_sent

    campaign = await pool.fetchrow(
        "select name, subject, template_ref, content from campaigns where id=$1", campaign_id)
    content = json.loads(campaign["content"])
    campaign_slug = slugify(campaign["name"])
    skip_host = urllib.parse.urlsplit(settings.public_base_url).netloc
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
        return 0

    sent_count = 0
    for send, unsub, r in zip(sends, unsub_urls, rendered):
        payload = {
            "from": settings.from_email,
            "to": [send["email"]],
            "subject": campaign["subject"],
            "html": add_utm_params(r.html, campaign_slug, skip_host),
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
    return sent_count + override_sent
