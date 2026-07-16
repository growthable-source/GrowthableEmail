import json
import re
import uuid

from app.services.suppressions import is_suppressed

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def sync_audience(pool, ghl, campaign_id: str) -> dict:
    """Pull the campaign's audience from GHL into contacts_cache + campaign_contacts.

    Drops at ingest (spec §3a): dnd, missing email, invalid syntax, suppressed.
    """
    cid = uuid.UUID(str(campaign_id))
    raw_filter = await pool.fetchval("select audience_filter from campaigns where id=$1", cid)
    if raw_filter is None:
        raise ValueError(f"campaign {campaign_id} not found")
    filters = json.loads(raw_filter)
    kept = dropped = 0
    async for c in ghl.search_contacts(filters):
        email = c["email"]
        if not email or not EMAIL_RE.match(email) or c["dnd"] or await is_suppressed(pool, email):
            dropped += 1
            continue
        await pool.execute(
            """insert into contacts_cache
                   (ghl_contact_id, email, first_name, last_name, custom, tags, dnd,
                    country, timezone, synced_at)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
               on conflict (ghl_contact_id) do update set
                   email = excluded.email, first_name = excluded.first_name,
                   last_name = excluded.last_name, custom = excluded.custom,
                   tags = excluded.tags, dnd = excluded.dnd,
                   country = excluded.country, timezone = excluded.timezone,
                   synced_at = now()""",
            c["ghl_contact_id"], email, c["first_name"], c["last_name"],
            json.dumps(c["custom"]), c["tags"], c["dnd"],
            c.get("country", ""), c.get("timezone", ""),
        )
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2) "
            "on conflict do nothing",
            cid, c["ghl_contact_id"],
        )
        kept += 1
    if kept:
        await pool.execute(
            "update campaigns set status='ready' where id=$1 and status='draft'", cid
        )
    # country/timezone breakdown so the bot can discuss send-time targeting
    rows = await pool.fetch(
        """select coalesce(nullif(c.country, ''), 'unknown') as country, count(*) as n
           from campaign_contacts cc join contacts_cache c using (ghl_contact_id)
           where cc.campaign_id = $1 group by 1 order by n desc""", cid)
    with_tz = await pool.fetchval(
        """select count(*) from campaign_contacts cc
           join contacts_cache c using (ghl_contact_id)
           where cc.campaign_id = $1 and c.timezone <> ''""", cid)
    return {"kept": kept, "dropped": dropped,
            "countries": {r["country"]: r["n"] for r in rows},
            "with_timezone": with_tz}
