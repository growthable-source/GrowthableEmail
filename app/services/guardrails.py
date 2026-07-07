import logging

import httpx

log = logging.getLogger(__name__)

BOUNCE_RATE_LIMIT = 0.03      # spec §2/§12: hard bounce > 3% on a day
COMPLAINT_RATE_LIMIT = 0.001  # spec §2/§12: complaint > 0.1% on a day
MIN_DAILY_VOLUME = 200        # don't trip on statistically meaningless samples


async def check_and_pause(pool, alert_webhook_url: str | None = None) -> bool:
    """Kill rule: on breach, pause all dispatching campaigns and alert. True if breached."""
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

    paused = await pool.execute(
        "update campaigns set status='paused' where status='dispatching'")
    message = (
        f"KILL RULE TRIPPED: bounce_rate={bounce_rate:.4f} (limit {BOUNCE_RATE_LIMIT}), "
        f"complaint_rate={complaint_rate:.4f} (limit {COMPLAINT_RATE_LIMIT}), "
        f"sent_today={sent}. Dispatch paused ({paused})."
    )
    log.error(message)
    if alert_webhook_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(alert_webhook_url, json={"text": message})
        except httpx.HTTPError:
            log.exception("alert webhook failed")
    return True
