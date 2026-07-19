# Knowledge Service Split — Design

**Date:** 2026-07-19
**Status:** Approved direction (Ryan, in-session). BUILD IN A FRESH SESSION —
do not implement at the tail of the 2026-07-18/19 incident day.
**Goal:** Move marketing-knowledge ingestion and analysis (data connectors,
scopes, meeting/CRM/revenue/analytics sources) out of the email-sending worker,
so adding a data source or changing an OAuth scope can never redeploy or crash
the safety-critical send pipeline.

## Why (the precise coupling being removed)

Today the pipeline already has **code-level isolation**: knowledge tools degrade
gracefully (`recent_meetings`/`sales_activity` return `unavailable`), so a bad
key/scope cannot crash sending. It lacks **deploy-level isolation**: knowledge
connectors live in the same worker process/env as the send loop, so every scope
change or new connector redeploys sending. This split adds deploy- and
failure-domain isolation. Justified because many sources are planned (Resonance,
GHL calendars, Xero, ad platforms, web analytics) — the churniest part of the
system should not share a blast radius with the stable, safety-critical core.

## Architecture: insights-snapshot (read model)

Three processes, one database:
- **web** (existing) — Slack/webhooks/unsub. Unchanged.
- **email worker** (existing, slimmed) — sending, verification, guardrails,
  dispatch, daily digest, weekly-review TRIGGER. Owns campaigns/sends/
  suppressions/email_verifications. Loses the external knowledge connectors.
- **knowledge worker** (NEW) — connects to all external marketing data sources
  on its own schedule, computes a brief, writes ONE row to `insights_snapshots`.
  Its deploys/scopes/crashes never touch the email services.

Data flow for the weekly review:
1. Knowledge worker, on its own cron (e.g. Sunday night + on demand), gathers
   campaign_history (reads shared tables), tag_stats, engagement_segments,
   sales_activity (GHL calendars+conversations), recent_meetings (Resonance),
   future sources → assembles a structured brief → upserts the latest into
   `insights_snapshots (week_start, brief jsonb, sources_ok jsonb, created_at)`.
2. Monday 9am the email worker's weekly trigger fires as today, BUT the bot's
   data tools now READ the latest `insights_snapshots.brief` instead of calling
   external APIs live. The Monday agent no longer depends on 5 live APIs
   answering in the moment — more reliable, not just more isolated.

## Migration from the current monolith

- New table `insights_snapshots` (migration 0009).
- Move `app/services/resonance.py` + the GHL calendar methods + the data-tool
  query bodies (`campaign_history`, `tag_stats`, `engagement_segments`,
  `sales_activity`, `recent_meetings`) into a new `app/knowledge/` package.
- Knowledge worker entrypoint `python -m app.knowledge.worker`; new Render
  service in render.yaml (Docker, same image, different command).
- Bot's data tools become thin readers of the snapshot (`brief["campaign_history"]`
  etc.) with a freshness note ("as of <created_at>"). Keep graceful-degrade:
  a missing source shows in `sources_ok` and the bot says so.
- Env split: RESONANCE_*, calendar-scoped GHL token, future data-source keys
  live ONLY on the knowledge worker. Email services never carry them again.

## Caveats (must address at build time)

- **Supabase pooler caps at 15 clients.** Current: web(5)+worker(5). Knowledge
  worker takes a SMALL pool (max 3). That is 13 — near ceiling. Before adding a
  4th service, move to transaction-mode pooling. Document the budget in db.py.
- This is a refactor of a WORKING system — do it test-first, one extraction at a
  time (table → knowledge package → new service → flip bot tools to read
  snapshot → remove connectors from email worker), verifying sending stays green
  at each step. Never both services touching external APIs at once mid-migration.
- Keep the weekly-review TRIGGER on the email worker (it owns the Slack thread +
  bot loop); only the DATA GATHERING moves. The bot still drafts/seed-tests/
  proposes on the email side — those are pipeline actions, not knowledge.

## Out of scope / phase 2

- Xero revenue connector (OAuth) — lands in the knowledge worker once it exists.
- Web analytics / ad-platform connectors — same home.
- Real Resonance API contract still pending Ryan's docs (client currently
  assumes REST bearer; see app/services/resonance.py).

## Explicitly NOT changing

Send core, verification, guardrails + auto-resume breaker, human Send gate — all
untouched. This split is about where knowledge is gathered, nothing about how
mail goes out.
