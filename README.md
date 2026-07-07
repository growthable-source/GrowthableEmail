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
    POST /campaigns/{id}/dispatch        fills the queue; the worker drains it under caps
    GET  /campaigns/{id}/report          delivered/open/click/bounce/complaint rollup

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

### Go-live checklist (spec §11.9)
- [ ] Seed test: `POST /campaigns/{id}/test` → inspect in Gmail: DKIM=news subdomain pass,
      List-Unsubscribe header present, one-click unsub works, plain-text part present,
      physical address in footer.
- [ ] Unsub flow: click footer link → confirmation page → suppression row + GHL DND set.
- [ ] Event flow: open/click the seed email → tags appear on the GHL contact.
- [ ] First real cohort: engagement-sorted top 500, DAILY_SEND_CAP=500.
