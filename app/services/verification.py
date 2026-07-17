"""Verdict-cache orchestration (spec: docs/superpowers/specs/2026-07-18-list-cleaning-design.md).
Fail-safe by construction: an email with no fresh 'valid' verdict is excluded
from every send path, so verification errors can only under-send, never over-send."""
import logging

from app.config import Settings
from app.services.jobs import complete_job, enqueue, fail_job, fetch_job
from app.services.suppressions import normalize
from app.services.verify_client import map_result

log = logging.getLogger(__name__)

BATCH_CHUNK = 1000       # emails per Emailable batch
POLL_DELAY_SECONDS = 30
VERDICT_TAGS = {"invalid": "email-invalid", "risky": "email-risky",
                "unknown": "email-risky"}

_UNVERIFIED_SQL = """
    select distinct c.email
    from campaign_contacts cc
    join contacts_cache c using (ghl_contact_id)
    where cc.campaign_id = $1
      and not exists (select 1 from email_verifications v
                      where v.email = c.email
                        and v.verified_at > now() - make_interval(days => $2))
    order by c.email
"""


async def unverified_emails(pool, campaign_id, ttl_days: int) -> list[str]:
    """Audience emails with no verdict, or only a stale one (any verdict re-verifies
    after TTL — a 91-day-old 'valid' is as untrustworthy as a missing one)."""
    return [r["email"] for r in await pool.fetch(_UNVERIFIED_SQL, campaign_id, ttl_days)]


async def unverified_count(pool, campaign_id, ttl_days: int) -> int:
    return len(await unverified_emails(pool, campaign_id, ttl_days))


async def upsert_verdicts(pool, results: list[tuple], provider: str = "emailable") -> None:
    """results: [(email, verdict, reason)]. Last write wins; verified_at refreshes."""
    await pool.executemany(
        """insert into email_verifications (email, verdict, reason, provider)
           values ($1, $2, $3, $4)
           on conflict (email) do update set verdict=excluded.verdict,
               reason=excluded.reason, provider=excluded.provider, verified_at=now()""",
        [(normalize(e), v, r, provider) for e, v, r in results])


async def request_verification(pool, settings: Settings, campaign_id) -> dict:
    """Kick off verification for a campaign's unverified audience. Auto-submits at or
    under the approval threshold; above it, the caller must post an approve button
    (spend gate) and the button handler enqueues verify_submit."""
    count = await unverified_count(pool, campaign_id, settings.verdict_ttl_days)
    if count == 0:
        return {"status": "verified"}
    est_cost = round(count * settings.verify_cost_per_email, 2)
    if count > settings.verify_approval_threshold:
        return {"status": "needs_approval", "unverified": count, "est_cost": est_cost}
    await enqueue(pool, "verify_submit", {"campaign_id": str(campaign_id)})
    return {"status": "submitted", "unverified": count, "est_cost": est_cost}


async def verification_summary(pool, campaign_id, ttl_days: int) -> dict:
    """Fresh-verdict counts for the campaign audience + how many still lack one."""
    rows = await pool.fetch(
        """select v.verdict, count(distinct c.email) as n
           from campaign_contacts cc
           join contacts_cache c using (ghl_contact_id)
           join email_verifications v on v.email = c.email
                and v.verified_at > now() - make_interval(days => $2)
           where cc.campaign_id = $1
           group by v.verdict""", campaign_id, ttl_days)
    summary = {r["verdict"]: r["n"] for r in rows}
    summary["unverified"] = await unverified_count(pool, campaign_id, ttl_days)
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


async def process_verification_jobs(pool, settings: Settings, client,
                                    backoff_seconds: int = 60) -> int:
    """One worker pass: drain verify_submit and verify_poll jobs. A still-processing
    provider batch re-enqueues its poll (completing the old job, so retry_limit only
    counts real failures, not long batches)."""
    done = 0
    while (job := await fetch_job(pool, "verify_submit")) is not None:
        try:
            emails = await unverified_emails(
                pool, job["data"]["campaign_id"], settings.verdict_ttl_days)
            for chunk in _chunks(emails, BATCH_CHUNK):
                batch_id = await client.create_batch(chunk)
                await enqueue(pool, "verify_poll", {"batch_id": batch_id},
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
                await complete_job(pool, job["id"])
                await enqueue(pool, "verify_poll", job["data"],
                              start_after_seconds=POLL_DELAY_SECONDS)
                continue
            results = [(r["email"], *map_result(r)) for r in raw]
            await upsert_verdicts(pool, results)
            await _enqueue_verdict_tags(pool, results)
        except Exception:
            log.exception("verify_poll job %s failed", job["id"])
            await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
