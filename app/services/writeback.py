import asyncio
import logging

from app.services.jobs import complete_job, fail_job, fetch_job

log = logging.getLogger(__name__)

MAX_JOBS_PER_PASS = 200
CONCURRENCY = 10  # parallel GHL calls; stays inside GHL's ~100 req/10s limit


async def _run_one(pool, ghl, job, backoff_seconds: int) -> int:
    data = job["data"]
    try:
        kind = data["kind"]
        if kind == "add_tags":
            await ghl.add_tags(data["contact_id"], data["tags"])
        elif kind == "set_dnd":
            await ghl.set_dnd_email(data["contact_id"])
        else:
            raise ValueError(f"unknown writeback kind: {kind}")
    except Exception:
        log.exception("writeback job %s failed", job["id"])
        await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
        return 0
    await complete_job(pool, job["id"])
    return 1


async def process_writeback_jobs(pool, ghl, backoff_seconds: int = 60) -> int:
    """Drain up to MAX_JOBS_PER_PASS ghl_writeback jobs, CONCURRENCY at a time.
    Sequential per-contact calls (~2s each cross-region) were ~25/min — a 25k-tag
    verification sweep would take 17h; parallel chunks bring it to well under an
    hour. Independent of dispatch — a GHL outage retries jobs here and never
    blocks webhook ingestion (spec §6)."""
    done = 0
    for _ in range(MAX_JOBS_PER_PASS // CONCURRENCY):
        batch = []
        for _ in range(CONCURRENCY):
            job = await fetch_job(pool, "ghl_writeback")
            if job is None:
                break
            batch.append(job)
        if not batch:
            break
        results = await asyncio.gather(
            *(_run_one(pool, ghl, job, backoff_seconds) for job in batch))
        done += sum(results)
    return done
