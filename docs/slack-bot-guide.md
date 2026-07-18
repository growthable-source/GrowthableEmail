# Email Bot — Slack Guide

How to drive the GHL→Resend email pipeline from #marketing-manager.
(The social bot in the social channel works the same way; this guide covers email.)

## The one rule that bites people

**Talk to the bot in a thread, as a plain reply.** Do NOT tick
*"Also send to #marketing-manager"* when replying — Slack marks those messages as
broadcasts and the bot deliberately ignores them (it filters out message subtypes
to avoid echo loops). If the bot seems deaf, this is the first thing to check.

Every message in the channel starts or continues a conversation — you don't need
to @mention the bot, but it helps humans follow along.

## Campaign lifecycle (what happens in what order)

1. **Draft** — describe the campaign; the bot writes the HTML per the brand guide
   and shows you copy in chat. Iterate until happy. (`create_campaign` /
   `update_campaign` under the hood.)
2. **Audience** — the bot pulls contacts from a GHL tag (`sync_audience`) and
   reports size + country breakdown. Changing content later resets the seed-test
   requirement.
3. **Verification** — runs automatically after every audience sync. Each email is
   checked once with Emailable, EVER — verdicts are permanent and never re-billed.
   - ≤1,000 unverified: runs automatically, no approval.
   - >1,000: the bot posts a **Verify / Not now** card with the cost estimate —
     a human must click Verify (that's the spend gate).
   - Progress posts at 25/50/75%, then a ✅ completion post with the
     valid/invalid/risky split.
   - Ask **"verification status"** any time for the live counts.
4. **Seed test** — mandatory. The bot sends `[TEST]` copies to the seed list;
   check your inbox and confirm before anything else can happen.
5. **Propose** — say **"propose the send"** (optionally "at 5000/day, 600/hour"
   for a ramp, and/or a scheduled time). The bot posts the approval card.
6. **Send** — a human clicks **Send** on the card. The bot can never dispatch on
   its own. Broadcast = everything at once; ramp = per_day/per_hour caps, each
   contact targeted at ~10am their local time.
7. **Launch confirmation** — when the queue physically fills, the worker posts
   `🚀 <name> is LAUNCHED — N recipients queued…` with the first-wave time.
   If you don't see this, the campaign is NOT sending yet.
8. **Progress** — the 8am daily digest shows each running ramp:
   `📤 <name> ramp: 12,400/24,032 sent (51%)…` plus the usual
   sent/delivered/bounced numbers.

## Who gets emailed (the hard rules)

An address must pass ALL of these on every send path, no exceptions:
- **verified-valid** (Emailable verdict `valid`; risky/invalid/unknown excluded)
- not suppressed (no prior hard bounce, complaint, or unsubscribe)
- not DND in GHL

Role accounts (`info@`, `sales@`…) and catch-all domains count as **risky** and
are excluded. They're kept with their verdicts, so this policy can be changed
deliberately later.

## Tags written back to GHL

| Tag | Meaning | Use in GHL |
|---|---|---|
| `email-invalid` | Mailbox confirmed dead or disposable | Exclude from everything |
| `email-risky` | Catch-all / role / unverifiable | Don't trust for email; SMS may still work |
| `emailed-<campaign>` / `opened-<campaign>` / `clicked-<campaign>` | Engagement | Segmentation |
| `complained` | Spam complaint (also sets DND) | Never contact |

## Phrases the bot understands (→ tool)

- "what tags do we have" → `list_ghl_tags`
- "create a campaign that…" → `create_campaign`
- "change the subject / rewrite section X" → `update_campaign`
- "sync the audience" → `sync_audience` (auto-starts verification)
- "verification status" → `verification_status`
- "send the seed test" → `send_seed_test`
- "how did campaign X do" → `get_report`
- "build a high-intent segment from the last 90 days" → `build_engaged_segment`,
  then "how's the tagging going" → `segment_progress`
- "propose the send [at N/day, M/hour] [at TIME]" → `propose_send` (posts the card)

## Automatic messages you'll see

- **8:00am daily digest** — last-24h numbers, ramp progress, paused campaigns.
  Note: a "Paused by guardrails: X" line means the campaign NAMED X is paused.
- **⏳ / ✅ verification progress** — during large verifications.
- **🚀 LAUNCHED** — the queue is filled; sending begins per schedule.
- **⚠️ kill rule** — bounce rate >3% or complaint rate >0.1% on ≥200 sends/day
  auto-pauses ALL dispatching campaigns and alerts immediately.
- **⚠️ EMAILABLE_API_KEY missing** — verification approved but the worker can't
  run it (env var missing on growthable-email-worker in Render).

## When things are stuck (escape hatches)

These are Supabase SQL editor operations — use when the bot/buttons are down.

Un-pause a guardrail-paused campaign (then re-propose + click Send):
```sql
update campaigns set status='ready' where id='<CAMPAIGN_ID>' and status='paused';
```

Launch directly (equivalent to clicking Send on a 5000/600 ramp):
```sql
update campaigns set status='dispatching', send_via='timed',
       per_day=5000, per_hour=600
where id='<CAMPAIGN_ID>' and status='ready';
```

Force a specific email to be re-verified next sync (costs one credit):
```sql
delete from email_verifications where email='someone@example.com';
```

Campaign IDs: the bot includes them when it creates campaigns, or
`select id, name, status from campaigns order by created_at desc;`

## Infrastructure map (for debugging)

- **Web** (`growthableemail.onrender.com`): Slack events/buttons, Resend + GHL
  webhooks, unsubscribe pages. If buttons don't respond, check this service.
- **Worker** (`growthable-email-worker`): everything else — bot replies, sending,
  verification, GHL write-backs, digests. If the bot is silent, check this
  service's logs. Both deploy automatically from `main`.
- Events from `notifications.voxility.ai` in the logs are the Xovera platform
  sharing the Resend account — different domain, harmless to Growthable sends.
