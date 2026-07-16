# GrowthableEmail — GHL ↔ Resend pipeline

FastAPI + Supabase service that pulls audiences from GoHighLevel, renders React Email
templates, dispatches via Resend, writes events back to GHL, and keeps a canonical
suppression list. Spec: docs/spec.md. Runbook: see bottom of this file.

## Dev setup
    uv sync
    (cd emails && npm install)
    docker start growthable-test-pg || docker run -d --name growthable-test-pg \
      -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
    uv run pytest

## Runbook

### One-time setup (build order §11 of docs/spec.md)
1. **Supabase:** create project → run `supabase/migrations/0001_init.sql` in the SQL editor
   (or `supabase db push`). Grab the *connection pooler* URL → `DATABASE_URL`.
2. **Resend domain:** add sending subdomain (e.g. `news.growthable.io`) in Resend →
   add the SPF, DKIM and return-path DNS records they show → verify. NEVER point Resend
   at the subdomain GHL/LC-Email uses (spec §2/§12).
3. **Render:** connect this repo — Render reads `render.yaml` (web + worker, one Docker
   image). Fill in the secret env vars on both services.
4. **Resend webhook:** dashboard → Webhooks → add `https://<api>/webhooks/resend`,
   subscribe to sent/delivered/opened/clicked/bounced/complained → copy the signing
   secret → `RESEND_WEBHOOK_SECRET`.
5. **GHL Private Integration:** Settings → Private Integrations → create with
   contacts read/write scope → `GHL_PI_TOKEN`, `GHL_LOCATION_ID`.
6. **GHL workflows:**
   - DND/unsub sync: workflow on "DND enabled / unsubscribed" → Webhook action →
     POST `https://<api>/webhooks/ghl/dnd` with header `x-webhook-secret: <GHL_WEBHOOK_SECRET>`
     and body `{"email": "{{contact.email}}", "contact_id": "{{contact.id}}"}`.
   - Behavioral enroll: workflow → Webhook action → POST `https://<api>/webhooks/ghl/enroll`
     with the same header and body
     `{"campaign_id": "<uuid>", "contact_id": "{{contact.id}}", "email": "{{contact.email}}", "first_name": "{{contact.first_name}}"}`.
7. **Templates:** replace the placeholder physical address in `emails/components/Layout.tsx`.

### Campaign flow
    POST /campaigns                      {name, subject, template_ref, template_version, audience_filter}
    POST /campaigns/{id}/sync-audience   pulls from GHL, applies ingest drop rules
    POST /campaigns/{id}/test            renders + sends to SEED_EMAILS — check headers, unsub, rendering
    POST /campaigns/{id}/dispatch        ops fallback: fills the per-email queue (capped drip)
    GET  /campaigns/{id}/report          delivered/open/click/bounce/complaint rollup

### Broadcast sends (all at once)
Campaigns approved through the Slack Send button without a ramp go out as a
**single Resend Broadcast**: the worker bulk-imports the synced audience (CSV,
one call — handles 50k+ contacts) into a per-campaign Resend segment, then
creates the broadcast with `send: true` once the import completes.
Personalization uses Resend merge tags (`{{{contact.first_name|there}}}`) and
Resend hosts the unsubscribe flow (`{{{RESEND_UNSUBSCRIBE_URL}}}`); unsubscribes
flow back as `contact.updated` webhook events into our suppression list.
DAILY_SEND_CAP does **not** apply to broadcasts — only to the per-email drip
queue (GHL enrollments, seed tests).
Setup: apply `supabase/migrations/0006_broadcasts.sql` and subscribe the Resend
webhook to the **contact.updated** event (in addition to the email.* events).

### Ramped, timezone-targeted sends (per-day / per-hour)
Tell the bot a per-day and/or per-hour amount when approving and the campaign is
dispatched through the queue instead: each contact is scheduled for the next
occurrence of `IDEAL_SEND_HOUR` (default 10am) **local time**, resolved from the
GHL contact's `timezone` field, else its `country` (representative zone per
country), else assumed US (America/Chicago). The worker throttles each ramped
campaign by its own `per_day`/`per_hour` (independent of the global
DAILY_SEND_CAP) and sends missing their local window by more than 8 hours roll
to the next day's window — so a per-hour-only ramp moves roughly 8×per_hour per
day. `sync-audience` returns the country breakdown so the bot can discuss
delivery timing. Setup: apply `supabase/migrations/0007_timed_sends.sql` and
re-run sync-audience so contacts pick up country/timezone. Throughput note:
queue sends respect SEND_RPS (2/s ≈ 7,200/hour ceiling) — ask Resend for a rate
increase before very aggressive ramps.

### Ramp schedule (spec §2 — adjust DAILY_SEND_CAP on the worker, then redeploy)
| Day | DAILY_SEND_CAP | Segment |
|---|---|---|
| 1–2 | 500 | most engaged / most recent |
| 3–4 | 2000 | engaged |
| 5–7 | 5000–10000 | broaden |
| 8+ | full volume | remainder, engagement-sorted |

Kill rule (automatic): bounce > 3% or complaint > 0.1% on ≥200 sends/day pauses all
dispatching campaigns and posts to ALERT_WEBHOOK_URL. Un-pause by fixing the cause and
setting campaign status back to 'dispatching' in Supabase. Check the report endpoint daily
during ramp. SEND_RPS stays 2 until Resend approves a rate increase.

### Slack bot
Create the Slack app from this manifest (api.slack.com/apps → Create New App → From manifest):

    display_information:
      name: Growthable Email
    features:
      bot_user:
        display_name: email-bot
        always_online: true
    oauth_config:
      scopes:
        bot: [app_mentions:read, channels:history, groups:history, chat:write]
    settings:
      event_subscriptions:
        request_url: https://growthableemail.onrender.com/slack/events
        bot_events: [app_mention, message.channels, message.groups]
      interactivity:
        is_enabled: true
        request_url: https://growthableemail.onrender.com/slack/interactions

Install to the workspace → copy the Bot User OAuth Token (SLACK_BOT_TOKEN) and, from
Basic Information, the Signing Secret (SLACK_SIGNING_SECRET). Create a private channel
(e.g. #email-campaigns), /invite the bot, and copy the channel ID from the channel's
details pane (SLACK_CHANNEL_ID). Set SLACK_ENABLED=true + ANTHROPIC_API_KEY on both
Render services. Apply supabase/migrations/0002_bot.sql.

Usage: tag @email-bot in the channel and describe the campaign. It will confirm the
audience tag, draft copy, seed-test to SEED_EMAILS, and post Send/Cancel buttons.
The seed test is mandatory; approved sends go out as one Resend Broadcast
(uncapped — plan limit applies); kill rules still pause everything on bounce spikes.

### Social media bot
Same Slack app, second channel. The bot drafts posts (brand voice §1-3 of the email
guide), generates images (Gemini), and publishes/schedules via GHL Social Planner
after a Publish button click.

Setup:
1. Add **View/Edit Social Planner** scopes to the GHL PIT.
2. Create a private channel (e.g. #social-posts), `/invite @email-bot`, copy its
   channel ID → `SLACK_SOCIAL_CHANNEL_ID` on both Render services.
3. Get a Gemini API key (aistudio.google.com) → `GEMINI_API_KEY` on both services.
4. Apply `supabase/migrations/0003_social.sql`.
5. Connect social accounts in GHL → Marketing → Social Planner (the bot posts to
   whatever is connected).

Usage: tag the bot in the social channel ("draft a LinkedIn post about X with an
image"). It confirms target accounts, drafts, shows the image, and posts
Publish/Cancel buttons. Scheduled posts land in GHL Social Planner where they can
also be edited or deleted.

### Daily digest
Each configured channel gets a once-a-day summary (not @channel-tagged — routine,
not urgent) after `DAILY_REPORT_HOUR` local time (`BOT_TIMEZONE`, default 8am):
the email channel gets sent/delivered/opened/clicked/bounced counts plus any
guardrail-paused campaigns; the social channel gets published/scheduled/cancelled
counts and what's coming up in the next 24h. No setup needed beyond the channels
already configured for the bots.

### Go-live checklist (spec §11.9)
- [ ] Seed test: `POST /campaigns/{id}/test` → inspect in Gmail: DKIM=news subdomain pass,
      List-Unsubscribe header present, one-click unsub works, plain-text part present,
      physical address in footer.
- [ ] Unsub flow: click footer link → confirmation page → suppression row + GHL DND set.
- [ ] Event flow: open/click the seed email → tags appear on the GHL contact.
- [ ] First real cohort: engagement-sorted top 500, DAILY_SEND_CAP=500.
