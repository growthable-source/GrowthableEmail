import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.daily_report import (build_email_stats, build_social_stats,
                                       format_email_report, format_social_report,
                                       maybe_post_daily_reports)
from tests.helpers import make_settings

SYD = ZoneInfo("Australia/Sydney")


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text})
        return "1"


async def test_build_email_stats_aggregates_across_campaigns(pool):
    c1 = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('A', 's', 'newsletter', 'v1', 'completed') returning id")
    c2 = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('B', 's', 'newsletter', 'v1', 'paused') returning id")
    s1 = await pool.fetchval(
        "insert into sends (campaign_id, ghl_contact_id, email, status, sent_at) "
        "values ($1, 'c1', 'a@x.co', 'sent', now()) returning id", c1)
    await pool.fetchval(
        "insert into sends (campaign_id, ghl_contact_id, email, status, sent_at) "
        "values ($1, 'c2', 'b@x.co', 'sent', now()) returning id", c1)
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email, status, created_at) "
        "values ($1, 'c3', 'c@x.co', 'failed', now())", c1)
    await pool.execute(
        "insert into events (send_id, type) values ($1, 'email.delivered')", s1)
    await pool.execute(
        "insert into events (send_id, type) values ($1, 'email.opened')", s1)
    await pool.execute(
        "insert into events (send_id, type) values ($1, 'email.bounced')", s1)
    # outside the 24h window — must not count
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email, status, sent_at) "
        "values ($1, 'old', 'old@x.co', 'sent', now() - interval '3 days')", c1)

    stats = await build_email_stats(pool)
    assert stats["sent"] == 2 and stats["failed"] == 1
    assert stats["delivered"] == 1 and stats["opened"] == 1 and stats["bounced"] == 1
    assert stats["clicked"] == 0 and stats["complained"] == 0
    assert stats["completed_campaigns"] == 1
    assert stats["paused_campaigns"] == ["B"]


async def test_build_social_stats_counts_by_status_and_upcoming(pool):
    await pool.execute(
        "insert into social_posts (account_ids, content, status, created_at) "
        "values (array['a'], $1, 'published', now())", json.dumps({"text": "x"}))
    await pool.execute(
        "insert into social_posts (account_ids, content, status, schedule_at) "
        "values (array['a'], $1, 'scheduled', now() - interval '1 hour')",
        json.dumps({"text": "went live"}))
    await pool.execute(
        "insert into social_posts (account_ids, content, status, created_at) "
        "values (array['a'], $1, 'cancelled', now())", json.dumps({"text": "x"}))
    await pool.execute(
        "insert into social_posts (account_ids, content, status, schedule_at) "
        "values (array['a'], $1, 'scheduled', now() + interval '2 hours')",
        json.dumps({"text": "upcoming post"}))
    # too far out — must not appear in upcoming
    await pool.execute(
        "insert into social_posts (account_ids, content, status, schedule_at) "
        "values (array['a'], $1, 'scheduled', now() + interval '3 days')",
        json.dumps({"text": "far future"}))

    stats = await build_social_stats(pool)
    assert stats["published"] == 1 and stats["went_live_scheduled"] == 1
    assert stats["cancelled"] == 1
    assert [u["text"] for u in stats["upcoming"]] == ["upcoming post"]


def test_format_email_report_flags_paused():
    text = format_email_report({
        "sent": 10, "failed": 0, "delivered": 9, "opened": 4, "clicked": 1,
        "bounced": 0, "complained": 0, "active_campaigns": 1, "completed_campaigns": 1,
        "paused_campaigns": ["July Launch"],
    })
    assert "Sent: *10*" in text
    assert "Paused by guardrails:* July Launch" in text


def test_format_social_report_no_upcoming():
    text = format_social_report({
        "published": 2, "went_live_scheduled": 0, "cancelled": 0, "upcoming": [],
    })
    assert "Nothing scheduled" in text


async def test_maybe_post_fires_once_per_day_per_channel(pool):
    slack = FakeSlack()
    settings = make_settings(daily_report_hour=8)
    morning = datetime(2026, 7, 9, 9, 0, tzinfo=SYD)
    await maybe_post_daily_reports(pool, slack, settings, now=morning)
    assert {p["channel"] for p in slack.posts} == {"C0TEST", "C0SOCIAL"}
    assert any("Daily email report" in p["text"] for p in slack.posts)
    assert any("Daily social report" in p["text"] for p in slack.posts)

    # same day, called again (e.g. next tick) — no duplicate posts
    await maybe_post_daily_reports(pool, slack, settings, now=morning)
    assert len(slack.posts) == 2

    # next day — fires again
    tomorrow = datetime(2026, 7, 10, 9, 0, tzinfo=SYD)
    await maybe_post_daily_reports(pool, slack, settings, now=tomorrow)
    assert len(slack.posts) == 4


async def test_maybe_post_skips_before_report_hour(pool):
    slack = FakeSlack()
    settings = make_settings(daily_report_hour=8)
    early = datetime(2026, 7, 9, 5, 0, tzinfo=SYD)
    await maybe_post_daily_reports(pool, slack, settings, now=early)
    assert slack.posts == []


async def test_maybe_post_noop_without_slack(pool):
    settings = make_settings(daily_report_hour=8)
    morning = datetime(2026, 7, 9, 9, 0, tzinfo=SYD)
    await maybe_post_daily_reports(pool, None, settings, now=morning)  # must not raise


async def test_maybe_post_skips_unconfigured_channel(pool):
    slack = FakeSlack()
    settings = make_settings(daily_report_hour=8, slack_social_channel_id="")
    morning = datetime(2026, 7, 9, 9, 0, tzinfo=SYD)
    await maybe_post_daily_reports(pool, slack, settings, now=morning)
    assert {p["channel"] for p in slack.posts} == {"C0TEST"}
