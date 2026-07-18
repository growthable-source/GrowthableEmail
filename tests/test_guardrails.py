import httpx
import respx

from app.services.guardrails import check_and_pause


async def seed_day(pool, sent: int, bounced: int, complained: int):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('x', 's', 'welcome', 'v1', 'dispatching') returning id")
    for i in range(sent):
        send_id = await pool.fetchval(
            "insert into sends (campaign_id, ghl_contact_id, email, status, sent_at) "
            "values ($1, $2, $3, 'sent', now()) returning id", cid, f"c{i}", f"u{i}@x.co")
        if i < bounced:
            await pool.execute(
                "insert into events (send_id, type) values ($1, 'email.bounced')", send_id)
        elif i < bounced + complained:
            await pool.execute(
                "insert into events (send_id, type) values ($1, 'email.complained')", send_id)
    return cid


async def test_below_thresholds_no_pause(pool):
    cid = await seed_day(pool, sent=1000, bounced=10, complained=0)  # 1% bounce
    assert await check_and_pause(pool) is False
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "dispatching"


async def test_bounce_breach_pauses(pool):
    cid = await seed_day(pool, sent=1000, bounced=40, complained=0)  # 4% > 3%
    assert await check_and_pause(pool) is True
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "paused"


async def test_complaint_breach_pauses_and_alerts(pool):
    await seed_day(pool, sent=1000, bounced=0, complained=2)  # 0.2% > 0.1%
    with respx.mock:
        alert = respx.post("https://hooks.example.com/alert").mock(
            return_value=httpx.Response(200))
        assert await check_and_pause(pool, alert_webhook_url="https://hooks.example.com/alert") is True
        assert alert.called
        assert b"complaint" in alert.calls[0].request.read()


async def test_low_volume_days_never_trip(pool):
    await seed_day(pool, sent=10, bounced=5, complained=2)  # tiny sample
    assert await check_and_pause(pool) is False


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text})
        return "1.1"


async def test_breach_alerts_slack_channel_with_campaign_names(pool):
    await seed_day(pool, sent=1000, bounced=40, complained=0)
    slack = FakeSlack()
    assert await check_and_pause(pool, slack=slack, channel="C0TEST") is True
    assert len(slack.posts) == 1
    text = slack.posts[0]["text"]
    assert slack.posts[0]["channel"] == "C0TEST"
    assert "<!channel>" in text and "x" in text        # pings + names the campaign
    assert "bounce" in text.lower()


async def test_breach_alerts_only_once_not_every_tick(pool):
    await seed_day(pool, sent=1000, bounced=40, complained=0)
    slack = FakeSlack()
    assert await check_and_pause(pool, slack=slack, channel="C0TEST") is True
    # next worker tick: still breached (rates persist all day) but nothing newly paused
    assert await check_and_pause(pool, slack=slack, channel="C0TEST") is True
    assert len(slack.posts) == 1


async def test_webhook_not_spammed_on_repeat_ticks(pool):
    await seed_day(pool, sent=1000, bounced=40, complained=0)
    with respx.mock:
        alert = respx.post("https://hooks.example.com/alert").mock(
            return_value=httpx.Response(200))
        await check_and_pause(pool, alert_webhook_url="https://hooks.example.com/alert")
        await check_and_pause(pool, alert_webhook_url="https://hooks.example.com/alert")
        assert alert.call_count == 1


# --- circuit breaker ---------------------------------------------------------

from app.services.guardrails import (ensure_auto_resume,  # noqa: E402
                                     process_auto_resume)
from app.services.verification import upsert_verdicts  # noqa: E402


class BreakerSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return "1.1"


async def seed_paused_timed(pool, queued_emails=("ok@x.co",), per_hour=600):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status, "
        "send_via, per_hour, channel, thread_ts) values ('Ramp', 's', 'custom', 'v1', "
        "'paused', 'timed', $1, 'C0TEST', '100.1') returning id", per_hour)
    for i, email in enumerate(queued_emails):
        await pool.execute(
            "insert into sends (campaign_id, ghl_contact_id, email, status) "
            "values ($1, $2, $3, 'queued')", cid, f"c{i}", email)
    return cid


async def test_breaker_schedules_resume_for_paused_timed_only(pool):
    cid = await seed_paused_timed(pool)
    # paused broadcast: must NOT get auto-resumed (stale segment risk)
    await pool.execute(
        "insert into campaigns (name, subject, template_ref, template_version, status, "
        "send_via) values ('BC', 's', 'custom', 'v1', 'paused', 'broadcast')")
    # paused timed with EMPTY queue: nothing to resume for
    await pool.execute(
        "insert into campaigns (name, subject, template_ref, template_version, status, "
        "send_via) values ('Empty', 's', 'custom', 'v1', 'paused', 'timed')")
    assert await ensure_auto_resume(pool) == 1
    job = await pool.fetchrow(
        "select data, start_after > now() + interval '1 minute' as delayed "
        "from jobs where name='auto_resume'")
    import json as _j
    assert _j.loads(job["data"]) == {"campaign_id": str(cid), "attempt": 1}
    assert job["delayed"]  # waits for the daily counter reset
    # reconciler is idempotent while a job is pending
    assert await ensure_auto_resume(pool) == 0


async def test_breaker_resume_prunes_halves_and_announces(pool):
    cid = await seed_paused_timed(pool, queued_emails=("ok@x.co", "bad@x.co"),
                                  per_hour=600)
    await upsert_verdicts(pool, [("ok@x.co", "valid", "ok")])  # bad@x.co unverified
    await ensure_auto_resume(pool)
    await pool.execute("update jobs set start_after=now() where name='auto_resume'")
    slack = BreakerSlack()
    assert await process_auto_resume(pool, slack) == 1
    row = await pool.fetchrow(
        "select status, per_hour from campaigns where id=$1", cid)
    assert row["status"] == "dispatching" and row["per_hour"] == 300
    states = {r["email"]: r["status"] for r in await pool.fetch(
        "select email, status from sends")}
    assert states == {"ok@x.co": "queued", "bad@x.co": "suppressed"}
    assert len(slack.posts) == 1
    text = slack.posts[0]["text"]
    assert "auto-resumed" in text and "300/hour" in text and "attempt 1/3" in text


async def test_breaker_caps_attempts(pool):
    cid = await seed_paused_timed(pool)
    for _ in range(3):  # three completed attempts already on record
        await pool.execute(
            "insert into jobs (name, data, state, completed_at) values "
            "('auto_resume', $1, 'completed', now())",
            f'{{"campaign_id": "{cid}"}}')
    assert await ensure_auto_resume(pool) == 0  # human required now


async def test_breaker_skips_if_already_resumed(pool):
    cid = await seed_paused_timed(pool)
    await ensure_auto_resume(pool)
    await pool.execute("update campaigns set status='completed' where id=$1", cid)
    await pool.execute("update jobs set start_after=now() where name='auto_resume'")
    slack = BreakerSlack()
    await process_auto_resume(pool, slack)
    assert slack.posts == []  # nothing announced, job completed quietly
    assert (await pool.fetchval(
        "select status from campaigns where id=$1", cid)) == "completed"
