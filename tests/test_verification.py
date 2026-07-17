import json
import uuid

from app.services.verification import (process_verification_jobs,
                                       request_verification, unverified_count,
                                       unverified_emails, upsert_verdicts,
                                       verification_summary)
from tests.helpers import make_settings


class FakeVerifyClient:
    def __init__(self, results=None):
        self.batches, self.results = {}, results or {}
        self._n = 0

    async def create_batch(self, emails):
        self._n += 1
        bid = f"batch_{self._n}"
        self.batches[bid] = list(emails)
        return bid

    async def get_batch(self, batch_id):
        return [{"email": e, **self.results.get(e, {"state": "deliverable",
                 "reason": "accepted_email"})} for e in self.batches[batch_id]]


async def seed_campaign(pool, emails):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, "
        "audience_filter, content) values ('camp', 'subj', 'custom', 'v1', '[]', $1) "
        "returning id", json.dumps({"html_body": "x {{unsubscribe_url}}"}))
    for email in emails:
        gid = f"g{uuid.uuid4().hex[:8]}"
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email, dnd, synced_at) "
            "values ($1, $2, false, now())", gid, email)
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
            cid, gid)
    return cid


async def test_unverified_count_ignores_fresh_valid(pool):
    cid = await seed_campaign(pool, ["a@x.com", "b@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "accepted_email")])
    assert await unverified_count(pool, cid, 90) == 1
    assert await unverified_emails(pool, cid, 90) == ["b@x.com"]


async def test_stale_verdict_counts_as_unverified(pool):
    cid = await seed_campaign(pool, ["a@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok")])
    await pool.execute(
        "update email_verifications set verified_at = now() - interval '91 days'")
    assert await unverified_count(pool, cid, 90) == 1


async def test_upsert_overwrites(pool):
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok")])
    await upsert_verdicts(pool, [("a@x.com", "invalid", "bounced")])
    row = await pool.fetchrow("select verdict, reason from email_verifications")
    assert (row["verdict"], row["reason"]) == ("invalid", "bounced")


async def test_request_small_audience_auto_submits(pool):
    cid = await seed_campaign(pool, ["a@x.com"])
    settings = make_settings()
    result = await request_verification(pool, settings, cid)
    assert result["status"] == "submitted"
    job = await pool.fetchrow("select data from jobs where name='verify_submit'")
    assert json.loads(job["data"])["campaign_id"] == str(cid)


async def test_request_large_audience_needs_approval(pool):
    cid = await seed_campaign(pool, [f"u{i}@x.com" for i in range(3)])
    settings = make_settings(verify_approval_threshold=2)
    result = await request_verification(pool, settings, cid)
    assert result["status"] == "needs_approval"
    assert result["unverified"] == 3
    assert await pool.fetchval("select count(*) from jobs where name='verify_submit'") == 0


async def test_request_fully_verified(pool):
    cid = await seed_campaign(pool, ["a@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok")])
    result = await request_verification(pool, make_settings(), cid)
    assert result["status"] == "verified"


async def test_verification_summary(pool):
    cid = await seed_campaign(pool, ["a@x.com", "b@x.com", "c@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok"),
                                 ("b@x.com", "invalid", "rejected_email")])
    summary = await verification_summary(pool, cid, 90)
    assert summary == {"valid": 1, "invalid": 1, "unverified": 1}


async def drain_verification(pool, settings, client, passes=5):
    for _ in range(passes):
        # poll jobs are enqueued with a delay — simulate the wait elapsing
        await pool.execute("update jobs set start_after=now() "
                           "where name in ('verify_submit', 'verify_poll') and state='created'")
        await process_verification_jobs(pool, settings, client)


async def test_pipeline_verifies_and_tags(pool):
    cid = await seed_campaign(pool, ["good@x.com", "dead@x.com", "role@x.com"])
    client = FakeVerifyClient(results={
        "dead@x.com": {"state": "undeliverable", "reason": "rejected_email"},
        "role@x.com": {"state": "deliverable", "reason": "accepted_email", "role": True},
    })
    settings = make_settings()
    await request_verification(pool, settings, cid)
    await drain_verification(pool, settings, client)

    rows = {r["email"]: (r["verdict"], r["reason"]) for r in await pool.fetch(
        "select email, verdict, reason from email_verifications")}
    assert rows["good@x.com"] == ("valid", "accepted_email")
    assert rows["dead@x.com"] == ("invalid", "rejected_email")
    assert rows["role@x.com"] == ("risky", "role")

    tag_jobs = [json.loads(r["data"]) for r in await pool.fetch(
        "select data from jobs where name='ghl_writeback' and state='created'")]
    tags = {(j["contact_id"], t) for j in tag_jobs for t in j["tags"]}
    dead_gid = await pool.fetchval(
        "select ghl_contact_id from contacts_cache where email='dead@x.com'")
    role_gid = await pool.fetchval(
        "select ghl_contact_id from contacts_cache where email='role@x.com'")
    assert (dead_gid, "email-invalid") in tags
    assert (role_gid, "email-risky") in tags
    assert await unverified_count(pool, cid, 90) == 0


async def test_pending_batch_reenqueues_poll(pool):
    class PendingClient(FakeVerifyClient):
        async def get_batch(self, batch_id):
            return None  # still processing
    cid = await seed_campaign(pool, ["a@x.com"])
    settings = make_settings()
    await request_verification(pool, settings, cid)
    client = PendingClient()
    await drain_verification(pool, settings, client, passes=2)  # submit, then pending poll
    # a fresh poll job exists (delayed), nothing verified yet
    assert await pool.fetchval(
        "select count(*) from jobs where name='verify_poll' and state='created'") == 1
    assert await pool.fetchval("select count(*) from email_verifications") == 0


async def test_queue_path_excludes_unverified_and_nonvalid(pool):
    from app.services.dispatch import enqueue_campaign_sends
    cid = await seed_campaign(pool, ["good@x.com", "risky@x.com", "novote@x.com"])
    await upsert_verdicts(pool, [("good@x.com", "valid", "ok"),
                                 ("risky@x.com", "risky", "role")])
    queued = await enqueue_campaign_sends(pool, make_settings(), cid)
    assert queued == 1
    assert await pool.fetchval("select email from sends") == "good@x.com"


async def test_timed_path_excludes_unverified(pool):
    from app.services.dispatch import enqueue_timed_sends
    cid = await seed_campaign(pool, ["good@x.com", "novote@x.com"])
    await upsert_verdicts(pool, [("good@x.com", "valid", "ok")])
    queued = await enqueue_timed_sends(pool, make_settings(), cid)
    assert queued == 1


async def test_broadcast_audience_excludes_unverified(pool):
    from app.services.broadcast import _audience_csv
    cid = await seed_campaign(pool, ["good@x.com", "novote@x.com"])
    await upsert_verdicts(pool, [("good@x.com", "valid", "ok")])
    csv_bytes, count = await _audience_csv(pool, cid, 90)
    assert count == 1 and b"good@x.com" in csv_bytes and b"novote" not in csv_bytes


async def test_provider_error_retries_job(pool):
    class BrokenClient(FakeVerifyClient):
        async def create_batch(self, emails):
            raise RuntimeError("api down")
    cid = await seed_campaign(pool, ["a@x.com"])
    settings = make_settings()
    await request_verification(pool, settings, cid)
    await process_verification_jobs(pool, settings, BrokenClient())
    job = await pool.fetchrow(
        "select state, retry_count from jobs where name='verify_submit'")
    assert job["state"] == "created" and job["retry_count"] == 1
