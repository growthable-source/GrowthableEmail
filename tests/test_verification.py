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


async def test_unverified_count_ignores_known_emails(pool):
    cid = await seed_campaign(pool, ["a@x.com", "b@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "accepted_email")])
    assert await unverified_count(pool, cid) == 1
    assert await unverified_emails(pool, cid) == ["b@x.com"]


async def test_verdicts_are_permanent_never_reverified(pool):
    """Ryan's rule: once verified, never pay to check the same email again."""
    cid = await seed_campaign(pool, ["a@x.com", "b@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok"),
                                 ("b@x.com", "invalid", "rejected_email")])
    await pool.execute(
        "update email_verifications set verified_at = now() - interval '2 years'")
    assert await unverified_count(pool, cid) == 0
    assert (await request_verification(pool, make_settings(), cid))["status"] == "verified"


async def test_old_valid_verdict_still_sends(pool):
    from app.services.dispatch import enqueue_campaign_sends
    cid = await seed_campaign(pool, ["a@x.com"])
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok")])
    await pool.execute(
        "update email_verifications set verified_at = now() - interval '2 years'")
    assert await enqueue_campaign_sends(pool, cid) == 1


async def test_upsert_bounce_overwrites_valid(pool):
    await upsert_verdicts(pool, [("a@x.com", "valid", "ok")])
    await upsert_verdicts(pool, [("a@x.com", "invalid", "bounced")], provider="resend")
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
    summary = await verification_summary(pool, cid)
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
    assert await unverified_count(pool, cid) == 0


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
    queued = await enqueue_campaign_sends(pool, cid)
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
    csv_bytes, count = await _audience_csv(pool, cid)
    assert count == 1 and b"good@x.com" in csv_bytes and b"novote" not in csv_bytes


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return "1.1"


async def test_completion_posted_to_campaign_thread(pool):
    cid = await seed_campaign(pool, ["good@x.com", "dead@x.com"])
    await pool.execute(
        "update campaigns set channel='C0TEST', thread_ts='100.1' where id=$1", cid)
    client = FakeVerifyClient(results={
        "dead@x.com": {"state": "undeliverable", "reason": "rejected_email"}})
    settings, slack = make_settings(), FakeSlack()
    await request_verification(pool, settings, cid)
    for _ in range(5):
        await pool.execute("update jobs set start_after=now() "
                           "where name in ('verify_submit', 'verify_poll') and state='created'")
        await process_verification_jobs(pool, settings, client, slack=slack)
    done = [p for p in slack.posts if "✅" in p["text"]]
    assert len(done) == 1  # exactly one completion post
    assert done[0]["channel"] == "C0TEST" and done[0]["thread_ts"] == "100.1"
    assert "1 valid" in done[0]["text"] and "1 invalid" in done[0]["text"]


async def test_progress_milestones_posted(pool, monkeypatch):
    from app.services import verification
    monkeypatch.setattr(verification, "BATCH_CHUNK", 1)  # 4 emails -> 4 batches
    cid = await seed_campaign(pool, [f"u{i}@x.com" for i in range(4)])
    await pool.execute(
        "update campaigns set channel='C0TEST', thread_ts='100.1' where id=$1", cid)
    settings, slack, client = make_settings(), FakeSlack(), FakeVerifyClient()
    await request_verification(pool, settings, cid)
    for _ in range(8):
        await pool.execute("update jobs set start_after=now() "
                           "where name in ('verify_submit', 'verify_poll') and state='created'")
        await process_verification_jobs(pool, settings, client, slack=slack)
    progress = [p["text"] for p in slack.posts if "⏳" in p["text"]]
    assert any("25%" in t for t in progress)
    assert any("50%" in t for t in progress)
    assert any("75%" in t for t in progress)
    assert sum("✅" in p["text"] for p in slack.posts) == 1  # and one completion


async def test_no_slack_no_posts_pipeline_still_works(pool):
    # regression: default slack=None path (also covers API-only campaigns)
    cid = await seed_campaign(pool, ["a@x.com"])
    settings, client = make_settings(), FakeVerifyClient()
    await request_verification(pool, settings, cid)
    await drain_verification(pool, settings, client)
    assert await unverified_count(pool, cid) == 0


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


async def test_missing_verifier_warns_once_in_campaign_thread(pool):
    from app.services import verification
    verification._warned_unconfigured = False
    cid = await seed_campaign(pool, ["a@x.com"])
    await pool.execute(
        "update campaigns set channel='C0TEST', thread_ts='100.1' where id=$1", cid)
    await request_verification(pool, make_settings(), cid)  # queues verify_submit
    slack = FakeSlack()
    await verification.warn_missing_verifier(pool, slack)
    await verification.warn_missing_verifier(pool, slack)  # second tick: no repeat
    warnings = [p for p in slack.posts if "EMAILABLE_API_KEY" in p["text"]]
    assert len(warnings) == 1
    assert warnings[0]["channel"] == "C0TEST" and warnings[0]["thread_ts"] == "100.1"
    verification._warned_unconfigured = False


async def test_submit_defers_while_batches_in_flight(pool):
    """Double Verify click / overlapping campaigns must never re-bill the same
    emails: a submit waits until no batches are out with the provider."""
    cid_a = await seed_campaign(pool, ["a@x.com"])
    cid_b = await seed_campaign(pool, ["a@x.com", "b@x.com"])  # overlapping audience
    settings, client = make_settings(), FakeVerifyClient()
    await request_verification(pool, settings, cid_a)
    await request_verification(pool, settings, cid_b)
    # pass 1: first submit creates a batch; second submit must defer, not submit
    await pool.execute("update jobs set start_after=now() "
                       "where name in ('verify_submit', 'verify_poll') and state='created'")
    await process_verification_jobs(pool, settings, client)
    assert len(client.batches) == 1  # only campaign A's batch went out
    # drain: poll completes, verdicts land, deferred submit runs on the remainder
    await drain_verification(pool, settings, client)
    all_emails = [e for b in client.batches.values() for e in b]
    assert sorted(all_emails) == ["a@x.com", "b@x.com"]  # a@x.com billed exactly once
    assert await unverified_count(pool, cid_b) == 0


async def test_stalled_batch_abandoned_after_max_polls(pool):
    from app.services import verification

    class PendingClient(FakeVerifyClient):
        async def get_batch(self, batch_id):
            return None
    cid = await seed_campaign(pool, ["a@x.com"])
    settings = make_settings()
    await request_verification(pool, settings, cid)
    client = PendingClient()
    await drain_verification(pool, settings, client, passes=1)  # submit -> poll
    # fast-forward: mark the pending poll as one attempt from the cap
    await pool.execute(
        "update jobs set data = jsonb_set(data, '{attempts}', to_jsonb($1::int)) "
        "where name='verify_poll' and state='created'",
        verification.MAX_POLL_ATTEMPTS - 1)
    await drain_verification(pool, settings, client, passes=1)
    # abandoned: no created/active poll jobs remain to jam the in-flight guard
    assert await pool.fetchval(
        "select count(*) from jobs where name='verify_poll' "
        "and state in ('created', 'active')") == 0


async def test_provider_result_never_downgrades_valid(pool):
    await upsert_verdicts(pool, [("a@x.com", "valid", "accepted_email")])
    # duplicate/greylisted second probe must not destroy the paid-for verdict
    await upsert_verdicts(pool, [("a@x.com", "unknown", "timeout")])
    await upsert_verdicts(pool, [("a@x.com", "risky", "low_deliverability")])
    assert await pool.fetchval("select verdict from email_verifications") == "valid"
    # upgrades still apply
    await upsert_verdicts(pool, [("b@x.com", "risky", "role")])
    await upsert_verdicts(pool, [("b@x.com", "valid", "accepted_email")])
    assert await pool.fetchval(
        "select verdict from email_verifications where email='b@x.com'") == "valid"
    # a real bounce always wins
    await upsert_verdicts(pool, [("a@x.com", "invalid", "bounced")], provider="resend")
    assert await pool.fetchval(
        "select verdict from email_verifications where email='a@x.com'") == "invalid"
