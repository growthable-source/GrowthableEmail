import logging

from app.services.jobs import complete_job, fail_job, fetch_job

log = logging.getLogger(__name__)

MAX_JOBS_PER_PASS = 50


async def process_writeback_jobs(pool, ghl, backoff_seconds: int = 60) -> int:
    """Drain up to MAX_JOBS_PER_PASS ghl_writeback jobs. Independent of dispatch —
    a GHL outage retries jobs here and never blocks webhook ingestion (spec §6)."""
    done = 0
    for _ in range(MAX_JOBS_PER_PASS):
        job = await fetch_job(pool, "ghl_writeback")
        if job is None:
            break
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
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
