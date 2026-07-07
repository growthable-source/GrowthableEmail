# GHL ↔ Resend Email Pipeline — Build Spec (Option B)

GHL remains the CRM and source of truth for contacts, tags, and DND. Resend is the sending engine. Claude Code designs emails in React Email. A thin FastAPI service on Render + Supabase orchestrates audience pull, rendering, batch dispatch, event write-back, and bidirectional suppression sync.

---

## 1. Architecture

```
GoHighLevel (CRM)                     Resend (delivery)
   │  ▲                                  │  ▲
   │  │ tags / notes / DND          webhooks │ batch send
   ▼  │                                  ▼  │
┌─────────────────────────────────────────────┐
│  FastAPI service (Render)                   │
│  - /campaigns  /webhooks/resend  /webhooks/ghl
│  - render worker (react-email → HTML)       │
│  - dispatch queue (pg-boss pattern)         │
└─────────────────────────────────────────────┘
   │  ▲
   ▼  │
Supabase (Postgres)
  campaigns, sends, events, suppressions, contacts_cache
```

## 2. Domain & warm-up

- **Reuse the existing warm root domain.** Domain-level reputation (DKIM alignment, Gmail domain reputation) transfers. IP reputation and return-path do not — Resend's IPs are new to your traffic.
- **New sending subdomain for Resend** (e.g. `news.yourdomain.io`). Keep GHL/LC-Email on its existing subdomain. Never point two ESPs at the same subdomain's DKIM/SPF.
- Verify subdomain in Resend: add their SPF, DKIM, and custom return-path records.
- **Compressed ramp (warm domain, cold infrastructure):**

  | Day | Max sends | Segment |
  |---|---|---|
  | 1–2 | 500/day | most engaged / most recent contacts |
  | 3–4 | 2,000/day | engaged |
  | 5–7 | 5,000–10,000/day | broaden |
  | 8+ | full volume | remainder, engagement-sorted |

- Kill rule: pause ramp if bounce rate > 3% or complaint rate > 0.1% on any day.
- Never-emailed contacts are cold recipients regardless of domain warmth — sort by recency/engagement proxy and send best cohorts first.

## 3. Contacts out of GHL

Two ingestion modes, both supported:

**a) Bulk audience pull (broadcasts)**
- API v2 `POST /contacts/search` with filters mirroring the smart list (tags, custom fields, pipeline stage).
- Paginate (`pageLimit` 100), hydrate into `contacts_cache` with: `ghl_contact_id`, `email`, `first_name`, `last_name`, custom fields needed for personalization, `dnd` flags, `tags[]`.
- Drop at ingest: `dnd = true`, missing email, invalid syntax, anything in `suppressions`.

**b) Per-contact trigger (behavioral)**
- GHL workflow → Webhook action → `POST /webhooks/ghl/enroll` with contact payload.
- Service validates against suppressions, enqueues single send or drip enrollment.

Auth: GHL Private Integration token (v2), scoped to contacts read/write + workflows.

## 4. Templates — React Email

- Dedicated repo (or `/emails` package in the pipeline monorepo). One component per campaign/template, shared layout + brand primitives (header, footer, button, typography tokens).
- Props are the personalization contract: `{ firstName, ...customFields }`. No GHL merge-field syntax anywhere — personalization happens your side at render time.
- Render pipeline: `@react-email/render` → HTML + auto-generated plain-text part. Version templates (`template_ref` + `template_version` on the campaign row) so a campaign's rendered output is reproducible.
- Store a `rendered_hash` per send for idempotency and debugging.
- Every template must include: unsubscribe link (`{{unsub_url}}` prop → signed `/u/{token}`), physical address footer, preheader text.
- Preview flow: `POST /campaigns/{id}/test` renders with seed contact data and sends to the seed list before any dispatch.

## 5. Dispatch worker

- Queue on Supabase (pg-boss pattern). Batch via Resend batch endpoint (up to 100/call) or single sends with an RPS limiter (`SEND_RPS`, default 2 until Resend limit increase approved).
- Idempotency: `unique(campaign_id, ghl_contact_id)` on `sends`; skip anything already `sent`/`delivered`.
- Suppression check again at dispatch time (not just ingest) — the list moves between sync and send.
- Daily cap enforced in code (`DAILY_SEND_CAP`) per the §2 ramp schedule; worker stops when cap hit and resumes next day.
- Headers on every send: `List-Unsubscribe` (mailto + one-click URL), `List-Unsubscribe-Post: List-Unsubscribe=One-Click`.
- Failure handling: transient errors retry with backoff (max 3); hard failures mark send `failed` with reason.

## 6. Event write-back (Resend → GHL)

- `POST /webhooks/resend` receives `email.sent`, `email.delivered`, `email.opened`, `email.clicked`, `email.bounced`, `email.complained`. Verify Svix signature (`RESEND_WEBHOOK_SECRET`), reject on failure.
- Persist raw payload to `events`, then enqueue GHL write-back:
  - `delivered/opened/clicked` → add tags (`emailed-{campaign}`, `opened-{campaign}`, `clicked-{campaign}`) so GHL automations can trip off tag changes.
  - `bounced` (hard) → add to `suppressions` (reason `hard_bounce`), set GHL DND for email channel.
  - `complained` → add to `suppressions` (reason `complaint`), set GHL DND, tag `complained`.
- GHL write-back worker is rate-limited and retried independently — a GHL outage must never block webhook ingestion.

## 7. Suppression sync (bidirectional, Supabase canonical)

- **Resend → GHL:** bounces/complaints per §6.
- **GHL → pipeline:** GHL workflow on DND/unsubscribe → `POST /webhooks/ghl/dnd` → upsert into `suppressions` (reason `ghl_dnd`).
- **One-click unsub:** `GET /u/{token}` (HMAC-signed with `UNSUB_SIGNING_SECRET`, payload = email + campaign) → upsert suppression (reason `unsubscribe`), push DND to GHL, render confirmation page.
- `suppressions` in Supabase is the single canonical store. Resend audiences and GHL DND are mirrors. Every audience sync and every dispatch checks it.

## 8. Data model (Supabase)

```sql
campaigns        (id, name, template_ref, template_version, status, scheduled_at, created_at)
contacts_cache   (ghl_contact_id pk, email, first_name, last_name, custom jsonb, tags text[], dnd bool, synced_at)
sends            (id, campaign_id, ghl_contact_id, email, resend_email_id, status, rendered_hash, sent_at,
                  unique(campaign_id, ghl_contact_id))
events           (id, send_id, type, payload jsonb, occurred_at)
suppressions     (email pk, ghl_contact_id, reason, source, created_at)
jobs             (pg-boss standard schema)
```

## 9. Service endpoints

```
POST /campaigns                     create campaign (template ref + audience filter)
POST /campaigns/{id}/sync-audience  pull from GHL contacts/search
POST /campaigns/{id}/test           render + send to seed list
POST /campaigns/{id}/dispatch       enqueue batches (respects ramp caps)
POST /webhooks/resend               Resend events (Svix-verified)
POST /webhooks/ghl/enroll           per-contact behavioral trigger
POST /webhooks/ghl/dnd              GHL DND/unsub sync
GET  /u/{token}                     one-click unsubscribe
GET  /campaigns/{id}/report         delivered/open/click/bounce/complaint rollup
```

## 10. Config & secrets (Render env)

```
RESEND_API_KEY
RESEND_WEBHOOK_SECRET
GHL_PI_TOKEN                # Private Integration token
GHL_LOCATION_ID
SUPABASE_URL / SUPABASE_SERVICE_KEY
UNSUB_SIGNING_SECRET
SEND_RPS=2                  # raise after Resend limit increase
DAILY_SEND_CAP              # ramp control, adjust per §2
```

## 11. Build order

1. Supabase schema + migrations; FastAPI skeleton on Render.
2. Resend subdomain verification + test send via API.
3. GHL Private Integration token; `contacts/search` pull into `contacts_cache`.
4. React Email repo; first campaign component; render pipeline.
5. Dispatch worker with batch endpoint, idempotency, RPS limiter, daily cap.
6. Resend webhook handler + Svix verification; events table.
7. GHL write-back worker (tags/DND/notes) with rate limiting.
8. Unsubscribe endpoint + GHL DND inbound webhook → suppressions complete.
9. Seed-list test campaign end-to-end; verify headers, unsub, event flow.
10. Ramp per §2 schedule with kill rules monitored daily.

## 12. Guardrails

- Suppression check at ingest **and** dispatch.
- No send without `List-Unsubscribe` one-click headers.
- Ramp caps enforced in code (`DAILY_SEND_CAP`), not by discipline.
- Complaint rate > 0.1% or hard bounce > 3% on a day → auto-pause dispatch, alert.
- One canonical suppression store (Supabase); Resend and GHL are mirrors.
- SPF/DKIM on separate subdomains per ESP — never shared.
