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
