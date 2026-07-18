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
