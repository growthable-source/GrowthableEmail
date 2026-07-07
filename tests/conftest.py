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
        "suppressions, jobs cascade"
    )
    yield
