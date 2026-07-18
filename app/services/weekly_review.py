"""Monday-morning marketing-manager kickoff: the worker opens a review thread
and hands the bot a planning brief, so campaign ideas arrive without a human
asking (spec: docs/superpowers/specs/2026-07-19-weekly-marketing-agent-design.md)."""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.jobs import enqueue

log = logging.getLogger(__name__)

KICKOFF_TEXT = ("📋 *Weekly marketing review* — digging through campaign "
                "performance, CRM activity and meeting notes. Plan incoming.")

REVIEW_BRIEF = (
    "It is Monday — run your weekly marketing review. Pull campaign_history, "
    "tag_stats, engagement_segments, sales_activity and recent_meetings, post a "
    "short analysis (what worked, what's decaying, what prospects are asking "
    "about, sendable audience sizes), then propose 1-2 campaigns: draft them "
    "fully, run seed tests, and finish with propose_send and a one-line 'why "
    "this, why now' for each. Max 2 campaigns; skip audiences emailed in the "
    "last 7 days unless engagement data justifies it.")


async def maybe_start_weekly_review(pool, slack, settings: Settings,
                                    now: datetime | None = None) -> bool:
    """Called every worker tick. Fires once per week, on the configured local
    day-of-week at/after the configured hour. Claim via the daily_reports table
    (same idempotency pattern as the daily digest)."""
    if slack is None or not settings.weekly_review_enabled:
        return False
    if not settings.slack_channel_id:
        return False
    now_local = (now or datetime.now(timezone.utc)).astimezone(
        ZoneInfo(settings.bot_timezone))
    if now_local.weekday() != settings.weekly_review_dow:
        return False
    if now_local.hour < settings.weekly_review_hour:
        return False
    claimed = await pool.fetchval(
        """insert into daily_reports (report_type, last_sent_date) values ($1, $2)
           on conflict (report_type) do update set last_sent_date=$2
           where daily_reports.last_sent_date < $2
           returning report_type""",
        "weekly_review", now_local.date())
    if not claimed:
        return False
    thread_ts = await slack.post_message(settings.slack_channel_id,
                                         text=KICKOFF_TEXT)
    await enqueue(pool, "bot_turn", {
        "channel": settings.slack_channel_id, "thread_ts": thread_ts,
        "user": "weekly-review", "text": REVIEW_BRIEF})
    log.info("weekly marketing review kicked off (thread %s)", thread_ts)
    return True
