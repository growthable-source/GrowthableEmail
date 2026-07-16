import json
import uuid

from app.services.audience import sync_audience
from app.services.suppressions import add_suppression


class FakeGHL:
    def __init__(self, contacts):
        self._contacts = contacts

    async def search_contacts(self, filters, page_limit=100):
        for c in self._contacts:
            yield c


def contact(cid, email, **kw):
    return {"ghl_contact_id": cid, "email": email, "first_name": kw.get("first_name", ""),
            "last_name": kw.get("last_name", ""), "tags": kw.get("tags", []),
            "dnd": kw.get("dnd", False), "custom": kw.get("custom", {}),
            "country": kw.get("country", ""), "timezone": kw.get("timezone", ""),
            "search_after": None}


async def make_campaign(pool) -> str:
    return str(await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, audience_filter) "
        "values ('launch', 'Hello', 'welcome', 'v1', $1) returning id",
        json.dumps([{"field": "tags", "operator": "eq", "value": "newsletter"}]),
    ))


async def test_sync_drops_dnd_missing_invalid_and_suppressed(pool):
    campaign_id = await make_campaign(pool)
    await add_suppression(pool, "sup@x.co", reason="complaint", source="resend")
    ghl = FakeGHL([
        contact("c1", "good@x.co", first_name="Ada", custom={"f1": "gold"}),
        contact("c2", ""),                        # missing email
        contact("c3", "not-an-email"),            # invalid syntax
        contact("c4", "dnd@x.co", dnd=True),      # GHL DND
        contact("c5", "sup@x.co"),                # suppressed
    ])
    result = await sync_audience(pool, ghl, campaign_id)
    assert result["kept"] == 1 and result["dropped"] == 4
    row = await pool.fetchrow("select * from contacts_cache where ghl_contact_id='c1'")
    assert row["email"] == "good@x.co"
    assert json.loads(row["custom"]) == {"f1": "gold"}
    linked = await pool.fetch("select ghl_contact_id from campaign_contacts where campaign_id=$1",
                              uuid.UUID(campaign_id))
    assert [r["ghl_contact_id"] for r in linked] == ["c1"]


async def test_sync_reports_country_breakdown_and_timezones(pool):
    campaign_id = await make_campaign(pool)
    ghl = FakeGHL([
        contact("c1", "a@x.co", country="US"),
        contact("c2", "b@x.co", country="US", timezone="America/New_York"),
        contact("c3", "c@x.co", country="AU"),
        contact("c4", "d@x.co"),  # no country → 'unknown' bucket
    ])
    result = await sync_audience(pool, ghl, campaign_id)
    assert result["countries"] == {"US": 2, "AU": 1, "unknown": 1}
    assert result["with_timezone"] == 1
    row = await pool.fetchrow(
        "select country, timezone from contacts_cache where ghl_contact_id='c2'")
    assert row["country"] == "US" and row["timezone"] == "America/New_York"


async def test_resync_updates_existing_contact(pool):
    campaign_id = await make_campaign(pool)
    ghl1 = FakeGHL([contact("c1", "old@x.co")])
    await sync_audience(pool, ghl1, campaign_id)
    ghl2 = FakeGHL([contact("c1", "new@x.co", first_name="Ada")])
    result = await sync_audience(pool, ghl2, campaign_id)
    assert result["kept"] == 1
    row = await pool.fetchrow("select email, first_name from contacts_cache where ghl_contact_id='c1'")
    assert row["email"] == "new@x.co" and row["first_name"] == "Ada"
