"""Sending-domain pool: rotation, per-domain daily caps, auto-ramp, kill.

Empty table → everything behaves exactly as before (settings.from_email).
Ramp: a healthy domain's cap doubles weekly from 30 up to max_cap.
Kill: >3% hard bounces on today's sends (min 10) pauses the domain.
"""
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


async def pick_from(pool, settings) -> tuple[str, str | None]:
    """(from_email, domain) — least-utilized active domain under its cap."""
    rows = await pool.fetch(
        """select d.domain, d.from_user, d.from_name, d.daily_cap,
                  count(s.id) filter (where s.sent_at >= date_trunc('day', now())) as sent_today
           from sending_domains d
           left join sends s on s.from_domain = d.domain
           where d.active
           group by d.id
           having count(s.id) filter (where s.sent_at >= date_trunc('day', now())) < d.daily_cap
           order by count(s.id) filter (where s.sent_at >= date_trunc('day', now()))::float
                    / greatest(d.daily_cap, 1) asc
           limit 1""")
    if not rows:
        return settings.from_email, None
    d = rows[0]
    return f"{d['from_name']} <{d['from_user']}@{d['domain']}>", d["domain"]


async def adjust_and_guard(pool, settings) -> None:
    """Hourly: weekly cap doubling for healthy domains; pause on bounce spikes."""
    domains = await pool.fetch("select * from sending_domains where active")
    for d in domains:
        stats = await pool.fetchrow(
            """select
                 count(*) filter (where s.sent_at >= now() - interval '1 day') as sent_1d,
                 count(*) filter (where e.type = 'email.bounced'
                                  and s.sent_at >= now() - interval '1 day') as bounced_1d
               from sends s left join events e on e.send_id = s.id
               where s.from_domain = $1""", d["domain"])
        sent, bounced = stats["sent_1d"] or 0, stats["bounced_1d"] or 0
        if sent >= 10 and bounced / sent > 0.03:
            await pool.execute(
                "update sending_domains set active=false, paused_reason=$2 where id=$1",
                d["id"], f"bounce rate {bounced}/{sent} on {datetime.now(timezone.utc).date()}")
            log.warning("paused sending domain %s (%s/%s bounced)", d["domain"], bounced, sent)
            if settings.alert_webhook_url:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(settings.alert_webhook_url, json={"text":
                        f"⛔ Sending domain {d['domain']} paused: {bounced}/{sent} bounced today"})
            continue
        weeks = max(0, (datetime.now(timezone.utc) -
                        d["created_at"]).days // 7)
        target = min(30 * (2 ** weeks), d["max_cap"])
        if target > d["daily_cap"]:
            await pool.execute(
                "update sending_domains set daily_cap=$2 where id=$1", d["id"], target)
            log.info("ramped %s daily_cap -> %s", d["domain"], target)
