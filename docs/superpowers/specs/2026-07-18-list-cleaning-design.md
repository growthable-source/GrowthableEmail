# Email List Cleaning Service — Design

**Date:** 2026-07-18
**Status:** Approved by Ryan (brainstorming session)

> **Amendment (2026-07-18, post-implementation):** the 90-day TTL described below
> was REMOVED at Ryan's direction — verdicts are permanent and each email is billed
> to the provider at most once, ever. Send paths require `verdict='valid'` with no
> freshness condition; `VERDICT_TTL_DAYS` no longer exists. List decay is covered by
> the bounce-feedback upsert (valid → invalid on hard bounce) and the guardrail.
> To force a re-check of an address, delete its `email_verifications` row.
**Trigger:** Campaign "A lot has changed at Growthable" was auto-paused by the
guardrail kill rule on 2026-07-17/18 — 22 bounces on 221 sends (~10%, limit 3%).
The 47.7k GHL list has never been mailbox-verified; `sync_audience` only drops
syntax-invalid, DND, and already-suppressed addresses, so dead mailboxes flow
straight to Resend and bounce.

## Goal

No email is ever sent to an address that has not been verified as deliverable
by a mailbox-level verification provider within the last 90 days. The 3%
guardrail becomes a backstop, not the first line of defense.

## Decisions (made in brainstorming)

1. **Paid verification API** — mailbox-level SMTP verification via a provider,
   not free heuristics. Provider: **Emailable** (~$0.0038/email; ~$180 for the
   current 47.7k list), behind a provider-neutral client interface so it can be
   swapped (ZeroBounce/NeverBounce/Kickbox) without pipeline changes.
2. **Send to verified-valid only.** Risky results (catch-all, role accounts,
   unknown/timeouts) are excluded from sends but retained with their verdict —
   not suppressed, revisitable later.
3. **Write verdicts back to GHL** as tags (`email-invalid`, `email-risky`) via
   the existing `jobs` write-back queue, so Dan's follow-ups / SMS workflows
   also benefit. GHL tags are advisory; Postgres is the enforcement point.
4. **Architecture: standalone verification service with a verdict cache**
   (chosen over inline-at-sync and manual bot command). Pay once per address
   per TTL window regardless of how many campaigns it appears in; sync stays
   fast.

## Schema (migration 0008)

```sql
create table email_verifications (
    email        text primary key,
    verdict      text not null,   -- valid | invalid | risky | unknown
    reason       text,            -- provider detail: mailbox_not_found, catch_all,
                                  -- role, disposable, timeout, ...
    provider     text not null default 'emailable',
    verified_at  timestamptz not null default now()
);
```

- Keyed by normalized email (same `normalize()` as suppressions).
- Deliberately separate from `suppressions`, which remains reserved for
  bounces/complaints/unsubscribes.
- A verdict is **fresh** for `VERDICT_TTL_DAYS` (default 90). Stale verdicts are
  re-verified on next audience sync; stale/missing = excluded from sends.
- `unknown` is treated exactly like `risky` for send eligibility: not sent, not
  suppressed, re-verifiable.

## Data flow

1. `sync_audience` completes → enqueue a `verify_audience` job (existing `jobs`
   table) covering all distinct audience emails lacking a fresh verdict.
2. **Spend gate:** if the unverified count exceeds 1,000, the job does not
   submit automatically — the bot posts "This audience has N unverified emails
   (~$X to verify)" with an approve button (mirrors the seed-test/approve-send
   pattern). ≤1,000 auto-runs.
3. Worker submits chunks to Emailable's async batch API, polls on its normal
   tick (a `verify_poll` job per provider batch), and upserts verdicts as
   batches complete.
4. Each `invalid` or `risky` verdict enqueues a GHL tag write-back job.
5. Send paths — broadcast `AUDIENCE_SQL`, queue dispatch, timed enqueue — each
   add one condition alongside their existing suppression check:

   ```sql
   and exists (select 1 from email_verifications v
               where v.email = c.email and v.verdict = 'valid'
                 and v.verified_at > now() - make_interval(days => $ttl))
   ```

6. **Approval gate:** `approve_send` refuses while any audience email lacks a
   fresh verdict; the bot reports progress ("41,200/47,700 verified, 4,100
   invalid so far").
7. **Bounce feedback:** the existing bounce webhook, in addition to
   suppressing, upserts `verdict='invalid', reason='bounced'` so the cache
   learns from real-world failures.

## Components

New:
- `app/services/verify_client.py` — `EmailableClient`: `create_batch(emails)
  -> batch_id`, `get_batch(batch_id) -> results | pending`. Thin httpx wrapper.
  Settings: `EMAILABLE_API_KEY`. Interface is provider-neutral.
- `app/services/verification.py` — orchestration: find stale/missing verdicts
  for a campaign, submit batches, handle poll jobs, upsert verdicts, enqueue
  GHL tag jobs, expose `unverified_count(campaign_id)` for the gate and bot.

Touched:
- `app/services/broadcast.py`, `app/services/dispatch.py` — valid-verdict
  condition in audience SQL (all three send paths).
- `app/routers/webhooks.py` — bounce handler upserts `invalid`.
- GHL client — add-tag call for write-back jobs.
- `app/services/bot.py` — verification status in reports, approve-verification
  button, `approve_send` gate.
- `app/config.py` — `EMAILABLE_API_KEY`, `VERDICT_TTL_DAYS=90`,
  `VERIFY_APPROVAL_THRESHOLD=1000`.

## Error handling

- Provider outage/timeout → poll job retries with the jobs queue's existing
  backoff; approval stays blocked; bot reports why.
- Partial batch failure → affected emails stay unverified, which is fail-safe:
  unverified means excluded, never included.
- GHL tag write-back failures retry independently and never block sending.
- No verdict deletion on TTL expiry — stale rows are overwritten on
  re-verification; history stays queryable via `verified_at`.

## Testing

- `FakeVerifyClient` in tests (mirrors GHL/Resend fakes).
- New `tests/test_verification.py`: verdict caching (no double-billing within
  TTL), TTL expiry re-verification, all three send paths excluding non-valid,
  bounce-feedback upsert, GHL job enqueue, >1,000 approval gate.
- Existing broadcast/dispatch/timed-send tests get a fixture marking their
  contacts valid so they keep passing.

## Rollout

1. Apply migration 0008 in Supabase; set `EMAILABLE_API_KEY` on Render.
2. One-off backfill of the 47.7k list (~$180) via the bot approve button.
3. Re-run `sync_audience` for the paused campaign.
4. Resume "A lot has changed at Growthable" against a verified-valid audience.

Related pending fix (out of scope here, do alongside): `ALERT_WEBHOOK_URL` is
unset, so guardrail kill-rule pauses are silent until the next daily digest.
