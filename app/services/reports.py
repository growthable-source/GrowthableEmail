async def campaign_report(pool, campaign_id) -> dict:
    campaign = await pool.fetchrow(
        "select id, name, status from campaigns where id=$1", campaign_id)
    if campaign is None:
        raise ValueError(f"campaign {campaign_id} not found")
    sends = await pool.fetchrow(
        """select count(*) as total,
                  count(*) filter (where status='sent') as sent,
                  count(*) filter (where status='queued') as queued,
                  count(*) filter (where status='failed') as failed,
                  count(*) filter (where status='suppressed') as suppressed
           from sends where campaign_id=$1""", campaign_id)
    events = await pool.fetch(
        """select e.type, count(distinct e.send_id) as n
           from events e join sends s on s.id = e.send_id
           where s.campaign_id=$1 group by e.type""", campaign_id)
    return {
        "campaign": {"id": str(campaign["id"]), "name": campaign["name"],
                     "status": campaign["status"]},
        "sends": dict(sends),
        "events": {r["type"]: r["n"] for r in events},
    }
