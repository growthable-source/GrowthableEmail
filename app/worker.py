"""Background worker: run as `python -m app.worker` (separate Render service)."""
import asyncio
import logging
import time

from app.config import get_settings
from app.db import create_pool
from app.services.bot import BotEngine
from app.services.bot_base import process_bot_turns
from app.services.broadcast import process_broadcast_campaigns
from app.services.social_bot import SocialBot
from app.services.daily_report import maybe_post_daily_reports
from app.services.dispatch import (ensure_timed_queues, process_send_queue,
                                   promote_scheduled, requeue_stale)
from app.services.domains import adjust_and_guard
from app.services.ghl import GHLClient
from app.services.inbound import process_reply_jobs
from app.services.guardrails import (check_and_pause, ensure_auto_resume,
                                     process_auto_resume)
from app.services.jobs import requeue_stale_jobs
from app.services.notify import notify_campaign_going_out, notify_post_going_out
from app.services.resend_client import ResendClient
from app.services.slack_client import SlackClient
from app.services.social_dispatch import notify_due_social_posts
from app.services.resonance import ResonanceClient
from app.services.verification import (process_verification_jobs,
                                       warn_missing_verifier)
from app.services.verify_client import EmailableClient
from app.services.weekly_review import maybe_start_weekly_review
from app.services.writeback import process_writeback_jobs

log = logging.getLogger("worker")

TICK_SECONDS = 5


async def run_forever() -> None:
    settings = get_settings()
    pool = await create_pool(settings.database_url)
    ghl = GHLClient(settings.ghl_pi_token, settings.ghl_location_id)
    resend = ResendClient(settings.resend_api_key, rps=settings.send_rps)
    verifier = (EmailableClient(settings.emailable_api_key)
                if settings.emailable_api_key else None)
    resonance = (ResonanceClient(settings.resonance_api_url, settings.resonance_api_key)
                 if settings.resonance_api_key and settings.resonance_api_url else None)
    slack = SlackClient(settings.slack_bot_token) if settings.slack_enabled else None
    engines: dict = {}
    if slack is not None:
        if settings.slack_channel_id:
            engines[settings.slack_channel_id] = BotEngine(
                pool=pool, settings=settings, ghl=ghl, slack=slack, resend=resend,
                resonance=resonance)
        if settings.slack_social_channel_id:
            engines[settings.slack_social_channel_id] = SocialBot(
                pool=pool, settings=settings, ghl=ghl, slack=slack)
    log.info("worker up: cap=%s rps=%s slack=%s channels=%s", settings.daily_send_cap,
             settings.send_rps, settings.slack_enabled, list(engines))
    import time
    last_domain_check = 0.0
    while True:
        try:
            await requeue_stale(pool)
            await requeue_stale_jobs(pool)
            await process_reply_jobs(pool, settings)
            if time.monotonic() - last_domain_check > 3600:
                await adjust_and_guard(pool, settings)
                last_domain_check = time.monotonic()
            promoted = await promote_scheduled(pool)
            due_posts = await notify_due_social_posts(pool)
            if slack is not None:
                for campaign_id in promoted:
                    await notify_campaign_going_out(pool, slack, campaign_id)
                for post_id in due_posts:
                    await notify_post_going_out(pool, slack, post_id)
            breached = await check_and_pause(pool, settings.alert_webhook_url,
                                             slack=slack,
                                             channel=settings.slack_channel_id)
            # circuit breaker: paused campaigns schedule their own comeback and
            # resume unattended once the daily counters reset
            await ensure_auto_resume(pool)
            if not breached:
                await process_auto_resume(pool, slack)
            # humans first: a deep writeback/verification backlog must never leave
            # a Slack message unanswered for half an hour (2026-07-18 incident)
            if engines:
                await process_bot_turns(pool, engines)
            if verifier is not None:
                await process_verification_jobs(pool, settings, verifier, slack=slack)
            else:
                await warn_missing_verifier(pool, slack)
            await maybe_post_daily_reports(pool, slack, settings)
            await maybe_start_weekly_review(pool, slack, settings)
            await process_writeback_jobs(pool, ghl)
            if not breached:
                broadcasts = await process_broadcast_campaigns(pool, settings, resend, slack)
                if broadcasts:
                    log.info("created %s broadcasts", broadcasts)
                await ensure_timed_queues(pool, settings, slack)
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
