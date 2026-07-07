"""Background worker: run as `python -m app.worker` (separate Render service)."""
import asyncio
import logging

from app.config import get_settings
from app.db import create_pool
from app.services.dispatch import process_send_queue, requeue_stale
from app.services.ghl import GHLClient
from app.services.guardrails import check_and_pause
from app.services.resend_client import ResendClient
from app.services.writeback import process_writeback_jobs

log = logging.getLogger("worker")

TICK_SECONDS = 5


async def run_forever() -> None:
    settings = get_settings()
    pool = await create_pool(settings.database_url)
    ghl = GHLClient(settings.ghl_pi_token, settings.ghl_location_id)
    resend = ResendClient(settings.resend_api_key, rps=settings.send_rps)
    log.info("worker up: cap=%s rps=%s", settings.daily_send_cap, settings.send_rps)
    while True:
        try:
            await requeue_stale(pool)
            breached = await check_and_pause(pool, settings.alert_webhook_url)
            await process_writeback_jobs(pool, ghl)
            if not breached:
                sent = await process_send_queue(pool, settings, resend)
                if sent:
                    log.info("dispatched %s emails", sent)
        except Exception:
            log.exception("worker tick failed")
        await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_forever())
