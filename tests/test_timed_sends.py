import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import respx

from app.services.dispatch import (ensure_timed_queues, enqueue_timed_sends,
                                   process_send_queue)
from app.services.resend_client import ResendClient
from app.services.sendtime import next_ideal_time, resolve_timezone
from tests.helpers import make_settings, verify_all_contacts

RESEND_API = "https://api.resend.com/emails"

HTML_BODY = ("<!DOCTYPE html><html><body><p>Hi {{first_name}},</p>"
             "<a href='{{unsubscribe_url}}'>Unsubscribe</a> · Woonona"
             "</body></html>")


# --- timezone resolution -----------------------------------------------------

def test_explicit_timezone_wins():
    assert resolve_timezone("AU", "America/New_York") == "America/New_York"


def test_invalid_timezone_falls_back_to_country():
    assert resolve_timezone("AU", "Not/AZone") == "Australia/Sydney"


def test_country_code_and_full_name():
    assert resolve_timezone("GB", "") == "Europe/London"
    assert resolve_timezone("United Kingdom", "") == "Europe/London"


def test_no_country_assumes_us():
    assert resolve_timezone("", "") == "America/Chicago"
    assert resolve_timezone("XX", "") == "America/Chicago"  # unmapped country


def test_next_ideal_time_before_and_after_hour():
    tz = "Australia/Sydney"
    # 08:00 Sydney → today 10:00 Sydney
    after = datetime(2026, 7, 16, 8, 0, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    ideal = next_ideal_time(tz, after, 10)
    assert ideal.astimezone(ZoneInfo(tz)).hour == 10
    assert ideal - after == timedelta(hours=2)
    # 11:00 Sydney → tomorrow 10:00 Sydney
    after = datetime(2026, 7, 16, 11, 0, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    ideal = next_ideal_time(tz, after, 10)
    local = ideal.astimezone(ZoneInfo(tz))
    assert local.hour == 10 and local.day == 17


# --- timed enqueue -----------------------------------------------------------

async def seed_timed_campaign(pool, per_day=None, per_hour=None, status="dispatching"):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status, "
        "send_via, per_day, per_hour, content) "
        "values ('July', 'Big', 'custom', 'v1', $1, 'timed', $2, $3, $4) returning id",
        status, per_day, per_hour, json.dumps({"html_body": HTML_BODY}))
    return cid


async def add_contact(pool, cid, i, country="", tz="", suppressed=False):
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email, country, timezone) "
        "values ($1, $2, $3, $4)", f"c{i}", f"u{i}@x.co", country, tz)
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
        cid, f"c{i}")
    if suppressed:
        await pool.execute(
            "insert into suppressions (email, reason, source) values ($1, 'complaint', 'resend')",
            f"u{i}@x.co")
    await verify_all_contacts(pool)


async def test_enqueue_timed_schedules_per_contact_timezone(pool):
    cid = await seed_timed_campaign(pool)
    await add_contact(pool, cid, 0, country="AU")
    await add_contact(pool, cid, 1, tz="Europe/London")
    await add_contact(pool, cid, 2)                      # no data → US
    await add_contact(pool, cid, 3, suppressed=True)     # dropped
    queued = await enqueue_timed_sends(pool, make_settings(), cid)
    assert queued == 3
    rows = await pool.fetch(
        "select email, timezone, next_attempt_at from sends order by email")
    zones = {r["email"]: r["timezone"] for r in rows}
    assert zones == {"u0@x.co": "Australia/Sydney", "u1@x.co": "Europe/London",
                     "u2@x.co": "America/Chicago"}
    for r in rows:
        local = r["next_attempt_at"].astimezone(ZoneInfo(r["timezone"]))
        assert local.hour == 10 and local.minute == 0


async def test_ensure_timed_queues_fills_once_and_pauses_empty(pool):
    cid = await seed_timed_campaign(pool)
    await add_contact(pool, cid, 0)
    empty = await seed_timed_campaign(pool)
    await ensure_timed_queues(pool, make_settings())
    assert (await pool.fetchval(
        "select count(*) from sends where campaign_id=$1", cid)) == 1
    assert (await pool.fetchval(
        "select status from campaigns where id=$1", empty)) == "paused"
    # idempotent: second pass adds nothing
    await ensure_timed_queues(pool, make_settings())
    assert (await pool.fetchval("select count(*) from sends")) == 1


# --- ramp caps ---------------------------------------------------------------

async def make_due(pool, cid):
    await pool.execute(
        "update sends set next_attempt_at=now() where campaign_id=$1", cid)


@respx.mock
async def test_per_hour_cap_limits_each_pass(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em"}))
    cid = await seed_timed_campaign(pool, per_hour=2)
    for i in range(5):
        await add_contact(pool, cid, i)
    await enqueue_timed_sends(pool, make_settings(), cid)
    await make_due(pool, cid)
    resend = ResendClient("re", rps=10_000, backoff_base=0)
    assert await process_send_queue(pool, make_settings(), resend) == 2
    assert await process_send_queue(pool, make_settings(), resend) == 0  # hour exhausted
    assert (await pool.fetchval(
        "select count(*) from sends where status='queued'")) == 3


@respx.mock
async def test_per_day_cap_ignores_global_cap(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em"}))
    cid = await seed_timed_campaign(pool, per_day=3)
    for i in range(5):
        await add_contact(pool, cid, i)
    await enqueue_timed_sends(pool, make_settings(), cid)
    await make_due(pool, cid)
    # global cap of 1 must NOT throttle a campaign with its own ramp
    settings = make_settings(daily_send_cap=1)
    resend = ResendClient("re", rps=10_000, backoff_base=0)
    assert await process_send_queue(pool, settings, resend) == 3
    assert await process_send_queue(pool, settings, resend) == 0  # per_day exhausted
    assert (await pool.fetchval(
        "select status from campaigns where id=$1", cid)) == "dispatching"  # not completed


@respx.mock
async def test_sends_not_due_yet_wait(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em"}))
    cid = await seed_timed_campaign(pool)
    await add_contact(pool, cid, 0)
    await enqueue_timed_sends(pool, make_settings(), cid)
    await pool.execute(
        "update sends set next_attempt_at=now() + interval '2 hours' where campaign_id=$1",
        cid)
    resend = ResendClient("re", rps=10_000, backoff_base=0)
    assert await process_send_queue(pool, make_settings(), resend) == 0
    # still queued, campaign not swept to completed
    assert (await pool.fetchval("select status from sends")) == "queued"
    assert (await pool.fetchval(
        "select status from campaigns where id=$1", cid)) == "dispatching"


@respx.mock
async def test_missed_window_rolls_to_next_day(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em"}))
    cid = await seed_timed_campaign(pool)
    await add_contact(pool, cid, 0, tz="Australia/Sydney")
    await enqueue_timed_sends(pool, make_settings(), cid)
    # simulate a send that sat past its window (cap exhaustion / downtime)
    await pool.execute(
        "update sends set next_attempt_at=now() - interval '9 hours' where campaign_id=$1",
        cid)
    resend = ResendClient("re", rps=10_000, backoff_base=0)
    sent = await process_send_queue(pool, make_settings(), resend)
    assert sent == 0  # rolled forward instead of sent at a bad local hour
    row = await pool.fetchrow("select status, next_attempt_at, timezone from sends")
    assert row["status"] == "queued"
    local = row["next_attempt_at"].astimezone(ZoneInfo(row["timezone"]))
    assert local.hour == 10
    assert row["next_attempt_at"] > datetime.now(timezone.utc)
