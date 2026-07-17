# Email List Cleaning Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No email is sent to an address without a fresh (90-day) verified-valid verdict from Emailable; verdicts cached in Postgres, invalid/risky tagged back to GHL, spend gated by a bot approve button.

**Architecture:** New `email_verifications` verdict cache filled by a jobs-queue pipeline (`verify_submit` → Emailable batch API → `verify_poll` → upsert). All three send paths (queue, timed, broadcast) add a valid-verdict EXISTS condition beside their existing suppression check. Bot triggers verification after `sync_audience`, gates `propose_send` on zero unverified.

**Tech Stack:** Python 3.12 / FastAPI / asyncpg / httpx / pytest (existing repo stack). Spec: `docs/superpowers/specs/2026-07-18-list-cleaning-design.md`.

**Test prerequisite:** `docker start growthable-test-pg` (Postgres on 54329), then `uv run pytest`.

---

### Task 1: Migration, config, test scaffolding

**Files:**
- Create: `supabase/migrations/0008_email_verifications.sql`
- Modify: `app/config.py` (add 4 settings after `daily_report_hour`)
- Modify: `tests/conftest.py:27-31` (truncate list)
- Modify: `tests/helpers.py` (add `verify_all_contacts`)

- [ ] **Step 1: Write migration**

```sql
-- supabase/migrations/0008_email_verifications.sql
-- Mailbox-verification verdict cache (spec: 2026-07-18-list-cleaning-design.md).
-- Separate from suppressions, which stays reserved for bounces/complaints/unsubs.
create table email_verifications (
    email        text primary key,
    verdict      text not null check (verdict in ('valid', 'invalid', 'risky', 'unknown')),
    reason       text,
    provider     text not null default 'emailable',
    verified_at  timestamptz not null default now()
);
```

- [ ] **Step 2: Add settings** in `app/config.py` after `daily_report_hour`:

```python
    emailable_api_key: str = ""
    verdict_ttl_days: int = 90       # verdicts older than this are re-verified
    verify_approval_threshold: int = 1000  # verify runs above this need a human button-click
    verify_cost_per_email: float = 0.0038  # USD, for the approval message estimate
```

- [ ] **Step 3: Add `email_verifications` to the truncate list** in `tests/conftest.py` `_clean_tables` (append before `daily_reports`).

- [ ] **Step 4: Add test helper** to `tests/helpers.py`:

```python
async def verify_all_contacts(pool):
    """Mark every cached contact verified-valid (for tests predating verification)."""
    await pool.execute(
        "insert into email_verifications (email, verdict) "
        "select distinct email, 'valid' from contacts_cache "
        "on conflict (email) do update set verdict='valid', verified_at=now()")
```

- [ ] **Step 5: Run** `uv run pytest tests/test_schema.py -q` — expect PASS (migration applies cleanly).

- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: email_verifications schema + verification settings"`

---

### Task 2: Emailable client

**Files:**
- Create: `app/services/verify_client.py`
- Create: `tests/test_verify_client.py`

- [ ] **Step 1: Write failing tests** (`tests/test_verify_client.py`):

```python
import httpx
import pytest

from app.services.verify_client import EmailableClient, map_result


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return EmailableClient("key_test", client=httpx.AsyncClient(transport=transport))


async def test_create_batch_returns_id():
    def handler(request):
        assert request.url.path == "/v1/batch"
        return httpx.Response(200, json={"id": "batch_1"})
    client = make_client(handler)
    assert await client.create_batch(["a@x.com", "b@y.com"]) == "batch_1"


async def test_get_batch_pending_returns_none():
    def handler(request):
        return httpx.Response(200, json={"processed": 5, "total": 10})
    client = make_client(handler)
    assert await client.get_batch("batch_1") is None


async def test_get_batch_complete_returns_emails():
    def handler(request):
        return httpx.Response(200, json={"emails": [
            {"email": "a@x.com", "state": "deliverable", "reason": "accepted_email"}]})
    client = make_client(handler)
    result = await client.get_batch("batch_1")
    assert result[0]["email"] == "a@x.com"


@pytest.mark.parametrize("raw,expected", [
    ({"email": "a@x.com", "state": "deliverable", "reason": "accepted_email"},
     ("valid", "accepted_email")),
    ({"email": "a@x.com", "state": "undeliverable", "reason": "rejected_email"},
     ("invalid", "rejected_email")),
    ({"email": "a@x.com", "state": "risky", "reason": "low_deliverability"},
     ("risky", "low_deliverability")),
    ({"email": "a@x.com", "state": "unknown", "reason": "timeout"},
     ("unknown", "timeout")),
    ({"email": "a@x.com", "state": "deliverable", "reason": "accepted_email",
      "role": True}, ("risky", "role")),
    ({"email": "a@x.com", "state": "deliverable", "reason": "accepted_email",
      "disposable": True}, ("invalid", "disposable")),
])
def test_map_result(raw, expected):
    assert map_result(raw) == expected
```

- [ ] **Step 2: Run** `uv run pytest tests/test_verify_client.py -q` — expect FAIL (module missing).

- [ ] **Step 3: Implement** `app/services/verify_client.py`:

```python
"""Thin Emailable API wrapper. Provider-neutral surface: create_batch/get_batch/
map_result are all the pipeline knows, so swapping providers touches only this file."""
import httpx

BASE_URL = "https://api.emailable.com/v1"


def map_result(raw: dict) -> tuple[str, str | None]:
    """Emailable result -> (verdict, reason). Role/disposable flags override state
    (spec: role accounts are risky, disposable domains are invalid)."""
    if raw.get("disposable"):
        return "invalid", "disposable"
    if raw.get("role"):
        return "risky", "role"
    state = raw.get("state")
    if state == "deliverable":
        return "valid", raw.get("reason")
    if state == "undeliverable":
        return "invalid", raw.get("reason")
    if state == "risky":
        return "risky", raw.get("reason")
    return "unknown", raw.get("reason")


class EmailableClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30)

    async def create_batch(self, emails: list[str]) -> str:
        resp = await self._client.post(f"{BASE_URL}/batch", json={
            "emails": ",".join(emails), "api_key": self._api_key})
        resp.raise_for_status()
        return resp.json()["id"]

    async def get_batch(self, batch_id: str) -> list[dict] | None:
        """None while the batch is still processing, else the per-email results."""
        resp = await self._client.get(f"{BASE_URL}/batch",
                                      params={"id": batch_id, "api_key": self._api_key})
        resp.raise_for_status()
        body = resp.json()
        return body.get("emails")  # absent until complete
```

- [ ] **Step 4: Run** `uv run pytest tests/test_verify_client.py -q` — expect PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: Emailable verification client"`

---

### Task 3: Verification service — queries, upsert, request gate

**Files:**
- Create: `app/services/verification.py`
- Create: `tests/test_verification.py`

- [ ] **Step 1: Write failing tests** (`tests/test_verification.py`) — uses a fake client and real DB fixtures:

```python
import json
import uuid

from app.services import verification
from app.services.verification import (request_verification, unverified_count,
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
    for i, email in enumerate(emails):
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
```

- [ ] **Step 2: Run** `uv run pytest tests/test_verification.py -q` — expect FAIL (module missing).

- [ ] **Step 3: Implement** `app/services/verification.py` (job processing comes in Task 4 — this step is queries + request gate only):

```python
"""Verdict-cache orchestration (spec: 2026-07-18-list-cleaning-design.md).
Fail-safe by construction: an email with no fresh 'valid' verdict is excluded
from every send path, so verification errors can only under-send, never over-send."""
import json
import logging

from app.config import Settings
from app.services.jobs import complete_job, enqueue, fail_job, fetch_job
from app.services.suppressions import normalize
from app.services.verify_client import map_result

log = logging.getLogger(__name__)

BATCH_CHUNK = 1000       # emails per Emailable batch
POLL_DELAY_SECONDS = 30
VERDICT_TAGS = {"invalid": "email-invalid", "risky": "email-risky",
                "unknown": "email-risky"}

_UNVERIFIED_SQL = """
    select distinct c.email
    from campaign_contacts cc
    join contacts_cache c using (ghl_contact_id)
    where cc.campaign_id = $1
      and not exists (select 1 from email_verifications v
                      where v.email = c.email and v.verified_at > now() - make_interval(days => $2))
    order by c.email
"""


async def unverified_emails(pool, campaign_id, ttl_days: int) -> list[str]:
    """Audience emails with no verdict, or only a stale one (any verdict re-verifies
    after TTL — a 91-day-old 'valid' is as untrustworthy as a missing one)."""
    return [r["email"] for r in await pool.fetch(_UNVERIFIED_SQL, campaign_id, ttl_days)]


async def unverified_count(pool, campaign_id, ttl_days: int) -> int:
    return len(await unverified_emails(pool, campaign_id, ttl_days))


async def upsert_verdicts(pool, results: list[tuple], provider: str = "emailable") -> None:
    """results: [(email, verdict, reason)]. Last write wins; verified_at refreshes."""
    await pool.executemany(
        """insert into email_verifications (email, verdict, reason, provider)
           values ($1, $2, $3, $4)
           on conflict (email) do update set verdict=excluded.verdict,
               reason=excluded.reason, provider=excluded.provider, verified_at=now()""",
        [(normalize(e), v, r, provider) for e, v, r in results])


async def request_verification(pool, settings: Settings, campaign_id) -> dict:
    """Kick off verification for a campaign's unverified audience. Auto-submits at or
    under the approval threshold; above it, the caller must post an approve button
    (spend gate) and the button handler enqueues verify_submit."""
    count = await unverified_count(pool, campaign_id, settings.verdict_ttl_days)
    if count == 0:
        return {"status": "verified"}
    est_cost = round(count * settings.verify_cost_per_email, 2)
    if count > settings.verify_approval_threshold:
        return {"status": "needs_approval", "unverified": count, "est_cost": est_cost}
    await enqueue(pool, "verify_submit", {"campaign_id": str(campaign_id)})
    return {"status": "submitted", "unverified": count, "est_cost": est_cost}


async def verification_summary(pool, campaign_id, ttl_days: int) -> dict:
    """Fresh-verdict counts for the campaign audience + how many still lack one."""
    rows = await pool.fetch(
        """select v.verdict, count(distinct c.email) as n
           from campaign_contacts cc
           join contacts_cache c using (ghl_contact_id)
           join email_verifications v on v.email = c.email
                and v.verified_at > now() - make_interval(days => $2)
           where cc.campaign_id = $1
           group by v.verdict""", campaign_id, ttl_days)
    summary = {r["verdict"]: r["n"] for r in rows}
    summary["unverified"] = await unverified_count(pool, campaign_id, ttl_days)
    return summary
```

- [ ] **Step 4: Run** `uv run pytest tests/test_verification.py -q` — expect PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: verification service — verdict queries, upsert, spend-gated request"`

---

### Task 4: Verification job pipeline (submit → poll → upsert → GHL tags)

**Files:**
- Modify: `app/services/verification.py` (append)
- Modify: `tests/test_verification.py` (append)

- [ ] **Step 1: Write failing tests** (append to `tests/test_verification.py`):

```python
async def drain_verification(pool, settings, client, passes=5):
    from app.services.verification import process_verification_jobs
    for _ in range(passes):
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
    from app.services.verification import process_verification_jobs
    await process_verification_jobs(pool, settings, client)  # submit -> poll job
    await process_verification_jobs(pool, settings, client)  # poll -> pending
    # a fresh poll job exists (delayed), nothing verified yet
    assert await pool.fetchval(
        "select count(*) from jobs where name='verify_poll' and state='created'") == 1
    assert await pool.fetchval("select count(*) from email_verifications") == 0


async def test_provider_error_retries_job(pool):
    class BrokenClient(FakeVerifyClient):
        async def create_batch(self, emails):
            raise RuntimeError("api down")
    cid = await seed_campaign(pool, ["a@x.com"])
    settings = make_settings()
    await request_verification(pool, settings, cid)
    from app.services.verification import process_verification_jobs
    await process_verification_jobs(pool, settings, BrokenClient())
    job = await pool.fetchrow(
        "select state, retry_count from jobs where name='verify_submit'")
    assert job["state"] == "created" and job["retry_count"] == 1
```

- [ ] **Step 2: Run** `uv run pytest tests/test_verification.py -q` — expect FAIL (`process_verification_jobs` missing).

- [ ] **Step 3: Implement** — append to `app/services/verification.py`:

```python
def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _enqueue_verdict_tags(pool, results: list[tuple]) -> None:
    """Tag invalid/risky contacts in GHL via the existing write-back queue.
    Advisory only — Postgres is the enforcement point; failures retry there."""
    flagged = {normalize(e): VERDICT_TAGS[v] for e, v, _ in results if v in VERDICT_TAGS}
    if not flagged:
        return
    rows = await pool.fetch(
        "select ghl_contact_id, email from contacts_cache where email = any($1::text[])",
        list(flagged))
    for r in rows:
        await enqueue(pool, "ghl_writeback", {
            "kind": "add_tags", "contact_id": r["ghl_contact_id"],
            "tags": [flagged[r["email"]]]})


async def process_verification_jobs(pool, settings: Settings, client,
                                    backoff_seconds: int = 60) -> int:
    """One worker pass: drain verify_submit and verify_poll jobs. A still-processing
    provider batch re-enqueues its poll (completing the old job, so retry_limit only
    counts real failures, not long batches)."""
    done = 0
    while (job := await fetch_job(pool, "verify_submit")) is not None:
        try:
            emails = await unverified_emails(
                pool, job["data"]["campaign_id"], settings.verdict_ttl_days)
            for chunk in _chunks(emails, BATCH_CHUNK):
                batch_id = await client.create_batch(chunk)
                await enqueue(pool, "verify_poll", {"batch_id": batch_id},
                              start_after_seconds=POLL_DELAY_SECONDS)
        except Exception:
            log.exception("verify_submit job %s failed", job["id"])
            await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
            continue
        await complete_job(pool, job["id"])
        done += 1

    while (job := await fetch_job(pool, "verify_poll")) is not None:
        try:
            raw = await client.get_batch(job["data"]["batch_id"])
            if raw is None:  # still processing — poll again later
                await complete_job(pool, job["id"])
                await enqueue(pool, "verify_poll", job["data"],
                              start_after_seconds=POLL_DELAY_SECONDS)
                continue
            results = [(r["email"], *map_result(r)) for r in raw]
            await upsert_verdicts(pool, results)
            await _enqueue_verdict_tags(pool, results)
        except Exception:
            log.exception("verify_poll job %s failed", job["id"])
            await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
```

- [ ] **Step 4: Run** `uv run pytest tests/test_verification.py -q` — expect PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: verification job pipeline with GHL tag write-back"`

---

### Task 5: Gate all three send paths

**Files:**
- Modify: `app/services/dispatch.py:23-43` (`enqueue_campaign_sends`), `:46-70` (`enqueue_timed_sends`)
- Modify: `app/services/broadcast.py:33-40` (`AUDIENCE_SQL`), `:65-72` (`_audience_csv`), `:85-101` (`_start_import`), `:128-140` (mirror insert)
- Modify: `app/routers/campaigns.py:80` (caller signature)
- Modify: `tests/test_dispatch.py`, `tests/test_timed_sends.py`, `tests/test_broadcast.py`, `tests/test_api_campaigns.py` (verify fixtures + signatures)
- Test: append to `tests/test_verification.py`

- [ ] **Step 1: Write failing tests** (append to `tests/test_verification.py`):

```python
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
```

- [ ] **Step 2: Run** `uv run pytest tests/test_verification.py -q` — expect FAIL (signatures/conditions missing).

- [ ] **Step 3: Implement.** The shared SQL condition (verbatim, used four times):

```sql
and exists (select 1 from email_verifications v
            where v.email = c.email and v.verdict = 'valid'
              and v.verified_at > now() - make_interval(days => $2))
```

In `app/services/dispatch.py`:
- `enqueue_campaign_sends(pool, campaign_id)` → `enqueue_campaign_sends(pool, settings: Settings, campaign_id)`; add the condition to its insert-select (after the suppression `not exists`), pass `settings.verdict_ttl_days` as `$2` (campaign_id stays `$1`).
- `enqueue_timed_sends` already takes settings; add the condition to its select with `$2 = settings.verdict_ttl_days`.

In `app/services/broadcast.py`:
- `AUDIENCE_SQL` gains the condition; `_audience_csv(pool, campaign_id)` → `_audience_csv(pool, campaign_id, ttl_days)` passing `$2`.
- `_start_import(pool, resend, slack, campaign)` → `_start_import(pool, settings, resend, slack, campaign)`, calls `_audience_csv(pool, campaign["id"], settings.verdict_ttl_days)`; update its call in `process_broadcast_campaigns` (settings already in scope).
- The mirror insert in `_send_if_imported` gains the same condition with `$2 = settings.verdict_ttl_days`.

In `app/routers/campaigns.py:80`: `enqueue_campaign_sends(request.app.state.pool, request.app.state.settings, campaign["id"])`.

- [ ] **Step 4: Fix pre-existing tests.** Run `uv run pytest tests/test_dispatch.py tests/test_timed_sends.py tests/test_broadcast.py tests/test_api_campaigns.py -q`. For each failure: (a) update `enqueue_campaign_sends`/`_audience_csv`/`_start_import` call signatures per Step 3, and (b) after the test's contact-cache setup add:

```python
from tests.helpers import verify_all_contacts
await verify_all_contacts(pool)
```

Do NOT weaken assertions — every fix is signature or fixture only. Tests that assert suppressed/dnd contacts are excluded still pass because `verify_all_contacts` marks all cached contacts valid and the other conditions still apply.

- [ ] **Step 5: Run** `uv run pytest tests/test_verification.py tests/test_dispatch.py tests/test_timed_sends.py tests/test_broadcast.py tests/test_api_campaigns.py -q` — expect PASS.
- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: require fresh valid verdict on all three send paths"`

---

### Task 6: Bounce feedback loop

**Files:**
- Modify: `app/routers/webhooks.py:89-95` (bounce branch)
- Modify: `tests/test_webhook_resend.py` (append test)

- [ ] **Step 1: Write failing test** (append to `tests/test_webhook_resend.py`, following that file's existing bounce-event test pattern for posting a signed svix event):

```python
async def test_hard_bounce_marks_verification_invalid(pool, client):
    # reuse the file's existing hard-bounce test setup verbatim (campaign + send +
    # signed email.bounced payload with bounce type Permanent), then assert:
    row = await pool.fetchrow(
        "select verdict, reason, provider from email_verifications where email=$1",
        "recipient@example.com")  # match the email used by that setup
    assert (row["verdict"], row["reason"], row["provider"]) == ("invalid", "bounced", "resend")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_webhook_resend.py -q` — expect FAIL (no verification row).

- [ ] **Step 3: Implement** — in `app/routers/webhooks.py`, inside the `email.bounced` branch after `add_suppression(...)` (same non-Transient guard):

```python
            await upsert_verdicts(pool, [(send["email"], "invalid", "bounced")],
                                  provider="resend")
```

with import at top: `from app.services.verification import upsert_verdicts`.

- [ ] **Step 4: Run** `uv run pytest tests/test_webhook_resend.py -q` — expect PASS.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: hard bounces feed back into verification cache"`

---

### Task 7: Bot + Slack integration

**Files:**
- Modify: `app/services/bot.py` (sync_audience tool, propose_send gate, new verification_status tool, verify approval blocks)
- Modify: `app/routers/slack.py:77-112` (approve_verify/cancel_verify actions)
- Modify: `tests/test_bot.py`, `tests/test_slack_interactions.py` (append tests)

- [ ] **Step 1: Write failing tests.** Append to `tests/test_bot.py` (follow that file's existing pattern for driving `_run_tool` with a stub slack/ghl):

```python
async def test_propose_send_blocked_until_verified(pool, ...):  # match file's fixture style
    # create campaign + one audience contact with NO verification row, seed_tested_at set
    result = await engine._run_tool("propose_send", {"campaign_id": str(cid)})
    assert "unverified" in result["error"]

async def test_verification_status_tool(pool, ...):
    # campaign with one valid-verified contact:
    result = await engine._run_tool("verification_status", {"campaign_id": str(cid)})
    assert result == {"valid": 1, "unverified": 0}
```

Append to `tests/test_slack_interactions.py` (follow the file's signed block_actions POST pattern):

```python
async def test_approve_verify_enqueues_submit(pool, client):
    # post a block_actions payload with action_id="approve_verify",
    # value=json.dumps({"campaign_id": str(cid), "count": 5})
    job = await pool.fetchrow("select data from jobs where name='verify_submit'")
    assert json.loads(job["data"])["campaign_id"] == str(cid)
```

- [ ] **Step 2: Run** the two test files — expect FAIL.

- [ ] **Step 3: Implement in `app/services/bot.py`:**

Add after `approval_blocks` (module level):

```python
def verify_approval_blocks(campaign_id: str, count: int, est_cost: float) -> list:
    value = json.dumps({"campaign_id": campaign_id, "count": count})
    summary = (f"*Verification needed:* {count} audience emails have no fresh "
               f"deliverability verdict.\nEstimated cost: *${est_cost:.2f}* "
               f"(Emailable). Sends stay blocked until the audience is verified.")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": "approve_verify",
             "text": {"type": "plain_text", "text": "Verify"}, "value": value},
            {"type": "button", "style": "danger", "action_id": "cancel_verify",
             "text": {"type": "plain_text", "text": "Not now"}, "value": value},
        ]},
    ]
```

Replace the `sync_audience` tool branch:

```python
        if name == "sync_audience":
            result = await sync_audience(pool, self._ghl, args["campaign_id"])
            verification = await request_verification(
                pool, self._settings, uuid.UUID(args["campaign_id"]))
            if verification["status"] == "needs_approval":
                await self._slack.post_message(
                    self._turn_context["channel"],
                    text="Verification approval needed",
                    blocks=verify_approval_blocks(
                        args["campaign_id"], verification["unverified"],
                        verification["est_cost"]),
                    thread_ts=self._turn_context["thread_ts"])
                verification["note"] = ("approval buttons posted; a human must click "
                                        "Verify before this audience can be checked")
            return {**result, "verification": verification}
```

Add to the `propose_send` branch, after the seed-test check and before the audience count:

```python
            unverified = await unverified_count(
                pool, campaign["id"], self._settings.verdict_ttl_days)
            if unverified:
                return {"error": f"{unverified} audience emails are unverified — "
                                 "verification must finish before sending (check "
                                 "verification_status; run sync_audience if it never started)"}
```

and change its audience count query to the send-eligible count (valid + not suppressed + not dnd), so the approval message shows the real recipient number:

```python
            audience = await pool.fetchval(
                """select count(*) from campaign_contacts cc
                   join contacts_cache c using (ghl_contact_id)
                   where cc.campaign_id = $1 and c.dnd = false
                     and not exists (select 1 from suppressions s where s.email = c.email)
                     and exists (select 1 from email_verifications v
                                 where v.email = c.email and v.verdict = 'valid'
                                   and v.verified_at > now() - make_interval(days => $2))""",
                campaign["id"], self._settings.verdict_ttl_days)
```

Add tool declaration to `TOOLS` and branch:

```python
    _tool("verification_status",
          "Deliverability-verification progress for the campaign audience: counts by "
          "verdict plus how many emails are still unverified.",
          {"campaign_id": {"type": "string"}}, ["campaign_id"]),
```

```python
        if name == "verification_status":
            return await verification_summary(
                pool, uuid.UUID(args["campaign_id"]), self._settings.verdict_ttl_days)
```

Imports at top of bot.py: `import uuid`, and `from app.services.verification import (request_verification, unverified_count, verification_summary)`.

Update `SYSTEM_PROMPT` workflow line 3 (currently "create_campaign, then sync_audience and report the audience size AND the country") to mention verification:

```
3. create_campaign, then sync_audience and report the audience size AND the country
   breakdown. sync_audience also starts deliverability verification — report its
   status; large audiences need a human to click Verify (cost gate). propose_send
   is blocked until every audience email has a fresh verdict; only verified-valid
   contacts are sent to.
```

- [ ] **Step 4: Implement in `app/routers/slack.py`** — insert before the `campaign_id = uuid.UUID(...)` line (`:97`):

```python
    if action["action_id"] in ("approve_verify", "cancel_verify"):
        if action["action_id"] == "cancel_verify":
            await slack.update_message(
                channel, message_ts,
                text=f"❌ Verification declined by <@{user}> — sends stay blocked "
                     "until the audience is verified.")
        else:
            await enqueue(pool, "verify_submit",
                          {"campaign_id": value["campaign_id"]})
            await slack.update_message(
                channel, message_ts,
                text=f"✅ Verification approved by <@{user}> — {value['count']} "
                     "emails submitted; I'll have verdicts shortly.")
        return Response(status_code=200)
```

- [ ] **Step 5: Run** `uv run pytest tests/test_bot.py tests/test_slack_interactions.py -q` — expect PASS.
- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: bot verification flow — auto-trigger, spend approval, send gate"`

---

### Task 8: Worker wiring + full suite

**Files:**
- Modify: `app/worker.py` (client + tick)
- Test: full suite

- [ ] **Step 1: Wire the worker.** In `app/worker.py` add imports:

```python
from app.services.verification import process_verification_jobs
from app.services.verify_client import EmailableClient
```

In `run_forever()` after the `resend = ...` line:

```python
    verifier = (EmailableClient(settings.emailable_api_key)
                if settings.emailable_api_key else None)
```

In the tick loop, after `await process_writeback_jobs(pool, ghl)`:

```python
            if verifier is not None:
                await process_verification_jobs(pool, settings, verifier)
```

- [ ] **Step 2: Run the full suite** `uv run pytest -q` — expect ALL PASS. Fix any straggler test still calling an old signature (same recipe as Task 5 Step 4).

- [ ] **Step 3: Commit** `git add -A && git commit -m "feat: verification jobs in worker tick"`

---

### Task 9: Deployment notes (no code)

- [ ] Apply `supabase/migrations/0008_email_verifications.sql` in Supabase (along with pending 0006/0007 if not yet applied).
- [ ] Set `EMAILABLE_API_KEY` on the Render worker service (Ryan signs up at emailable.com — do not create the account for him).
- [ ] Backfill: in #marketing-manager, re-run sync_audience for the paused campaign; the bot posts the Verify button (~47.7k ≈ $180); Ryan clicks Verify.
- [ ] After verification completes: reset campaign status from 'paused', re-approve, send.
- [ ] Separately recommended: set `ALERT_WEBHOOK_URL` so future guardrail trips alert in real time.

---

## Self-review notes

- Spec coverage: schema/TTL (T1), provider client + mapping (T2), request gate + threshold (T3), batch pipeline + GHL tags (T4), all three send paths (T5), bounce feedback (T6), bot gate + buttons + status (T7), worker (T8), rollout (T9). `unknown` → excluded + tagged `email-risky` (VERDICT_TAGS) per spec's "unknown = risky" rule.
- Type consistency: `request_verification` returns `status ∈ {verified, submitted, needs_approval}`; both bot and tests use those literals. `enqueue_campaign_sends(pool, settings, campaign_id)` used consistently in T5 code, T5 tests, and the campaigns router.
- Deliberate scope cuts (YAGNI): no per-campaign verify-batch progress table (poll jobs + `verification_summary` cover reporting); no daily-report changes.
