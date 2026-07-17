import os
import pathlib

import asyncpg
import pytest

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:test@localhost:54329/postgres"
)
MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "supabase" / "migrations"


@pytest.fixture(scope="session")
async def pool():
    setup = await asyncpg.connect(TEST_DB_URL)
    await setup.execute("drop schema public cascade; create schema public;")
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await setup.execute(migration.read_text())
    await setup.close()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=5)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
async def _clean_tables(pool):
    await pool.execute(
        "truncate campaigns, contacts_cache, campaign_contacts, sends, events, "
        "suppressions, jobs, bot_sessions, slack_events, social_posts, images, "
        "replies, sending_domains, "
        "daily_reports cascade"
    )
    yield


@pytest.fixture
async def client(pool):
    import httpx as _httpx

    from app.main import create_app
    from tests.helpers import make_settings

    app = create_app()
    app.state.pool = pool
    app.state.settings = make_settings()
    transport = _httpx.ASGITransport(app=app)
    async with _httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                  headers={"x-api-key": "test-api-key"}) as c:
        yield c
