import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.weekly_review import maybe_start_weekly_review
from tests.helpers import make_settings

SYD = ZoneInfo("Australia/Sydney")


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return "777.001"


async def test_fires_monday_after_nine_once(pool):
    slack = FakeSlack()
    monday_10am = datetime(2026, 7, 20, 10, 0, tzinfo=SYD)  # Monday
    assert await maybe_start_weekly_review(pool, slack, make_settings(),
                                           now=monday_10am) is True
    assert "Weekly marketing review" in slack.posts[0]["text"]
    job = await pool.fetchrow("select data from jobs where name='bot_turn'")
    data = json.loads(job["data"])
    assert data["thread_ts"] == "777.001" and data["channel"] == "C0TEST"
    assert "weekly marketing review" in data["text"]
    # same day again: claimed, no repeat
    assert await maybe_start_weekly_review(pool, slack, make_settings(),
                                           now=monday_10am) is False
    assert len(slack.posts) == 1


async def test_does_not_fire_early_or_wrong_day(pool):
    slack = FakeSlack()
    monday_8am = datetime(2026, 7, 20, 8, 0, tzinfo=SYD)
    tuesday_10am = datetime(2026, 7, 21, 10, 0, tzinfo=SYD)
    assert await maybe_start_weekly_review(pool, slack, make_settings(),
                                           now=monday_8am) is False
    assert await maybe_start_weekly_review(pool, slack, make_settings(),
                                           now=tuesday_10am) is False
    assert slack.posts == []


async def test_disabled_or_no_slack(pool):
    settings = make_settings(weekly_review_enabled=False)
    monday = datetime(2026, 7, 20, 10, 0, tzinfo=SYD)
    assert await maybe_start_weekly_review(pool, FakeSlack(), settings,
                                           now=monday) is False
    assert await maybe_start_weekly_review(pool, None, make_settings(),
                                           now=monday) is False
