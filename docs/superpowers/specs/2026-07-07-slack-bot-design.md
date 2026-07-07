# Slack Bot Interface — Design

Conversational Slack interface for the GHL↔Resend pipeline: tag the bot in a designated
private channel, it gathers audience/subject/content/schedule via a Claude-powered
conversation, previews via seed test, and dispatches after a button-click approval.

## Decisions (from brainstorming, 2026-07-07)

1. **Content model:** one generic `newsletter.tsx` template; Claude writes the copy and
   fills its props. Campaign copy stored as JSON on the campaign row. Bespoke templates
   remain a Claude Code session job.
2. **Approval:** anyone in the designated (private) channel can approve. The channel is
   the permission boundary; the bot ignores all other channels.
3. **Scheduling:** supported. Approval sets `scheduled_at`; the worker promotes
   `scheduled → dispatching` when due. "Send now" = `scheduled_at = now()`.
4. **Architecture (Option A):** bot lives inside the existing FastAPI service + worker.
   No new services. Slack Events API (not Socket Mode). Durability via the existing
   Postgres `jobs` queue.

## Architecture & data flow

```
Slack app_mention / thread reply
  → POST /slack/events (signature check, 200 ack immediately)
      → enqueue jobs row: name='bot_turn' {channel, thread_ts, user, text}
  → worker tick: process_bot_turns()
      → load bot_sessions row (thread_ts) → messages history
      → Claude API (tool-use loop) with pipeline tools
      → chat.postMessage reply into thread; save session

Slack "Send" / "Cancel" button click
  → POST /slack/interactions (signature check)
      → send: set scheduled_at + status='scheduled' (or dispatch-now); update message
      → cancel: campaign status='draft'; update message
```

## Components

**1. `emails/templates/newsletter.tsx`** — generic campaign template. Props:
`{ preheader, headline, sections: [{heading?, paragraphs: string[]}], cta?: {label, url},
firstName?, unsubUrl }`. Uses existing `Layout`.

**2. Schema migration `0002_bot.sql`:**
- `campaigns.content jsonb not null default '{}'` — template props written by the bot;
  dispatch merges `content` + per-contact props at render time (contact props win).
- `bot_sessions (thread_ts text pk, channel text, campaign_id uuid, messages jsonb,
  updated_at)` — conversation history per Slack thread.
- Campaign status gains `scheduled` (between approval and dispatching).

**3. `app/routers/slack.py`** — two endpoints:
- `POST /slack/events`: Slack URL-verification handshake; signature verification
  (HMAC v0 scheme, `SLACK_SIGNING_SECRET`); drop events that aren't from
  `SLACK_CHANNEL_ID`; dedupe retries (Slack retries on non-2xx / >3s); enqueue
  `bot_turn`; return 200 fast.
- `POST /slack/interactions`: signature check; handle `approve_send` (with schedule
  time from the button value) and `cancel` actions; update the original message via
  `chat.update`.

**4. `app/services/slack_client.py`** — thin Slack Web API client (httpx):
`post_message`, `update_message`. Reuses `RateLimiter` + retry pattern from `ghl.py`.

**5. `app/services/bot.py`** — the Claude conversation engine, run by the worker:
- `process_bot_turns(pool, settings)` drains `bot_turn` jobs.
- System prompt: role, the fixed workflow (audience → subject/copy → seed test →
  approval), guardrails (never dispatch directly; only offer the Send button after a
  seed test on this campaign; audience comes from GHL tag filters).
- Tools (Claude tool-use): `list_ghl_tags`, `create_campaign(name, subject, tag,
  content)`, `update_campaign_content`, `sync_audience`, `send_seed_test`,
  `get_report`, `propose_send(campaign_id, when_iso)` — the last posts the Block Kit
  approval message (Send/Cancel buttons) rather than sending anything itself.
- Model: latest Sonnet (`claude-sonnet-5`); consult the claude-api skill at
  implementation time for exact API usage.
- History capped (last ~30 messages) to bound tokens.

**6. Worker changes (`app/worker.py`):**
- each tick: promote scheduled campaigns
  (`update campaigns set status='dispatching' where status='scheduled' and scheduled_at <= now()`),
  then `process_bot_turns` alongside existing passes.

**7. Dispatch changes (`app/services/dispatch.py`):** merge `campaigns.content` into
render props: `props = {**content, firstName, lastName, **custom, unsubUrl}`.

**8. Config additions:** `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`,
`ANTHROPIC_API_KEY` (all required when bot enabled; `SLACK_ENABLED=true` flag gates the
whole feature so the pipeline runs without Slack config).

## Error handling

- Slack events endpoint never blocks: signature fail → 401; anything else → 200 + log
  (Slack retries aggressively; a 200 with internal enqueue failure is logged and the
  user retags the bot).
- Claude/tool errors inside a bot turn → post an apologetic message with the error
  summary into the thread; job marked completed (no infinite retry loops on bad input);
  infra errors (DB down) → job retries via existing backoff.
- Send button clicked twice / after cancel → interaction handler checks campaign
  status first; stale clicks get an "already handled" ephemeral reply.

## Testing

- Slack signature verification: unit tests with synthetic signed payloads (same
  pattern as svix tests).
- Events endpoint: URL-verification handshake, wrong-channel drop, dedupe, job enqueue.
- Bot engine: fake Anthropic client (scripted tool calls) exercising the tool
  dispatcher end-to-end against the test DB; no live API calls in tests.
- Interactions: approve → campaign `scheduled` with correct `scheduled_at`; cancel;
  stale-click handling.
- Worker promotion: scheduled campaign flips to dispatching when due.
- Newsletter template: node render test (sections, CTA, personalization, unsub).

## Operator setup (Ryan)

1. Create Slack app from a provided manifest (app_mention + message.channels events →
   `https://growthableemail.onrender.com/slack/events`; interactivity →
   `/slack/interactions`; scopes: `app_mentions:read`, `channels:history`,
   `groups:history`, `chat:write`). Install to workspace.
2. Create private channel (e.g. `#email-campaigns`), invite the bot, copy channel ID.
3. Provide `SLACK_BOT_TOKEN` (xoxb-…), `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`,
   `ANTHROPIC_API_KEY` → Render env on both services.

## Out of scope (v1)

Multi-channel support; per-user permissions; editing sent campaigns; A/B tests;
bespoke template generation from Slack; DMs with the bot.
