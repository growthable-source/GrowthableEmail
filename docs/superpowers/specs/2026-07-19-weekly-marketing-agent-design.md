# Weekly Marketing Agent — Design

**Date:** 2026-07-19
**Status:** Approved direction (Ryan, in-session); implementation plan to follow
**Goal:** Every Monday 9am AEST the email bot stops waiting for instructions and
acts as a marketing manager: reviews all available data, proposes 1–2 campaigns
with finished drafts and seed tests already in Ryan's inbox, and posts them for
one-click approval. Humans keep exactly one job: clicking Send.

## Decisions (made in brainstorming)

1. **Full pipeline autonomy** — the Monday post contains analysis AND finished
   campaign drafts AND completed seed tests. Not ideas-first, not report-only.
   The human approval gate on sending is untouched and non-negotiable.
2. **Data sources v1:** email performance history (own DB), GHL CRM — tag
   inventory, conversation activity, and **sales-calendar bookings over the last
   90 days** (Ryan: "likely most telling") — and **Resonance**, the in-house
   notetaker (Ryan provides API key; integration lands when API details arrive).
   Xero revenue: phase 2.
3. **Cadence:** Monday 09:00 Australia/Sydney, in #marketing-manager, as a new
   thread per week.

## Architecture

### Trigger (worker)
`maybe_start_weekly_review(pool, slack, settings)` on the worker tick, guarded
by the existing `daily_reports` claim table (`report_type='weekly_review'`,
fires when local dow==Monday and hour>=9, once per week). It:
1. Posts the kickoff message to the channel ("📋 *Weekly marketing review* —
   digging through the data, plan incoming.") to obtain a fresh `thread_ts`.
2. Enqueues a `bot_turn` job with a synthetic user message ("It is Monday —
   run your weekly marketing review.") bound to that thread, so the normal
   BotEngine loop does the work with its tools.

### New bot tools
- `campaign_history` — last 90 days of campaigns: audience tag + size,
  sent/delivered/opened/clicked/bounced/unsubscribed counts and rates, send
  mode, dates. Source: own campaigns/sends/events tables.
- `tag_stats` — contact counts per GHL tag from contacts_cache, with
  verified-valid counts (sendable audience inventory).
- `engagement_segments` — counts of contacts carrying `opened-*`/`clicked-*`
  tags per campaign slug (who is warm, per topic).
- `sales_activity` — GHL: appointments booked in the sales calendar(s) over the
  last N days (default 90) + conversation counts per week. Requires the PIT to
  carry `calendars.readonly` scope — verify; if absent, tool degrades to
  conversations-only and says so.
- `recent_meetings` — Resonance API: meeting titles/summaries/key topics for
  the last 7 days. Ships as a thin client behind `RESONANCE_API_KEY` +
  `RESONANCE_API_URL` settings once Ryan supplies the API shape; until
  configured the tool returns {"unavailable": "RESONANCE_API_KEY not set"}
  and the bot plans without it.

### System prompt addition (weekly ritual)
When the turn starts with the weekly-review trigger, the bot must:
1. Pull campaign_history, tag_stats, engagement_segments, sales_activity,
   recent_meetings.
2. Post a SHORT analysis: what worked, what's decaying, what prospects are
   asking about, sendable-audience state (sizes after verification).
3. Propose 1–2 campaigns with clear goals (bookings, reactivation, launch
   follow-up), pick audiences from real tags, DRAFT them fully per the brand
   guide, run seed tests, then propose_send with a recommended ramp — all in
   the same thread, ending with approval buttons and a one-line "why this,
   why now" per campaign.
4. Never exceed 2 proposed campaigns/week; never touch audiences that
   received email in the last 7 days unless justified by engagement data.

### Guardrails unchanged
Verification gates, seed-test requirement, human Send click, kill rule +
auto-resume breaker all apply to weekly-agent campaigns identically.

## Config additions
- `weekly_review_enabled: bool = True`
- `weekly_review_dow: int = 0` (Monday), `weekly_review_hour: int = 9` (bot_timezone)
- `resonance_api_key: str = ""`, `resonance_api_url: str = ""`

## Open items
- Resonance API contract (endpoint + auth + response sample) — Ryan to provide;
  key goes directly into Render worker env, never through chat.
- Confirm GHL PIT has `calendars.readonly`; if not, Ryan regenerates the token
  with the scope added.
