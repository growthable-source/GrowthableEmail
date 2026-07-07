import httpx
import respx

from app.services.dispatch import enqueue_campaign_sends, process_send_queue, requeue_stale
from app.services.resend_client import ResendClient
from app.services.suppressions import add_suppression
from tests.helpers import make_settings

RESEND_API = "https://api.resend.com/emails"


async def seed_campaign(pool, n_contacts=3, status="ready"):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('launch', 'Big Launch', 'welcome', 'v1', $1) returning id", status)
    for i in range(n_contacts):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email, first_name) "
            "values ($1, $2, $3)", f"c{i}", f"user{i}@x.co", f"User{i}")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
            cid, f"c{i}")
    return cid


async def test_enqueue_is_idempotent_and_skips_suppressed(pool):
    cid = await seed_campaign(pool)
    await add_suppression(pool, "user1@x.co", reason="complaint", source="resend")
    assert await enqueue_campaign_sends(pool, cid) == 2
    assert await enqueue_campaign_sends(pool, cid) == 0  # rerun inserts nothing
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "dispatching"


@respx.mock
async def test_process_sends_updates_rows_and_sets_headers(pool):
    route = respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    cid = await seed_campaign(pool, n_contacts=2)
    await enqueue_campaign_sends(pool, cid)
    settings = make_settings()
    sent = await process_send_queue(pool, settings, ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 2
    rows = await pool.fetch("select status, resend_email_id, rendered_hash, sent_at from sends")
    assert all(r["status"] == "sent" and r["resend_email_id"] == "em_1"
               and r["rendered_hash"] and r["sent_at"] for r in rows)
    body = route.calls[0].request.read().decode()
    assert "List-Unsubscribe" in body and "List-Unsubscribe=One-Click" in body
    assert "/u/" in body  # signed unsub URL made it into html + headers
    # queue drained → campaign completed
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "completed"


@respx.mock
async def test_daily_cap_limits_batch(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    cid = await seed_campaign(pool, n_contacts=3)
    await enqueue_campaign_sends(pool, cid)
    settings = make_settings(daily_send_cap=2)
    resend = ResendClient("re_test", rps=10_000, backoff_base=0)
    assert await process_send_queue(pool, settings, resend) == 2
    assert await process_send_queue(pool, settings, resend) == 0  # cap hit, resumes next day
    assert (await pool.fetchval("select count(*) from sends where status='queued'")) == 1


@respx.mock
async def test_suppression_rechecked_at_dispatch(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    cid = await seed_campaign(pool, n_contacts=2)
    await enqueue_campaign_sends(pool, cid)
    await add_suppression(pool, "user0@x.co", reason="unsubscribe", source="unsub_page")
    sent = await process_send_queue(pool, make_settings(),
                                    ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 1
    assert (await pool.fetchval(
        "select status from sends where email='user0@x.co'")) == "suppressed"


@respx.mock
async def test_transient_failure_requeues_with_backoff_then_fails(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(500))
    cid = await seed_campaign(pool, n_contacts=1)
    await enqueue_campaign_sends(pool, cid)
    settings = make_settings()
    resend = ResendClient("re_test", rps=10_000, backoff_base=0)
    await process_send_queue(pool, settings, resend)
    row = await pool.fetchrow("select status, retry_count, next_attempt_at > now() as delayed from sends")
    assert row["status"] == "queued" and row["retry_count"] == 1 and row["delayed"]
    # force due and exhaust remaining retries (MAX_SEND_RETRIES=3: rc 1→2 requeues, rc 2→3 fails,
    # once failed the claim query no longer picks it up)
    for expected in ("queued", "failed", "failed"):
        await pool.execute("update sends set next_attempt_at = now() where status='queued'")
        await process_send_queue(pool, settings, resend)
        assert (await pool.fetchval("select status from sends")) == expected


async def test_requeue_stale_recovers_crashed_sends(pool):
    cid = await seed_campaign(pool, n_contacts=1, status="dispatching")
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email, status, created_at) "
        "values ($1, 'c0', 'user0@x.co', 'sending', now() - interval '20 minutes')", cid)
    await pool.execute("update sends set next_attempt_at = now() - interval '20 minutes'")
    assert await requeue_stale(pool) == 1
    assert (await pool.fetchval("select status from sends")) == "queued"
