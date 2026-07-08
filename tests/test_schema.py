async def test_all_tables_exist(pool):
    rows = await pool.fetch(
        "select table_name from information_schema.tables where table_schema='public'"
    )
    names = {r["table_name"] for r in rows}
    assert {"campaigns", "contacts_cache", "campaign_contacts", "sends",
            "events", "suppressions", "jobs"} <= names


async def test_bot_tables_exist(pool):
    rows = await pool.fetch(
        "select table_name from information_schema.tables where table_schema='public'")
    assert {"bot_sessions", "slack_events"} <= {r["table_name"] for r in rows}


async def test_notify_columns_exist(pool):
    campaign_cols = {r["column_name"] for r in await pool.fetch(
        "select column_name from information_schema.columns where table_name='campaigns'")}
    assert {"thread_ts", "channel"} <= campaign_cols
    post_cols = {r["column_name"] for r in await pool.fetch(
        "select column_name from information_schema.columns where table_name='social_posts'")}
    assert {"channel", "notified_at"} <= post_cols


async def test_social_tables_exist(pool):
    rows = await pool.fetch(
        "select table_name from information_schema.tables where table_schema='public'")
    assert {"social_posts", "images"} <= {r["table_name"] for r in rows}


async def test_campaigns_have_content_and_seed_tested(pool):
    cols = {r["column_name"] for r in await pool.fetch(
        "select column_name from information_schema.columns where table_name='campaigns'")}
    assert {"content", "seed_tested_at"} <= cols


async def test_sends_unique_per_campaign_contact(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('t', 's', 'welcome', 'v1') returning id"
    )
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email) values ($1, 'c1', 'a@b.co')", cid
    )
    import asyncpg, pytest
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "insert into sends (campaign_id, ghl_contact_id, email) values ($1, 'c1', 'a@b.co')", cid
        )
