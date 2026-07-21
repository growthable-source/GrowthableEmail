import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.services.jobs import complete_job, enqueue, fail_job, fetch_job

log = logging.getLogger(__name__)

BOUNCE_RATE_LIMIT = 0.03      # spec §2/§12: hard bounce > 3% on a day
COMPLAINT_RATE_LIMIT = 0.001  # spec §2/§12: complaint > 0.1% on a day
# Don't trip on statistically meaningless samples. At n=200 a single bounce moves
# the rate half a point, so a list running a healthy ~1.8% crosses 3% on noise
# alone — which is exactly what stalled the July ramp three times (2026-07-17/18/21),
# a full day lost each time while its uninterrupted days measured 1.6-1.9%.
# n=1000 makes 3% ~4 standard deviations out for such a list: real breaches still
# trip, noise doesn't. The rate limits themselves are unchanged.
MIN_DAILY_VOLUME = 1000

MAX_AUTO_RESUMES = 3          # circuit breaker: unattended retries before a human
MIN_PER_HOUR = 50             # rate never backs off below this


def _seconds_until_counter_reset(now: datetime | None = None) -> int:
    """The kill rule's counters are per-UTC-day; resuming before they reset would
    re-trip instantly on yesterday's bounces. Resume at 00:30 UTC — still hours
    before the 10am-local US wave, so no recipient sees a delay."""
    now = now or datetime.now(timezone.utc)
    target = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0,
                                               microsecond=0)
    return int((target - now).total_seconds())


async def check_and_pause(pool, alert_webhook_url: str | None = None,
                          slack=None, channel: str = "") -> bool:
    """Kill rule: on breach, pause all dispatching campaigns and alert. True if breached.

    Alerts fire only when campaigns were actually paused this call — the breach
    persists in the day's rates for hours, and the worker re-checks every tick."""
    stats = await pool.fetchrow(
        """select
               (select count(*) from sends where sent_at >= date_trunc('day', now())) as sent,
               (select count(distinct send_id) from events
                where type='email.bounced' and occurred_at >= date_trunc('day', now())) as bounced,
               (select count(distinct send_id) from events
                where type='email.complained' and occurred_at >= date_trunc('day', now())) as complained""")
    sent, bounced, complained = stats["sent"], stats["bounced"], stats["complained"]
    if sent < MIN_DAILY_VOLUME:
        return False
    bounce_rate = bounced / sent
    complaint_rate = complained / sent
    if bounce_rate <= BOUNCE_RATE_LIMIT and complaint_rate <= COMPLAINT_RATE_LIMIT:
        return False

    paused = await pool.fetch(
        "update campaigns set status='paused' where status='dispatching' returning name")
    if not paused:
        return True  # already paused a previous tick — nothing new to announce
    names = ", ".join(r["name"] for r in paused)
    message = (
        f"KILL RULE TRIPPED: bounce_rate={bounce_rate:.4f} (limit {BOUNCE_RATE_LIMIT}), "
        f"complaint_rate={complaint_rate:.4f} (limit {COMPLAINT_RATE_LIMIT}), "
        f"sent_today={sent}. Paused: {names}."
    )
    log.error(message)
    if slack is not None and channel:
        try:
            await slack.post_message(
                channel,
                text=f"<!channel> 🛑 *Deliverability kill rule tripped* — "
                     f"bounce {bounce_rate:.1%} / complaint {complaint_rate:.2%} on "
                     f"{sent} sends today (limits {BOUNCE_RATE_LIMIT:.0%} / "
                     f"{COMPLAINT_RATE_LIMIT:.1%}). Paused: *{names}*. "
                     "Ramped campaigns auto-resume after the daily counters reset "
                     "(queue re-pruned, rate halved).")
        except Exception:
            log.exception("slack kill-rule alert failed")
    if alert_webhook_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(alert_webhook_url, json={"text": message})
        except httpx.HTTPError:
            log.exception("alert webhook failed")
    return True


async def ensure_auto_resume(pool) -> int:
    """Circuit-breaker reconciler: every paused *timed* campaign with sends still
    queued gets an auto_resume job scheduled for the counter reset — including
    campaigns paused before this feature existed. Capped at MAX_AUTO_RESUMES
    unattended attempts per 7 days; broadcast campaigns stay manual (a resumed
    broadcast would fire a stale imported segment)."""
    rows = await pool.fetch(
        """select c.id from campaigns c
           where c.status = 'paused' and c.send_via = 'timed'
             and exists (select 1 from sends s where s.campaign_id = c.id
                         and s.status = 'queued')
             and not exists (select 1 from jobs j where j.name = 'auto_resume'
                             and j.state in ('created', 'active')
                             and j.data->>'campaign_id' = c.id::text)
             and (select count(*) from jobs j2 where j2.name = 'auto_resume'
                  and j2.state = 'completed'
                  and j2.data->>'campaign_id' = c.id::text
                  and j2.completed_at > now() - interval '7 days') < $1""",
        MAX_AUTO_RESUMES)
    for r in rows:
        attempts = await pool.fetchval(
            "select count(*) from jobs where name='auto_resume' and state='completed' "
            "and data->>'campaign_id' = $1 and completed_at > now() - interval '7 days'",
            str(r["id"]))
        await enqueue(pool, "auto_resume",
                      {"campaign_id": str(r["id"]), "attempt": attempts + 1},
                      start_after_seconds=_seconds_until_counter_reset())
        log.info("auto-resume scheduled for paused campaign %s (attempt %s)",
                 r["id"], attempts + 1)
    return len(rows)


async def process_auto_resume(pool, slack=None) -> int:
    """Execute due auto_resume jobs: prune the queue to verified-valid, back the
    hourly rate off by half (reputation recovery), un-pause, and say so in the
    campaign thread. Every action is stated, none needs a human."""
    done = 0
    while (job := await fetch_job(pool, "auto_resume")) is not None:
        try:
            campaign = await pool.fetchrow(
                """select id, name, status, send_via, per_hour, channel, thread_ts
                   from campaigns where id = $1::uuid""", job["data"]["campaign_id"])
            if (campaign is None or campaign["status"] != "paused"
                    or campaign["send_via"] != "timed"):
                await complete_job(pool, job["id"])  # resolved some other way
                done += 1
                continue
            result = await pool.execute(
                """update sends set status='suppressed'
                   where campaign_id = $1 and status = 'queued'
                     and not exists (select 1 from email_verifications v
                                     where v.email = sends.email
                                       and v.verdict = 'valid')""",
                campaign["id"])
            pruned = int(result.split()[-1])
            new_per_hour = campaign["per_hour"]
            if new_per_hour:
                new_per_hour = max(MIN_PER_HOUR, new_per_hour // 2)
                await pool.execute(
                    "update campaigns set per_hour=$2 where id=$1",
                    campaign["id"], new_per_hour)
            await pool.execute(
                "update campaigns set status='dispatching' where id=$1 and status='paused'",
                campaign["id"])
            remaining = await pool.fetchval(
                "select count(*) from sends where campaign_id=$1 and status='queued'",
                campaign["id"])
            attempt = job["data"].get("attempt", 1)
            log.info("auto-resumed campaign %s: pruned=%s remaining=%s per_hour=%s "
                     "(attempt %s/%s)", campaign["id"], pruned, remaining,
                     new_per_hour, attempt, MAX_AUTO_RESUMES)
            if slack is not None and campaign["channel"]:
                rate = (f"rate backed off to {new_per_hour}/hour"
                        if new_per_hour else "no hourly cap set")
                await slack.post_message(
                    campaign["channel"],
                    text=f"🔁 *{campaign['name']}* auto-resumed after the guardrail "
                         f"pause (attempt {attempt}/{MAX_AUTO_RESUMES}): pruned "
                         f"{pruned} unverified from the queue, {remaining:,} verified "
                         f"remain, {rate}. If the kill rule trips again I'll back off "
                         "further and retry tomorrow; after "
                         f"{MAX_AUTO_RESUMES} attempts a human needs to look.",
                    thread_ts=campaign["thread_ts"])
        except Exception:
            log.exception("auto_resume job %s failed", job["id"])
            await fail_job(pool, job["id"])
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
