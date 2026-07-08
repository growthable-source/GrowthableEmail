"""GHL owns the actual publish of a scheduled social post; we only know our own
clock, which is the same one we told GHL to use. Flag posts whose schedule has
arrived so the worker can @channel-ping once, without claiming certainty GHL
succeeded (the interaction handler's error path already surfaces publish failures
for immediate posts; scheduled failures would only show up in GHL itself)."""


async def notify_due_social_posts(pool) -> list:
    rows = await pool.fetch(
        """update social_posts set notified_at=now()
           where status='scheduled' and schedule_at <= now() and notified_at is null
           returning id""")
    return [r["id"] for r in rows]
