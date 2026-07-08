"""Daily ops digest posted to each Slack channel — a routine summary, not an alert,
so unlike notify.py it does not @channel-ping."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Settings

WINDOW = timedelta(hours=24)


async def build_email_stats(pool) -> dict:
    since = datetime.now(timezone.utc) - WINDOW
    sends = await pool.fetchrow(
        "select count(*) filter (where status='sent' and sent_at >= $1) as sent, "
        "count(*) filter (where status='failed' and created_at >= $1) as failed "
        "from sends", since)
    events = await pool.fetch(
        "select type, count(distinct send_id) as n from events "
        "where occurred_at >= $1 group by type", since)
    event_counts = {r["type"]: r["n"] for r in events}
    active = await pool.fetchval(
        "select count(*) from campaigns where status in ('dispatching', 'scheduled')")
    completed = await pool.fetchval(
        "select count(*) from campaigns where status='completed' and created_at >= $1", since)
    paused = await pool.fetch("select name from campaigns where status='paused'")
    return {
        "sent": sends["sent"] or 0, "failed": sends["failed"] or 0,
        "delivered": event_counts.get("email.delivered", 0),
        "opened": event_counts.get("email.opened", 0),
        "clicked": event_counts.get("email.clicked", 0),
        "bounced": event_counts.get("email.bounced", 0),
        "complained": event_counts.get("email.complained", 0),
        "active_campaigns": active or 0, "completed_campaigns": completed or 0,
        "paused_campaigns": [r["name"] for r in paused],
    }


async def build_social_stats(pool) -> dict:
    since = datetime.now(timezone.utc) - WINDOW
    upcoming_cutoff = datetime.now(timezone.utc) + WINDOW
    published = await pool.fetchval(
        "select count(*) from social_posts where status='published' and created_at >= $1",
        since)
    went_live_scheduled = await pool.fetchval(
        "select count(*) from social_posts where status='scheduled' "
        "and schedule_at >= $1 and schedule_at <= now()", since)
    cancelled = await pool.fetchval(
        "select count(*) from social_posts where status='cancelled' and created_at >= $1",
        since)
    upcoming = await pool.fetch(
        "select content, schedule_at from social_posts where status='scheduled' "
        "and schedule_at > now() and schedule_at <= $1 order by schedule_at",
        upcoming_cutoff)
    return {
        "published": published or 0, "went_live_scheduled": went_live_scheduled or 0,
        "cancelled": cancelled or 0,
        "upcoming": [{"text": json.loads(r["content"])["text"][:80],
                     "when": r["schedule_at"].isoformat()} for r in upcoming],
    }


def format_email_report(stats: dict) -> str:
    lines = [
        "*📊 Daily email report (last 24h)*",
        f"Sent: *{stats['sent']}*  ·  Delivered: *{stats['delivered']}*  ·  "
        f"Opened: *{stats['opened']}*  ·  Clicked: *{stats['clicked']}*",
        f"Bounced: *{stats['bounced']}*  ·  Complained: *{stats['complained']}*  ·  "
        f"Failed: *{stats['failed']}*",
        f"Active campaigns: *{stats['active_campaigns']}*  ·  "
        f"Completed today: *{stats['completed_campaigns']}*",
    ]
    if stats["paused_campaigns"]:
        lines.append(f"⚠️ *Paused by guardrails:* {', '.join(stats['paused_campaigns'])} "
                     "— needs a look.")
    return "\n".join(lines)


def format_social_report(stats: dict) -> str:
    lines = [
        "*📊 Daily social report (last 24h)*",
        f"Published: *{stats['published']}*  ·  Went live (scheduled): "
        f"*{stats['went_live_scheduled']}*  ·  Cancelled: *{stats['cancelled']}*",
    ]
    if stats["upcoming"]:
        lines.append("*Upcoming in the next 24h:*")
        for u in stats["upcoming"]:
            lines.append(f"• {u['when']} — {u['text']}")
    else:
        lines.append("Nothing scheduled in the next 24h.")
    return "\n".join(lines)


REPORTS = [
    ("email", "slack_channel_id", build_email_stats, format_email_report),
    ("social", "slack_social_channel_id", build_social_stats, format_social_report),
]


async def maybe_post_daily_reports(pool, slack, settings: Settings,
                                   now: datetime | None = None) -> None:
    """Called every worker tick. Posts each configured channel's digest once per
    local day, after `daily_report_hour`. `now` is injectable for tests."""
    if slack is None:
        return
    now_local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(settings.bot_timezone))
    if now_local.hour < settings.daily_report_hour:
        return
    today = now_local.date()

    for report_type, channel_attr, build, fmt in REPORTS:
        channel = getattr(settings, channel_attr)
        if not channel:
            continue
        claimed = await pool.fetchval(
            """insert into daily_reports (report_type, last_sent_date) values ($1, $2)
               on conflict (report_type) do update set last_sent_date=$2
               where daily_reports.last_sent_date < $2
               returning report_type""",
            report_type, today)
        if not claimed:
            continue
        stats = await build(pool)
        await slack.post_message(channel, text=fmt(stats))
