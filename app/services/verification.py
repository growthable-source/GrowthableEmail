"""Verdict-cache orchestration (spec: docs/superpowers/specs/2026-07-18-list-cleaning-design.md).
Fail-safe by construction: an email without a 'valid' verdict is excluded from
every send path, so verification errors can only under-send, never over-send.

Verdicts are PERMANENT — each email is billed to the provider at most once, ever
(Ryan's rule, 2026-07-18). List decay is handled for free: hard bounces overwrite
'valid' with 'invalid' via the Resend webhook, and the guardrail kill rule backstops
anything that slips through. To force a re-check, delete the row."""
import logging

from app.config import Settings
from app.services.jobs import complete_job, enqueue, fail_job, fetch_job
from app.services.suppressions import normalize
from app.services.verify_client import map_result

log = logging.getLogger(__name__)

BATCH_CHUNK = 1000       # emails per Emailable batch
POLL_DELAY_SECONDS = 30
MAX_POLL_ATTEMPTS = 240  # ~2h at 30s: a batch stalled this long (e.g. credits ran
                         # out mid-run) is abandoned so it can't jam the pipeline
VERDICT_TAGS = {"invalid": "email-invalid", "risky": "email-risky",
                "unknown": "email-risky"}

_UNVERIFIED_SQL = """
    select distinct c.email
    from campaign_contacts cc
    join contacts_cache c using (ghl_contact_id)
    where cc.campaign_id = $1
      and not exists (select 1 from email_verifications v where v.email = c.email)
    order by c.email
"""


async def unverified_emails(pool, campaign_id) -> list[str]:
    """Audience emails we have never verified. Any existing verdict — however old —
    means the email is never submitted to the provider again."""
    return [r["email"] for r in await pool.fetch(_UNVERIFIED_SQL, campaign_id)]


async def unverified_count(pool, campaign_id) -> int:
    return len(await unverified_emails(pool, campaign_id))


async def upsert_verdicts(pool, results: list[tuple], provider: str = "emailable") -> None:
    """results: [(email, verdict, reason)]. A provider result never downgrades an
    existing 'valid' (a duplicate/greylisted second probe must not destroy a
    paid-for verdict); real-world bounce feedback (provider='resend') always wins."""
    await pool.executemany(
        """insert into email_verifications (email, verdict, reason, provider)
           values ($1, $2, $3, $4)
           on conflict (email) do update set verdict=excluded.verdict,
               reason=excluded.reason, provider=excluded.provider, verified_at=now()
           where excluded.provider = 'resend'
              or excluded.verdict = 'valid'
              or email_verifications.verdict <> 'valid'""",
        [(normalize(e), v, r, provider) for e, v, r in results])


async def request_verification(pool, settings: Settings, campaign_id) -> dict:
    """Kick off verification for a campaign's unverified audience. Auto-submits at or
    under the approval threshold; above it, the caller must post an approve button
    (spend gate) and the button handler enqueues verify_submit."""
    count = await unverified_count(pool, campaign_id)
    if count == 0:
        return {"status": "verified"}
    est_cost = round(count * settings.verify_cost_per_email, 2)
    if count > settings.verify_approval_threshold:
        return {"status": "needs_approval", "unverified": count, "est_cost": est_cost}
    await enqueue(pool, "verify_submit", {"campaign_id": str(campaign_id)})
    return {"status": "submitted", "unverified": count, "est_cost": est_cost}


async def verification_summary(pool, campaign_id) -> dict:
    """Verdict counts for the campaign audience + how many still lack one."""
    rows = await pool.fetch(
        """select v.verdict, count(distinct c.email) as n
           from campaign_contacts cc
           join contacts_cache c using (ghl_contact_id)
           join email_verifications v on v.email = c.email
           where cc.campaign_id = $1
           group by v.verdict""", campaign_id)
    summary = {r["verdict"]: r["n"] for r in rows}
    summary["unverified"] = await unverified_count(pool, campaign_id)
    return summary


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _enqueue_verdict_tags(pool, results: list[tuple]) -> None:
    """Tag invalid/risky contacts in GHL via the existing write-back queue.
    Advisory only — Postgres is the enforcement point; failures retry there."""
    flagged = {normalize(e): VERDICT_TAGS[v] for e, v, _ in results if v in VERDICT_TAGS}
    if not flagged:
        return
    rows = await pool.fetch(
        "select ghl_contact_id, email from contacts_cache where email = any($1::text[])",
        list(flagged))
    for r in rows:
        await enqueue(pool, "ghl_writeback", {
            "kind": "add_tags", "contact_id": r["ghl_contact_id"],
            "tags": [flagged[r["email"]]]})


async def _notify_progress(pool, slack, campaign_id, before_remaining: int,
                           after_remaining: int) -> None:
    """Proactive progress in the campaign thread: milestone posts at each 25%
    crossing, and a completion post with the verdict breakdown (Ryan's rule:
    transparent progress, no babysitting)."""
    campaign = await pool.fetchrow(
        "select name, channel, thread_ts from campaigns where id=$1::uuid", campaign_id)
    if campaign is None or not campaign["channel"]:
        return
    total = await pool.fetchval(
        """select count(distinct c.email) from campaign_contacts cc
           join contacts_cache c using (ghl_contact_id) where cc.campaign_id=$1::uuid""",
        campaign_id)
    if not total:
        return
    if after_remaining == 0:
        if before_remaining == 0:
            return  # late/duplicate batch — completion was already announced
        s = await verification_summary(pool, campaign_id)
        valid = s.get("valid", 0)
        invalid = s.get("invalid", 0)
        risky = s.get("risky", 0) + s.get("unknown", 0)
        await slack.post_message(
            campaign["channel"],
            text=f"✅ *{campaign['name']}* audience verification finished — "
                 f"{valid} valid, {invalid} invalid, {risky} risky. "
                 f"{invalid + risky} bad addresses will be skipped automatically. "
                 "Say 'resume this campaign' and I'll get it moving.",
            thread_ts=campaign["thread_ts"])
        return
    done_before, done_after = total - before_remaining, total - after_remaining
    if (done_after * 4) // total > (done_before * 4) // total:  # crossed a 25% line
        pct = done_after * 100 // total
        await slack.post_message(
            campaign["channel"],
            text=f"⏳ *{campaign['name']}* verification {pct}% — "
                 f"{done_after:,}/{total:,} checked.",
            thread_ts=campaign["thread_ts"])


_warned_unconfigured = False


async def warn_missing_verifier(pool, slack) -> None:
    """Worker has no Emailable key but verification jobs are waiting — say so in the
    campaign thread instead of silently skipping (once per worker process). Without
    this, an approved verification just never happens and nobody is told."""
    global _warned_unconfigured
    if slack is None or _warned_unconfigured:
        return
    rows = await pool.fetch(
        """select distinct c.channel, c.thread_ts from jobs j
           join campaigns c on c.id = (j.data->>'campaign_id')::uuid
           where j.name in ('verify_submit', 'verify_poll') and j.state = 'created'
             and c.channel is not null""")
    if not rows:
        return
    for r in rows:
        await slack.post_message(
            r["channel"],
            text="⚠️ Verification is approved but I CANNOT run it: "
                 "`EMAILABLE_API_KEY` is not set on the *worker* service "
                 "(growthable-email-worker on Render). Add it there and the queued "
                 "verification starts automatically — no need to re-approve.",
            thread_ts=r["thread_ts"])
    _warned_unconfigured = True
    log.error("verification jobs pending but EMAILABLE_API_KEY is not configured")


async def process_verification_jobs(pool, settings: Settings, client, slack=None,
                                    backoff_seconds: int = 60) -> int:
    """One worker pass: drain verify_submit and verify_poll jobs. A still-processing
    provider batch re-enqueues its poll (completing the old job, so retry_limit only
    counts real failures, not long batches)."""
    done = 0
    while (job := await fetch_job(pool, "verify_submit")) is not None:
        # In-flight guard: while ANY batches are still out with the provider, defer —
        # their emails have no verdict rows yet, so submitting now would re-bill the
        # same addresses (double Verify clicks, overlapping campaigns).
        in_flight = await pool.fetchval(
            "select count(*) from jobs where name='verify_poll' "
            "and state in ('created', 'active')")
        if in_flight:
            await complete_job(pool, job["id"])
            await enqueue(pool, "verify_submit", job["data"],
                          start_after_seconds=POLL_DELAY_SECONDS * 2)
            continue
        try:
            emails = await unverified_emails(pool, job["data"]["campaign_id"])
            for chunk in _chunks(emails, BATCH_CHUNK):
                batch_id = await client.create_batch(chunk)
                await enqueue(pool, "verify_poll",
                              {"batch_id": batch_id,
                               "campaign_id": job["data"]["campaign_id"]},
                              start_after_seconds=POLL_DELAY_SECONDS)
        except Exception:
            log.exception("verify_submit job %s failed", job["id"])
            await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
            continue
        await complete_job(pool, job["id"])
        done += 1

    while (job := await fetch_job(pool, "verify_poll")) is not None:
        try:
            raw = await client.get_batch(job["data"]["batch_id"])
            if raw is None:  # still processing — poll again later
                attempts = job["data"].get("attempts", 0) + 1
                await complete_job(pool, job["id"])
                if attempts >= MAX_POLL_ATTEMPTS:
                    log.error("verify batch %s never completed after %s polls — "
                              "abandoned", job["data"]["batch_id"], attempts)
                    continue
                await enqueue(pool, "verify_poll", {**job["data"], "attempts": attempts},
                              start_after_seconds=POLL_DELAY_SECONDS)
                continue
            results = [(r["email"], *map_result(r)) for r in raw]
            campaign_id = job["data"].get("campaign_id")
            notify = slack is not None and campaign_id is not None
            before = await unverified_count(pool, campaign_id) if notify else 0
            await upsert_verdicts(pool, results)
            await _enqueue_verdict_tags(pool, results)
            if notify:
                after = await unverified_count(pool, campaign_id)
                await _notify_progress(pool, slack, campaign_id, before, after)
        except Exception:
            log.exception("verify_poll job %s failed", job["id"])
            await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
