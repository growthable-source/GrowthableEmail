# GHL ↔ Resend Email Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Option-B pipeline from `docs/spec.md`: GHL stays the CRM/source of truth, Resend sends, a FastAPI service on Render + Supabase Postgres orchestrates audience pull, React Email rendering, batch dispatch, event write-back, and bidirectional suppression sync.

**Architecture:** Monorepo. `app/` is a FastAPI service (Python 3.12, asyncpg, raw SQL — no ORM) exposing the §9 endpoints; a separate worker process (`python -m app.worker`) drains a Postgres-backed queue (pg-boss *pattern*: `FOR UPDATE SKIP LOCKED`; pg-boss itself is Node-only so we implement the pattern). `emails/` is a Node package holding React Email templates plus a `render.tsx` CLI that the Python service invokes as a subprocess (JSON in → `[{html,text}]` out) in batches. `supabase/migrations/` holds plain SQL migrations. The `sends` table itself is the send queue (status `queued` + SKIP LOCKED claim); the `jobs` table queues GHL write-backs so a GHL outage never blocks webhook ingestion.

**Tech Stack:** Python 3.12 (uv), FastAPI, asyncpg, httpx, pydantic-settings, svix (webhook verification), pytest + pytest-asyncio + respx; Node 22, React Email (`@react-email/components`, `@react-email/render`), tsx; Postgres 16 (Supabase in prod, local Docker for tests); Docker deploy on Render (one image, web + worker services).

**Decisions that deviate from / extend the spec** (surface these to Ryan):
1. **Single sends with RPS limiter, not the batch endpoint.** Spec §5 allows either. Single `POST /emails` guarantees per-message `List-Unsubscribe` headers and simpler retry semantics; at `SEND_RPS=2` that's ~7k emails/hour, plenty for the §2 ramp. Batch can be added later behind a flag.
2. **`campaigns.subject` column added** — spec schema has no subject anywhere; it must live on the campaign.
3. **`campaign_contacts` join table added** — spec's `contacts_cache` is global with no campaign linkage, but dispatch needs to know which contacts belong to which campaign's audience.
4. **`DATABASE_URL` instead of `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`** — we talk straight Postgres via asyncpg (Supabase's connection-pooler URL). No PostgREST layer needed.
5. **Extra env vars:** `PUBLIC_BASE_URL` (to build unsub URLs), `FROM_EMAIL`, `GHL_WEBHOOK_SECRET` (shared secret for inbound GHL webhooks), `SEED_EMAILS`, optional `ALERT_WEBHOOK_URL`.
6. **Unsub endpoint accepts GET and POST** — RFC 8058 one-click sends POST; humans clicking the footer link send GET.

## File Structure

```
GrowthableEmail/
├── docs/spec.md                      # the source spec (copied in)
├── pyproject.toml                    # uv project, deps, pytest config
├── .env.example
├── Dockerfile                        # python 3.12 + node 22, serves web & worker
├── render.yaml                       # Render blueprint: web + worker
├── supabase/migrations/0001_init.sql
├── app/
│   ├── __init__.py
│   ├── config.py                     # Settings (pydantic-settings)
│   ├── db.py                         # asyncpg pool helper
│   ├── main.py                       # create_app() factory, lifespan, routers
│   ├── worker.py                     # background worker loop (python -m app.worker)
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── campaigns.py              # POST /campaigns, sync-audience, test, dispatch, report
│   │   ├── webhooks.py               # /webhooks/resend, /webhooks/ghl/enroll, /webhooks/ghl/dnd
│   │   └── unsub.py                  # GET+POST /u/{token}
│   └── services/
│       ├── __init__.py
│       ├── unsub_tokens.py           # HMAC token make/parse (pure)
│       ├── suppressions.py           # canonical suppression store ops
│       ├── ratelimit.py              # RateLimiter (token-interval)
│       ├── ghl.py                    # GHL v2 client: search/tags/DND, retry+ratelimit
│       ├── resend_client.py          # Resend client: send, retry classes
│       ├── renderer.py               # subprocess bridge to emails/render.tsx
│       ├── audience.py               # sync_audience: GHL → contacts_cache + campaign_contacts
│       ├── jobs.py                   # pg-boss-pattern queue (enqueue/fetch/complete/fail)
│       ├── dispatch.py               # enqueue_campaign_sends, process_send_queue
│       ├── writeback.py              # process ghl_writeback jobs
│       └── guardrails.py             # daily bounce/complaint kill rule → auto-pause
├── emails/
│   ├── package.json
│   ├── render.tsx                    # CLI: stdin {template, props[]} → stdout [{html,text}]
│   ├── components/Layout.tsx         # shared brand shell: header, footer, address, unsub
│   ├── templates/welcome.tsx         # first campaign template
│   └── tests/render.test.tsx         # node:test render assertions
└── tests/
    ├── conftest.py                   # test DB pool (docker pg), migrations, truncation, app client
    ├── helpers.py                    # test Settings factory, svix signer
    ├── test_config.py
    ├── test_unsub_tokens.py
    ├── test_suppressions.py
    ├── test_renderer.py
    ├── test_ghl.py
    ├── test_audience.py
    ├── test_jobs.py
    ├── test_resend_client.py
    ├── test_dispatch.py
    ├── test_guardrails.py
    ├── test_api_campaigns.py
    ├── test_webhook_resend.py
    └── test_inbound_and_unsub.py
```

**Conventions used throughout:**
- All emails normalized `lower().strip()` before storage/comparison. `suppressions.email` stores lowercase.
- asyncpg returns `jsonb` as `str` → `json.loads` on read, `json.dumps` on write.
- Timestamps UTC; "today" = `date_trunc('day', now())` in Postgres (UTC server).
- Money/limits enforced in code paths, not by discipline (spec §12).
- Test DB: local Docker Postgres on port 54329 (started once in Task 2), `TEST_DATABASE_URL` env can override.
- Run Python things with `uv run <cmd>` from repo root. Run Node things from `emails/`.

---

### Task 1: Repo scaffold

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `docs/spec.md` (copy), `.env.example`, `README.md`, `app/__init__.py`, `app/routers/__init__.py`, `app/services/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Init repo and copy spec**

```bash
cd /Users/ryan/GrowthableEmail
git init -b main
mkdir -p docs app/routers app/services tests supabase/migrations emails
cp /Users/ryan/Downloads/ghl-resend-pipeline-spec.md docs/spec.md
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
node_modules/
.pytest_cache/
.DS_Store
emails/.react-email/
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[project]
name = "growthable-email"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "asyncpg>=0.30",
    "pydantic-settings>=2.6",
    "httpx>=0.27",
    "svix>=1.44",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
]

[tool.uv]
package = false

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
testpaths = ["tests"]
```

- [ ] **Step 4: Create empty package markers and `.env.example`**

`app/__init__.py`, `app/routers/__init__.py`, `app/services/__init__.py`, `tests/__init__.py` — all empty files.

`.env.example`:
```bash
DATABASE_URL=postgresql://postgres:password@db.xxx.supabase.co:5432/postgres
RESEND_API_KEY=re_xxx
RESEND_WEBHOOK_SECRET=whsec_xxx
GHL_PI_TOKEN=pit-xxx
GHL_LOCATION_ID=xxx
GHL_WEBHOOK_SECRET=choose-a-long-random-string
UNSUB_SIGNING_SECRET=choose-a-long-random-string
PUBLIC_BASE_URL=https://growthable-email-api.onrender.com
FROM_EMAIL="Growthable <news@news.growthable.io>"
SEND_RPS=2
DAILY_SEND_CAP=500
SEED_EMAILS=ryan@growthable.io
ALERT_WEBHOOK_URL=
```

`README.md`:
```markdown
# GrowthableEmail — GHL ↔ Resend pipeline

FastAPI + Supabase service that pulls audiences from GoHighLevel, renders React Email
templates, dispatches via Resend, writes events back to GHL, and keeps a canonical
suppression list. Spec: docs/spec.md. Runbook: see bottom of this file (added in Task 18).

## Dev setup
    uv sync
    (cd emails && npm install)
    docker start growthable-test-pg || docker run -d --name growthable-test-pg \
      -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
    uv run pytest
```

- [ ] **Step 5: Install deps and verify env**

```bash
uv sync
uv run python -c "import fastapi, asyncpg, httpx, svix; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: scaffold monorepo (uv project, spec, env example)"
```

---

### Task 2: Supabase schema + test harness

**Files:**
- Create: `supabase/migrations/0001_init.sql`
- Create: `tests/conftest.py`, `tests/helpers.py`
- Test: `tests/test_schema.py`

- [ ] **Step 1: Start the test Postgres container (one-time)**

```bash
docker run -d --name growthable-test-pg -e POSTGRES_PASSWORD=test -p 54329:5432 postgres:16
sleep 3 && docker exec growthable-test-pg pg_isready -U postgres
```
Expected: `accepting connections` (if the container already exists: `docker start growthable-test-pg`)

- [ ] **Step 2: Write the failing schema test**

`tests/test_schema.py`:
```python
async def test_all_tables_exist(pool):
    rows = await pool.fetch(
        "select table_name from information_schema.tables where table_schema='public'"
    )
    names = {r["table_name"] for r in rows}
    assert {"campaigns", "contacts_cache", "campaign_contacts", "sends",
            "events", "suppressions", "jobs"} <= names


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
```

- [ ] **Step 3: Write `tests/conftest.py`**

```python
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
```

- [ ] **Step 4: Write `tests/helpers.py`**

```python
import base64
import hashlib
import hmac
import time

from app.config import Settings

TEST_WEBHOOK_KEY = base64.b64encode(b"0" * 32).decode()


def make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://postgres:test@localhost:54329/postgres",
        resend_api_key="re_test_key",
        resend_webhook_secret=f"whsec_{TEST_WEBHOOK_KEY}",
        ghl_pi_token="pit-test",
        ghl_location_id="loc_test",
        ghl_webhook_secret="hook-secret",
        unsub_signing_secret="unsub-secret",
        public_base_url="http://testserver",
        from_email="Growthable <news@news.growthable.io>",
        send_rps=1000.0,
        daily_send_cap=500,
        seed_emails="seed@growthable.io",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def svix_headers(secret: str, payload: str, msg_id: str = "msg_1") -> dict:
    ts = str(int(time.time()))  # svix rejects timestamps outside ±5 min tolerance
    key = base64.b64decode(secret.split("_", 1)[1])
    to_sign = f"{msg_id}.{ts}.{payload}".encode()
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}
```
(Note: `Settings` import will fail until Task 3 — for this task only, temporarily comment nothing; `helpers.py` is only imported by later tests, and `test_schema.py` does not import it.)

- [ ] **Step 5: Run test to verify it fails**

```bash
uv run pytest tests/test_schema.py -v
```
Expected: FAIL (`UndefinedTableError` / migration glob applies nothing yet)

- [ ] **Step 6: Write `supabase/migrations/0001_init.sql`**

```sql
create table campaigns (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    subject text not null,
    template_ref text not null,
    template_version text not null,
    audience_filter jsonb not null default '[]',
    status text not null default 'draft', -- draft|ready|dispatching|paused|completed
    scheduled_at timestamptz,
    created_at timestamptz not null default now()
);

create table contacts_cache (
    ghl_contact_id text primary key,
    email text not null,
    first_name text not null default '',
    last_name text not null default '',
    custom jsonb not null default '{}',
    tags text[] not null default '{}',
    dnd boolean not null default false,
    synced_at timestamptz not null default now()
);

create table campaign_contacts (
    campaign_id uuid not null references campaigns(id) on delete cascade,
    ghl_contact_id text not null,
    primary key (campaign_id, ghl_contact_id)
);

create table sends (
    id uuid primary key default gen_random_uuid(),
    campaign_id uuid not null references campaigns(id),
    ghl_contact_id text not null,
    email text not null,
    resend_email_id text,
    status text not null default 'queued', -- queued|sending|sent|failed|suppressed
    error text,
    rendered_hash text,
    retry_count int not null default 0,
    next_attempt_at timestamptz not null default now(),
    sent_at timestamptz,
    created_at timestamptz not null default now(),
    unique (campaign_id, ghl_contact_id)
);
create index sends_campaign_status_idx on sends (campaign_id, status);
create index sends_queue_idx on sends (status, next_attempt_at);
create index sends_resend_email_id_idx on sends (resend_email_id);
create index sends_sent_at_idx on sends (sent_at);

create table events (
    id uuid primary key default gen_random_uuid(),
    send_id uuid references sends(id),
    type text not null,
    payload jsonb not null default '{}',
    occurred_at timestamptz not null default now()
);
create index events_send_idx on events (send_id);
create index events_type_time_idx on events (type, occurred_at);

create table suppressions (
    email text primary key,
    ghl_contact_id text,
    reason text not null, -- hard_bounce|complaint|unsubscribe|ghl_dnd
    source text not null, -- resend|ghl|unsub_page|manual
    created_at timestamptz not null default now()
);

create table jobs (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    data jsonb not null default '{}',
    state text not null default 'created', -- created|active|completed|failed
    retry_count int not null default 0,
    retry_limit int not null default 3,
    start_after timestamptz not null default now(),
    created_at timestamptz not null default now(),
    completed_at timestamptz
);
create index jobs_fetch_idx on jobs (name, state, start_after);
```

- [ ] **Step 7: Run test to verify it passes**

```bash
uv run pytest tests/test_schema.py -v
```
Expected: 2 PASS

- [ ] **Step 8: Commit**

```bash
git add supabase tests
git commit -m "feat: initial schema migration + pytest DB harness"
```

---

### Task 3: Config module

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
from tests.helpers import make_settings


def test_settings_construct_and_defaults():
    s = make_settings()
    assert s.send_rps == 1000.0
    assert s.daily_send_cap == 500
    assert s.alert_webhook_url is None


def test_seed_list_parses_csv():
    s = make_settings(seed_emails="a@x.co, b@x.co ,")
    assert s.seed_list == ["a@x.co", "b@x.co"]


def test_from_domain_extracted():
    s = make_settings(from_email="Growthable <news@news.growthable.io>")
    assert s.from_domain == "news.growthable.io"


def test_env_vars_override(monkeypatch):
    for key, val in {
        "DATABASE_URL": "postgresql://env/db", "RESEND_API_KEY": "re_env",
        "RESEND_WEBHOOK_SECRET": "whsec_env", "GHL_PI_TOKEN": "pit-env",
        "GHL_LOCATION_ID": "loc-env", "GHL_WEBHOOK_SECRET": "hs",
        "UNSUB_SIGNING_SECRET": "us", "PUBLIC_BASE_URL": "https://x",
        "FROM_EMAIL": "a <a@b.co>", "DAILY_SEND_CAP": "2000",
    }.items():
        monkeypatch.setenv(key, val)
    from app.config import Settings
    s = Settings()
    assert s.daily_send_cap == 2000
    assert s.resend_api_key == "re_env"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_config.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'` (via helpers import)

- [ ] **Step 3: Write `app/config.py`**

```python
import re
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    resend_api_key: str
    resend_webhook_secret: str
    ghl_pi_token: str
    ghl_location_id: str
    ghl_webhook_secret: str
    unsub_signing_secret: str
    public_base_url: str
    from_email: str
    send_rps: float = 2.0
    daily_send_cap: int = 500
    seed_emails: str = ""
    alert_webhook_url: str | None = None

    @property
    def seed_list(self) -> list[str]:
        return [e.strip() for e in self.seed_emails.split(",") if e.strip()]

    @property
    def from_domain(self) -> str:
        match = re.search(r"@([\w.-]+)", self.from_email)
        return match.group(1) if match else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_config.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests
git commit -m "feat: settings module (env-driven config)"
```

---

### Task 4: Unsubscribe token signing

**Files:**
- Create: `app/services/unsub_tokens.py`
- Test: `tests/test_unsub_tokens.py`

- [ ] **Step 1: Write the failing test**

`tests/test_unsub_tokens.py`:
```python
from app.services.unsub_tokens import make_token, parse_token

SECRET = "unsub-secret"


def test_roundtrip():
    token = make_token("Ada@Example.COM", "camp-123", SECRET)
    assert parse_token(token, SECRET) == ("ada@example.com", "camp-123")


def test_tampered_token_rejected():
    token = make_token("ada@example.com", "camp-123", SECRET)
    payload, sig = token.split(".")
    assert parse_token(f"{payload}x.{sig}", SECRET) is None
    assert parse_token(token, "other-secret") is None


def test_garbage_rejected():
    assert parse_token("not-a-token", SECRET) is None
    assert parse_token("a.b.c", SECRET) is None
    assert parse_token("", SECRET) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_unsub_tokens.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/unsub_tokens.py`**

```python
import base64
import hashlib
import hmac


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest())


def make_token(email: str, campaign_id: str, secret: str) -> str:
    payload_b64 = _b64(f"{email.strip().lower()}|{campaign_id}".encode())
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def parse_token(token: str, secret: str) -> tuple[str, str] | None:
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    if not hmac.compare_digest(sig, _sign(payload_b64, secret)):
        return None
    try:
        email, campaign_id = _unb64(payload_b64).decode().split("|", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    return email, campaign_id
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_unsub_tokens.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/unsub_tokens.py tests/test_unsub_tokens.py
git commit -m "feat: HMAC-signed one-click unsubscribe tokens"
```

---

### Task 5: Suppressions service (canonical store)

**Files:**
- Create: `app/services/suppressions.py`
- Test: `tests/test_suppressions.py`

- [ ] **Step 1: Write the failing test**

`tests/test_suppressions.py`:
```python
from app.services.suppressions import add_suppression, is_suppressed, suppressed_subset


async def test_add_and_check_normalizes_email(pool):
    await add_suppression(pool, " Ada@Example.COM ", reason="unsubscribe", source="unsub_page")
    assert await is_suppressed(pool, "ada@example.com") is True
    assert await is_suppressed(pool, "ADA@EXAMPLE.COM") is True
    assert await is_suppressed(pool, "other@example.com") is False


async def test_first_reason_wins(pool):
    await add_suppression(pool, "a@b.co", reason="hard_bounce", source="resend", ghl_contact_id="c1")
    await add_suppression(pool, "a@b.co", reason="complaint", source="resend")
    row = await pool.fetchrow("select reason, ghl_contact_id from suppressions where email='a@b.co'")
    assert row["reason"] == "hard_bounce"
    assert row["ghl_contact_id"] == "c1"


async def test_suppressed_subset(pool):
    await add_suppression(pool, "a@b.co", reason="ghl_dnd", source="ghl")
    await add_suppression(pool, "c@d.co", reason="complaint", source="resend")
    result = await suppressed_subset(pool, ["A@B.CO", "x@y.co", "c@d.co"])
    assert result == {"a@b.co", "c@d.co"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_suppressions.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/suppressions.py`**

```python
def normalize(email: str) -> str:
    return email.strip().lower()


async def add_suppression(pool, email: str, *, reason: str, source: str,
                          ghl_contact_id: str | None = None) -> None:
    await pool.execute(
        "insert into suppressions (email, ghl_contact_id, reason, source) "
        "values ($1, $2, $3, $4) on conflict (email) do nothing",
        normalize(email), ghl_contact_id, reason, source,
    )


async def is_suppressed(pool, email: str) -> bool:
    return await pool.fetchval(
        "select exists(select 1 from suppressions where email = $1)", normalize(email)
    )


async def suppressed_subset(pool, emails: list[str]) -> set[str]:
    rows = await pool.fetch(
        "select email from suppressions where email = any($1::text[])",
        [normalize(e) for e in emails],
    )
    return {r["email"] for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_suppressions.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/suppressions.py tests/test_suppressions.py
git commit -m "feat: canonical suppression store operations"
```

---

### Task 6: React Email package + render CLI

**Files:**
- Create: `emails/package.json`, `emails/components/Layout.tsx`, `emails/templates/welcome.tsx`, `emails/render.tsx`
- Test: `emails/tests/render.test.tsx`

- [ ] **Step 1: Write `emails/package.json` and install**

```json
{
  "name": "growthable-emails",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "email dev",
    "test": "tsx --test tests/render.test.tsx"
  },
  "dependencies": {
    "@react-email/components": "^0.0.31",
    "@react-email/render": "^1.0.3",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@types/react": "^18.3.12",
    "react-email": "^3.0.2",
    "tsx": "^4.19.2"
  }
}
```

```bash
cd emails && npm install
```
Expected: installs cleanly (react-email pulls a lot; that's the preview dev server, dev-only)

- [ ] **Step 2: Write the failing node test**

`emails/tests/render.test.tsx`:
```tsx
import test from 'node:test';
import assert from 'node:assert';
import { createElement } from 'react';
import { render } from '@react-email/render';
import Welcome from '../templates/welcome.tsx';

test('welcome renders personalization, unsub link, address, preheader', async () => {
  const html = await render(
    createElement(Welcome, { firstName: 'Ada', unsubUrl: 'https://x.io/u/tok123' }),
  );
  assert.ok(html.includes('Ada'));
  assert.ok(html.includes('https://x.io/u/tok123'));
  assert.ok(html.toLowerCase().includes('unsubscribe'));
  assert.ok(html.includes('PHYSICAL_ADDRESS'));
});

test('welcome renders a plain-text part', async () => {
  const text = await render(
    createElement(Welcome, { firstName: 'Ada', unsubUrl: 'https://x.io/u/tok123' }),
    { plainText: true },
  );
  assert.ok(text.includes('Ada'));
  assert.ok(text.includes('https://x.io/u/tok123'));
});
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd emails && npm test
```
Expected: FAIL — cannot find `../templates/welcome.tsx`

- [ ] **Step 4: Write `emails/components/Layout.tsx`**

```tsx
import {
  Body, Container, Head, Hr, Html, Link, Preview, Section, Text,
} from '@react-email/components';
import * as React from 'react';

// TODO(ryan): replace with the real business mailing address before first send —
// a physical address in the footer is a CAN-SPAM requirement (spec §4).
export const BUSINESS_ADDRESS = 'PHYSICAL_ADDRESS — 123 Example St, Brisbane QLD 4000, Australia';

interface LayoutProps {
  preheader: string;
  unsubUrl: string;
  children: React.ReactNode;
}

export default function Layout({ preheader, unsubUrl, children }: LayoutProps) {
  return (
    <Html lang="en">
      <Head />
      <Preview>{preheader}</Preview>
      <Body style={{ backgroundColor: '#f4f4f5', fontFamily: 'Helvetica, Arial, sans-serif' }}>
        <Container style={{ backgroundColor: '#ffffff', margin: '0 auto', padding: '32px', maxWidth: '600px' }}>
          <Text style={{ fontSize: '20px', fontWeight: 700, color: '#18181b' }}>Growthable</Text>
          <Section>{children}</Section>
          <Hr style={{ borderColor: '#e4e4e7', margin: '32px 0 16px' }} />
          <Text style={{ fontSize: '12px', color: '#71717a', lineHeight: '18px' }}>
            {BUSINESS_ADDRESS}
            <br />
            You are receiving this because you are a Growthable contact.{' '}
            <Link href={unsubUrl} style={{ color: '#71717a', textDecoration: 'underline' }}>
              Unsubscribe
            </Link>
          </Text>
        </Container>
      </Body>
    </Html>
  );
}
```

- [ ] **Step 5: Write `emails/templates/welcome.tsx`**

```tsx
import { Button, Text } from '@react-email/components';
import * as React from 'react';
import Layout from '../components/Layout.tsx';

interface WelcomeProps {
  firstName?: string;
  unsubUrl: string;
}

export default function Welcome({ firstName = 'there', unsubUrl }: WelcomeProps) {
  return (
    <Layout preheader="News and updates from the Growthable team" unsubUrl={unsubUrl}>
      <Text style={{ fontSize: '16px', color: '#18181b', lineHeight: '24px' }}>
        Hi {firstName},
      </Text>
      <Text style={{ fontSize: '16px', color: '#18181b', lineHeight: '24px' }}>
        Welcome to the new Growthable newsletter. Expect practical updates — no fluff.
      </Text>
      <Button
        href="https://growthable.io"
        style={{ backgroundColor: '#18181b', color: '#ffffff', padding: '12px 20px', borderRadius: '6px', fontSize: '14px' }}
      >
        Visit Growthable
      </Button>
    </Layout>
  );
}
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd emails && npm test
```
Expected: 2 PASS

- [ ] **Step 7: Write `emails/render.tsx` (the CLI the Python side calls)**

Contract: stdin JSON `{"template": "welcome", "props": [{...}, ...]}` → stdout JSON `[{"html": "...", "text": "..."}, ...]`. Exit 1 with message on stderr for unknown template/bad input.

```tsx
import { createElement } from 'react';
import { render } from '@react-email/render';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  const input = JSON.parse(await readStdin());
  if (!input.template || !Array.isArray(input.props)) {
    throw new Error('expected {template: string, props: object[]}');
  }
  if (!/^[\w-]+$/.test(input.template)) {
    throw new Error(`invalid template ref: ${input.template}`);
  }
  const templatePath = path.join(
    path.dirname(new URL(import.meta.url).pathname), 'templates', `${input.template}.tsx`,
  );
  const mod = await import(pathToFileURL(templatePath).href);
  const Component = mod.default;
  const out = [];
  for (const props of input.props) {
    out.push({
      html: await render(createElement(Component, props)),
      text: await render(createElement(Component, props), { plainText: true }),
    });
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch((err) => {
  process.stderr.write(String(err?.stack ?? err));
  process.exit(1);
});
```

- [ ] **Step 8: Smoke-test the CLI**

```bash
cd emails && echo '{"template":"welcome","props":[{"firstName":"Ada","unsubUrl":"https://x/u/t"}]}' \
  | ./node_modules/.bin/tsx render.tsx | head -c 200
```
Expected: JSON starting `[{"html":"<!DOCTYPE html...` containing "Ada"

- [ ] **Step 9: Commit**

```bash
git add emails
git commit -m "feat: react-email package with layout, welcome template, render CLI"
```

---

### Task 7: Python renderer bridge

**Files:**
- Create: `app/services/renderer.py`
- Test: `tests/test_renderer.py`

- [ ] **Step 1: Write the failing test** (invokes the real Node CLI — Task 6 must be done)

`tests/test_renderer.py`:
```python
import pytest

from app.services.renderer import RenderError, render_batch


async def test_render_batch_personalizes_each_contact():
    results = await render_batch("welcome", [
        {"firstName": "Ada", "unsubUrl": "https://x.io/u/t1"},
        {"firstName": "Bob", "unsubUrl": "https://x.io/u/t2"},
    ])
    assert len(results) == 2
    assert "Ada" in results[0].html and "https://x.io/u/t1" in results[0].html
    assert "Bob" in results[1].html and "Bob" in results[1].text
    assert results[0].hash != results[1].hash
    assert len(results[0].hash) == 64


async def test_unknown_template_raises():
    with pytest.raises(RenderError):
        await render_batch("nope-not-real", [{}])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_renderer.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/renderer.py`**

```python
import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

EMAILS_DIR = Path(__file__).resolve().parents[2] / "emails"


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class Rendered:
    html: str
    text: str
    hash: str


async def render_batch(template_ref: str, props_list: list[dict]) -> list[Rendered]:
    proc = await asyncio.create_subprocess_exec(
        str(EMAILS_DIR / "node_modules" / ".bin" / "tsx"),
        str(EMAILS_DIR / "render.tsx"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=EMAILS_DIR,
    )
    payload = json.dumps({"template": template_ref, "props": props_list}).encode()
    out, err = await proc.communicate(payload)
    if proc.returncode != 0:
        raise RenderError(f"render failed for {template_ref!r}: {err.decode()[:2000]}")
    return [
        Rendered(html=item["html"], text=item["text"],
                 hash=hashlib.sha256(item["html"].encode()).hexdigest())
        for item in json.loads(out)
    ]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_renderer.py -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/renderer.py tests/test_renderer.py
git commit -m "feat: python-to-node render bridge with content hashing"
```

---

### Task 8: Rate limiter + GHL client

**Files:**
- Create: `app/services/ratelimit.py`, `app/services/ghl.py`
- Test: `tests/test_ghl.py`

- [ ] **Step 1: Write the failing test**

`tests/test_ghl.py`:
```python
import httpx
import pytest
import respx

from app.services.ghl import GHLClient, GHLError

BASE = "https://services.leadconnectorhq.com"


def make_client(**kw) -> GHLClient:
    return GHLClient(token="pit-test", location_id="loc1", rps=10_000, backoff_base=0, **kw)


@respx.mock
async def test_search_contacts_paginates_and_parses():
    page1 = {
        "contacts": [
            {"id": "c1", "email": "Ada@Example.com", "firstNameLowerCase": "ada",
             "lastNameLowerCase": "lovelace", "tags": ["vip"], "dnd": False,
             "customFields": [{"id": "f1", "value": "gold"}], "searchAfter": [1, "c1"]},
        ] * 2,
        "total": 3,
    }
    page1["contacts"][1] = {**page1["contacts"][1], "id": "c2", "searchAfter": [2, "c2"]}
    page2 = {"contacts": [{"id": "c3", "email": "c3@x.co", "dnd": True, "searchAfter": [3, "c3"]}],
             "total": 3}
    route = respx.post(f"{BASE}/contacts/search").mock(side_effect=[
        httpx.Response(200, json=page1), httpx.Response(200, json=page2),
    ])
    client = make_client()
    contacts = [c async for c in client.search_contacts(filters=[{"field": "tags", "operator": "eq", "value": "vip"}], page_limit=2)]
    assert [c["ghl_contact_id"] for c in contacts] == ["c1", "c2", "c3"]
    assert contacts[0]["email"] == "ada@example.com"
    assert contacts[0]["first_name"] == "ada"
    assert contacts[0]["custom"] == {"f1": "gold"}
    assert contacts[2]["dnd"] is True
    body = respx.calls[0].request.read().decode()
    assert '"locationId": "loc1"' in body or '"locationId":"loc1"' in body
    # second request carries searchAfter cursor from last contact of page 1
    assert "searchAfter" in respx.calls[1].request.read().decode()


@respx.mock
async def test_add_tags_and_set_dnd():
    tag_route = respx.post(f"{BASE}/contacts/c1/tags").mock(return_value=httpx.Response(200, json={}))
    dnd_route = respx.put(f"{BASE}/contacts/c1").mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    await client.add_tags("c1", ["opened-launch"])
    await client.set_dnd_email("c1")
    assert tag_route.called and b"opened-launch" in tag_route.calls[0].request.read()
    dnd_body = dnd_route.calls[0].request.read().decode()
    assert "dndSettings" in dnd_body and "Email" in dnd_body


@respx.mock
async def test_retries_on_429_then_succeeds():
    route = respx.post(f"{BASE}/contacts/c1/tags").mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json={}),
    ])
    await make_client().add_tags("c1", ["x"])
    assert route.call_count == 2


@respx.mock
async def test_hard_4xx_raises_immediately():
    route = respx.post(f"{BASE}/contacts/c1/tags").mock(return_value=httpx.Response(422, json={"msg": "bad"}))
    with pytest.raises(GHLError):
        await make_client().add_tags("c1", ["x"])
    assert route.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_ghl.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/ratelimit.py`**

```python
import asyncio
import time


class RateLimiter:
    """Serializes callers to at most `rps` calls per second (min-interval pacing)."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_slot:
                await asyncio.sleep(self._next_slot - now)
                now = time.monotonic()
            self._next_slot = max(now, self._next_slot) + self._interval
```

- [ ] **Step 4: Write `app/services/ghl.py`**

```python
import asyncio
import logging
from typing import AsyncIterator

import httpx

from app.services.ratelimit import RateLimiter

log = logging.getLogger(__name__)

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
MAX_ATTEMPTS = 3


class GHLError(Exception):
    pass


def _parse_contact(raw: dict) -> dict:
    return {
        "ghl_contact_id": raw["id"],
        "email": (raw.get("email") or "").strip().lower(),
        "first_name": raw.get("firstNameRaw") or raw.get("firstName")
        or raw.get("firstNameLowerCase") or "",
        "last_name": raw.get("lastNameRaw") or raw.get("lastName")
        or raw.get("lastNameLowerCase") or "",
        "tags": raw.get("tags") or [],
        "dnd": bool(raw.get("dnd")),
        "custom": {f["id"]: f.get("value") for f in raw.get("customFields") or []},
        "search_after": raw.get("searchAfter"),
    }


class GHLClient:
    def __init__(self, token: str, location_id: str, rps: float = 8.0,
                 backoff_base: float = 1.0):
        self.location_id = location_id
        self._limiter = RateLimiter(rps)
        self._backoff_base = backoff_base
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.wait()
            try:
                async with httpx.AsyncClient(base_url=BASE_URL, headers=self._headers,
                                             timeout=30) as client:
                    resp = await client.request(method, path, json=json_body)
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = GHLError(f"{method} {path} -> {resp.status_code}")
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise GHLError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.content else {}
        raise GHLError(f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    async def search_contacts(self, filters: list[dict],
                              page_limit: int = 100) -> AsyncIterator[dict]:
        search_after = None
        while True:
            body: dict = {"locationId": self.location_id, "pageLimit": page_limit}
            if filters:
                body["filters"] = filters
            if search_after:
                body["searchAfter"] = search_after
            data = await self._request("POST", "/contacts/search", body)
            contacts = data.get("contacts") or []
            if not contacts:
                return
            for raw in contacts:
                yield _parse_contact(raw)
            search_after = _parse_contact(contacts[-1])["search_after"]
            if len(contacts) < page_limit or not search_after:
                return

    async def add_tags(self, contact_id: str, tags: list[str]) -> None:
        await self._request("POST", f"/contacts/{contact_id}/tags", {"tags": tags})

    async def set_dnd_email(self, contact_id: str) -> None:
        await self._request("PUT", f"/contacts/{contact_id}", {
            "dndSettings": {"Email": {"status": "active",
                                      "message": "Suppressed by email pipeline"}},
        })
```

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_ghl.py -v
```
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/ratelimit.py app/services/ghl.py tests/test_ghl.py
git commit -m "feat: GHL v2 client (search/tags/DND) with rate limiting and retries"
```

---

### Task 9: Audience sync (GHL → contacts_cache)

**Files:**
- Create: `app/services/audience.py`
- Test: `tests/test_audience.py`

- [ ] **Step 1: Write the failing test**

`tests/test_audience.py`:
```python
import json

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
            "dnd": kw.get("dnd", False), "custom": kw.get("custom", {}), "search_after": None}


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
        contact("c2", "", ),                      # missing email
        contact("c3", "not-an-email"),            # invalid syntax
        contact("c4", "dnd@x.co", dnd=True),      # GHL DND
        contact("c5", "sup@x.co"),                # suppressed
    ])
    result = await sync_audience(pool, ghl, campaign_id)
    assert result == {"kept": 1, "dropped": 4}
    row = await pool.fetchrow("select * from contacts_cache where ghl_contact_id='c1'")
    assert row["email"] == "good@x.co"
    assert json.loads(row["custom"]) == {"f1": "gold"}
    linked = await pool.fetch("select ghl_contact_id from campaign_contacts where campaign_id=$1",
                              __import__("uuid").UUID(campaign_id))
    assert [r["ghl_contact_id"] for r in linked] == ["c1"]


async def test_resync_updates_existing_contact(pool):
    campaign_id = await make_campaign(pool)
    ghl1 = FakeGHL([contact("c1", "old@x.co")])
    await sync_audience(pool, ghl1, campaign_id)
    ghl2 = FakeGHL([contact("c1", "new@x.co", first_name="Ada")])
    result = await sync_audience(pool, ghl2, campaign_id)
    assert result["kept"] == 1
    row = await pool.fetchrow("select email, first_name from contacts_cache where ghl_contact_id='c1'")
    assert row["email"] == "new@x.co" and row["first_name"] == "Ada"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_audience.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/audience.py`**

```python
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
                   (ghl_contact_id, email, first_name, last_name, custom, tags, dnd, synced_at)
               values ($1, $2, $3, $4, $5, $6, $7, now())
               on conflict (ghl_contact_id) do update set
                   email = excluded.email, first_name = excluded.first_name,
                   last_name = excluded.last_name, custom = excluded.custom,
                   tags = excluded.tags, dnd = excluded.dnd, synced_at = now()""",
            c["ghl_contact_id"], email, c["first_name"], c["last_name"],
            json.dumps(c["custom"]), c["tags"], c["dnd"],
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
    return {"kept": kept, "dropped": dropped}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_audience.py -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/audience.py tests/test_audience.py
git commit -m "feat: audience sync with ingest-time drop rules"
```

---

### Task 10: Jobs queue (pg-boss pattern)

**Files:**
- Create: `app/services/jobs.py`
- Test: `tests/test_jobs.py`

- [ ] **Step 1: Write the failing test**

`tests/test_jobs.py`:
```python
from app.services.jobs import complete_job, enqueue, fail_job, fetch_job


async def test_enqueue_fetch_complete(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "add_tags", "contact_id": "c1"})
    job = await fetch_job(pool, "ghl_writeback")
    assert job["data"]["kind"] == "add_tags"
    assert (await pool.fetchval("select state from jobs where id=$1", job["id"])) == "active"
    # another fetch while active gets nothing (SKIP LOCKED semantics + state filter)
    assert await fetch_job(pool, "ghl_writeback") is None
    await complete_job(pool, job["id"])
    assert (await pool.fetchval("select state from jobs where id=$1", job["id"])) == "completed"


async def test_fail_retries_with_backoff_then_dead(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "set_dnd"})
    for expected_retry in (1, 2, 3):
        job = await fetch_job(pool, "ghl_writeback")
        assert job is not None, f"retry {expected_retry} should be fetchable"
        await fail_job(pool, job["id"], backoff_seconds=0)
        row = await pool.fetchrow("select state, retry_count from jobs where id=$1", job["id"])
        if expected_retry < 3:
            assert row["state"] == "created" and row["retry_count"] == expected_retry
        else:
            assert row["state"] == "failed"
    assert await fetch_job(pool, "ghl_writeback") is None


async def test_start_after_delays_fetch(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "x"}, start_after_seconds=3600)
    assert await fetch_job(pool, "ghl_writeback") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_jobs.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/jobs.py`**

```python
import json


async def enqueue(pool, name: str, data: dict, start_after_seconds: int = 0) -> None:
    await pool.execute(
        "insert into jobs (name, data, start_after) "
        "values ($1, $2, now() + make_interval(secs => $3))",
        name, json.dumps(data), start_after_seconds,
    )


async def fetch_job(pool, name: str) -> dict | None:
    row = await pool.fetchrow(
        """update jobs set state='active'
           where id = (select id from jobs
                       where name = $1 and state = 'created' and start_after <= now()
                       order by created_at
                       limit 1
                       for update skip locked)
           returning id, name, data, retry_count, retry_limit""",
        name,
    )
    if row is None:
        return None
    job = dict(row)
    job["data"] = json.loads(job["data"])
    return job


async def complete_job(pool, job_id) -> None:
    await pool.execute(
        "update jobs set state='completed', completed_at=now() where id=$1", job_id
    )


async def fail_job(pool, job_id, backoff_seconds: int = 60) -> None:
    """Retry with exponential backoff until retry_limit, then dead-letter as 'failed'."""
    await pool.execute(
        """update jobs set
               retry_count = retry_count + 1,
               state = case when retry_count + 1 >= retry_limit then 'failed' else 'created' end,
               completed_at = case when retry_count + 1 >= retry_limit then now() else null end,
               start_after = now() + make_interval(secs => $2 * power(2, retry_count))
           where id = $1""",
        job_id, backoff_seconds,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_jobs.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/jobs.py tests/test_jobs.py
git commit -m "feat: postgres job queue (skip-locked fetch, retry with backoff)"
```

---

### Task 11: Resend client

**Files:**
- Create: `app/services/resend_client.py`
- Test: `tests/test_resend_client.py`

- [ ] **Step 1: Write the failing test**

`tests/test_resend_client.py`:
```python
import httpx
import pytest
import respx

from app.services.resend_client import HardSendError, ResendClient, TransientSendError

API = "https://api.resend.com"


def make_client() -> ResendClient:
    return ResendClient(api_key="re_test", rps=10_000, backoff_base=0)


@respx.mock
async def test_send_email_returns_id_and_sends_auth():
    route = respx.post(f"{API}/emails").mock(
        return_value=httpx.Response(200, json={"id": "email_123"})
    )
    email_id = await make_client().send_email({
        "from": "a <a@b.co>", "to": ["x@y.co"], "subject": "s", "html": "<p>h</p>",
    })
    assert email_id == "email_123"
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer re_test"
    assert b'"subject"' in req.read()


@respx.mock
async def test_retries_5xx_then_succeeds():
    route = respx.post(f"{API}/emails").mock(side_effect=[
        httpx.Response(500), httpx.Response(200, json={"id": "email_1"}),
    ])
    assert await make_client().send_email({"to": ["x@y.co"]}) == "email_1"
    assert route.call_count == 2


@respx.mock
async def test_exhausted_retries_raise_transient():
    respx.post(f"{API}/emails").mock(return_value=httpx.Response(429))
    with pytest.raises(TransientSendError):
        await make_client().send_email({"to": ["x@y.co"]})


@respx.mock
async def test_validation_error_raises_hard():
    route = respx.post(f"{API}/emails").mock(
        return_value=httpx.Response(422, json={"message": "Invalid `to`"})
    )
    with pytest.raises(HardSendError):
        await make_client().send_email({"to": ["bad"]})
    assert route.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_resend_client.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/resend_client.py`**

```python
import asyncio

import httpx

from app.services.ratelimit import RateLimiter

API_URL = "https://api.resend.com"
MAX_ATTEMPTS = 3


class SendError(Exception):
    pass


class TransientSendError(SendError):
    """Retryable at a later dispatch pass (429/5xx/network)."""


class HardSendError(SendError):
    """Permanent — do not retry (validation, auth)."""


class ResendClient:
    def __init__(self, api_key: str, rps: float = 2.0, backoff_base: float = 1.0):
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"}
        self._limiter = RateLimiter(rps)
        self._backoff_base = backoff_base

    async def send_email(self, payload: dict) -> str:
        """POST /emails; returns the Resend email id."""
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.wait()
            try:
                async with httpx.AsyncClient(base_url=API_URL, headers=self._headers,
                                             timeout=30) as client:
                    resp = await client.post("/emails", json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = TransientSendError(f"resend -> {resp.status_code}")
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise HardSendError(f"resend -> {resp.status_code}: {resp.text[:500]}")
            return resp.json()["id"]
        raise TransientSendError(f"send failed after {MAX_ATTEMPTS} attempts: {last_error}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_resend_client.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/resend_client.py tests/test_resend_client.py
git commit -m "feat: resend client with rps limiter and transient/hard error split"
```

---

### Task 12: Dispatch service (queue fill + send loop)

**Files:**
- Create: `app/services/dispatch.py`
- Test: `tests/test_dispatch.py`

Design notes for the implementer:
- `enqueue_campaign_sends` fills `sends` from `campaign_contacts ⋈ contacts_cache`, skipping suppressed/DND at fill time, idempotent via `on conflict do nothing` (spec §5 idempotency).
- `process_send_queue` is one worker pass: claim up to `min(100, daily-cap remaining)` due queued sends (`status='queued' → 'sending'` with SKIP LOCKED), **re-check suppressions** (spec §5: the list moves between sync and send), render per-contact via the Node CLI (one subprocess call per campaign group), then send one-by-one through the RPS-limited Resend client. Every send carries `List-Unsubscribe` + `List-Unsubscribe-Post` headers (spec §12: no send without them).
- Transient failure → back to `queued` with `retry_count+1` and exponential `next_attempt_at`; after 3 retries or on hard failure → `failed` with reason. Crash recovery: `requeue_stale` returns `sending` rows older than 10 minutes to `queued`.

- [ ] **Step 1: Write the failing test**

`tests/test_dispatch.py`:
```python
import httpx
import respx

from app.services.dispatch import enqueue_campaign_sends, process_send_queue, requeue_stale
from app.services.resend_client import ResendClient
from app.services.suppressions import add_suppression
from tests.helpers import make_settings

RESEND_API = "https://api.resend.com/emails"


async def seed_campaign(pool, n_contacts=3, status="ready"):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('launch', 'Big Launch', 'welcome', 'v1', $1) returning id", status)
    for i in range(n_contacts):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email, first_name) "
            "values ($1, $2, $3)", f"c{i}", f"user{i}@x.co", f"User{i}")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
            cid, f"c{i}")
    return cid


async def test_enqueue_is_idempotent_and_skips_suppressed(pool):
    cid = await seed_campaign(pool)
    await add_suppression(pool, "user1@x.co", reason="complaint", source="resend")
    assert await enqueue_campaign_sends(pool, cid) == 2
    assert await enqueue_campaign_sends(pool, cid) == 0  # rerun inserts nothing
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "dispatching"


@respx.mock
async def test_process_sends_updates_rows_and_sets_headers(pool):
    route = respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    cid = await seed_campaign(pool, n_contacts=2)
    await enqueue_campaign_sends(pool, cid)
    settings = make_settings()
    sent = await process_send_queue(pool, settings, ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 2
    rows = await pool.fetch("select status, resend_email_id, rendered_hash, sent_at from sends")
    assert all(r["status"] == "sent" and r["resend_email_id"] == "em_1"
               and r["rendered_hash"] and r["sent_at"] for r in rows)
    body = route.calls[0].request.read().decode()
    assert "List-Unsubscribe" in body and "List-Unsubscribe=One-Click" in body
    assert "/u/" in body  # signed unsub URL made it into html + headers
    # queue drained → campaign completed
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "completed"


@respx.mock
async def test_daily_cap_limits_batch(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    cid = await seed_campaign(pool, n_contacts=3)
    await enqueue_campaign_sends(pool, cid)
    settings = make_settings(daily_send_cap=2)
    resend = ResendClient("re_test", rps=10_000, backoff_base=0)
    assert await process_send_queue(pool, settings, resend) == 2
    assert await process_send_queue(pool, settings, resend) == 0  # cap hit, resumes next day
    assert (await pool.fetchval("select count(*) from sends where status='queued'")) == 1


@respx.mock
async def test_suppression_rechecked_at_dispatch(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    cid = await seed_campaign(pool, n_contacts=2)
    await enqueue_campaign_sends(pool, cid)
    await add_suppression(pool, "user0@x.co", reason="unsubscribe", source="unsub_page")
    sent = await process_send_queue(pool, make_settings(),
                                    ResendClient("re_test", rps=10_000, backoff_base=0))
    assert sent == 1
    assert (await pool.fetchval(
        "select status from sends where email='user0@x.co'")) == "suppressed"


@respx.mock
async def test_transient_failure_requeues_with_backoff_then_fails(pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(500))
    cid = await seed_campaign(pool, n_contacts=1)
    await enqueue_campaign_sends(pool, cid)
    settings = make_settings()
    resend = ResendClient("re_test", rps=10_000, backoff_base=0)
    await process_send_queue(pool, settings, resend)
    row = await pool.fetchrow("select status, retry_count, next_attempt_at > now() as delayed from sends")
    assert row["status"] == "queued" and row["retry_count"] == 1 and row["delayed"]
    # force due and exhaust remaining retries (MAX_SEND_RETRIES=3: rc 1→2 requeues, rc 2→3 fails,
    # once failed the claim query no longer picks it up)
    for expected in ("queued", "failed", "failed"):
        await pool.execute("update sends set next_attempt_at = now() where status='queued'")
        await process_send_queue(pool, settings, resend)
        assert (await pool.fetchval("select status from sends")) == expected


async def test_requeue_stale_recovers_crashed_sends(pool):
    cid = await seed_campaign(pool, n_contacts=1, status="dispatching")
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email, status, created_at) "
        "values ($1, 'c0', 'user0@x.co', 'sending', now() - interval '20 minutes')", cid)
    await pool.execute("update sends set next_attempt_at = now() - interval '20 minutes'")
    assert await requeue_stale(pool) == 1
    assert (await pool.fetchval("select status from sends")) == "queued"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dispatch.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/dispatch.py`**

```python
import json
import logging
from collections import defaultdict

from app.config import Settings
from app.services.renderer import RenderError, render_batch
from app.services.resend_client import HardSendError, ResendClient, TransientSendError
from app.services.suppressions import suppressed_subset
from app.services.unsub_tokens import make_token

log = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_SEND_RETRIES = 3
RETRY_BASE_SECONDS = 120


async def enqueue_campaign_sends(pool, campaign_id) -> int:
    """Fill the send queue for a campaign. Idempotent; marks campaign dispatching."""
    inserted = await pool.fetchval(
        """with ins as (
               insert into sends (campaign_id, ghl_contact_id, email)
               select cc.campaign_id, cc.ghl_contact_id, c.email
               from campaign_contacts cc
               join contacts_cache c using (ghl_contact_id)
               where cc.campaign_id = $1
                 and c.dnd = false
                 and not exists (select 1 from suppressions s where s.email = c.email)
               on conflict (campaign_id, ghl_contact_id) do nothing
               returning 1)
           select count(*) from ins""",
        campaign_id,
    )
    await pool.execute(
        "update campaigns set status='dispatching' where id=$1 and status in ('draft','ready')",
        campaign_id,
    )
    return inserted


async def requeue_stale(pool, stale_minutes: int = 10) -> int:
    """Return crashed 'sending' claims to the queue (worker restart recovery)."""
    result = await pool.execute(
        "update sends set status='queued' where status='sending' "
        "and next_attempt_at < now() - make_interval(mins => $1)", stale_minutes)
    return int(result.split()[-1])


def _unsub_url(settings: Settings, email: str, campaign_id) -> str:
    token = make_token(email, str(campaign_id), settings.unsub_signing_secret)
    return f"{settings.public_base_url}/u/{token}"


def build_headers(settings: Settings, unsub_url: str) -> dict:
    return {
        "List-Unsubscribe": f"<mailto:unsubscribe@{settings.from_domain}>, <{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


async def _claim_batch(pool, limit: int) -> list[dict]:
    rows = await pool.fetch(
        """update sends set status='sending', next_attempt_at=now()
           where id in (
               select s.id from sends s
               join campaigns c on c.id = s.campaign_id
               where s.status='queued' and s.next_attempt_at <= now()
                 and c.status='dispatching'
               order by s.created_at
               limit $1
               for update of s skip locked)
           returning id, campaign_id, ghl_contact_id, email, retry_count""",
        limit,
    )
    return [dict(r) for r in rows]


async def _mark_transient_failure(pool, send_id, retry_count: int, error: str) -> None:
    if retry_count + 1 >= MAX_SEND_RETRIES:
        await pool.execute(
            "update sends set status='failed', error=$2 where id=$1", send_id, error[:500])
    else:
        await pool.execute(
            """update sends set status='queued', retry_count=retry_count+1,
                   next_attempt_at=now() + make_interval(secs => $2 * power(2, retry_count)),
                   error=$3
               where id=$1""",
            send_id, RETRY_BASE_SECONDS, error[:500],
        )


async def process_send_queue(pool, settings: Settings, resend: ResendClient) -> int:
    """One worker pass. Returns number of emails successfully handed to Resend."""
    sent_today = await pool.fetchval(
        "select count(*) from sends where sent_at >= date_trunc('day', now())")
    remaining = settings.daily_send_cap - sent_today
    if remaining <= 0:
        log.info("daily cap %s reached; dispatch resumes tomorrow", settings.daily_send_cap)
        return 0

    claimed = await _claim_batch(pool, min(BATCH_SIZE, remaining))
    if not claimed:
        return 0

    # Dispatch-time suppression re-check (spec §5)
    suppressed = await suppressed_subset(pool, [s["email"] for s in claimed])
    to_send = []
    for send in claimed:
        if send["email"] in suppressed:
            await pool.execute(
                "update sends set status='suppressed' where id=$1", send["id"])
        else:
            to_send.append(send)

    # Group by campaign so each group is one render subprocess call
    by_campaign: dict = defaultdict(list)
    for send in to_send:
        by_campaign[send["campaign_id"]].append(send)

    sent_count = 0
    for campaign_id, sends in by_campaign.items():
        campaign = await pool.fetchrow(
            "select subject, template_ref from campaigns where id=$1", campaign_id)
        props_list = []
        for send in sends:
            contact = await pool.fetchrow(
                "select first_name, last_name, custom from contacts_cache "
                "where ghl_contact_id=$1", send["ghl_contact_id"])
            custom = json.loads(contact["custom"]) if contact else {}
            props_list.append({
                "firstName": (contact["first_name"] if contact else "") or None,
                "lastName": (contact["last_name"] if contact else "") or None,
                **custom,
                "unsubUrl": _unsub_url(settings, send["email"], campaign_id),
            })
        try:
            rendered = await render_batch(campaign["template_ref"], props_list)
        except RenderError as exc:
            log.error("render failed for campaign %s: %s", campaign_id, exc)
            for send in sends:
                await _mark_transient_failure(pool, send["id"], send["retry_count"], str(exc))
            continue

        for send, props, r in zip(sends, props_list, rendered):
            payload = {
                "from": settings.from_email,
                "to": [send["email"]],
                "subject": campaign["subject"],
                "html": r.html,
                "text": r.text,
                "headers": build_headers(settings, props["unsubUrl"]),
            }
            try:
                email_id = await resend.send_email(payload)
            except TransientSendError as exc:
                await _mark_transient_failure(pool, send["id"], send["retry_count"], str(exc))
                continue
            except HardSendError as exc:
                await pool.execute(
                    "update sends set status='failed', error=$2 where id=$1",
                    send["id"], str(exc)[:500])
                continue
            await pool.execute(
                "update sends set status='sent', resend_email_id=$2, rendered_hash=$3, "
                "sent_at=now() where id=$1",
                send["id"], email_id, r.hash)
            sent_count += 1

    # Close out campaigns whose queues drained
    await pool.execute(
        """update campaigns set status='completed'
           where status='dispatching'
             and not exists (select 1 from sends
                             where campaign_id = campaigns.id
                               and status in ('queued', 'sending'))""")
    return sent_count
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_dispatch.py -v
```
Expected: 6 PASS

- [ ] **Step 5: Run full suite to check for regressions**

```bash
uv run pytest -q
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/dispatch.py tests/test_dispatch.py
git commit -m "feat: dispatch worker pass (cap, suppression recheck, headers, retries)"
```

---

### Task 13: Guardrails (kill rule → auto-pause)

**Files:**
- Create: `app/services/guardrails.py`
- Test: `tests/test_guardrails.py`

- [ ] **Step 1: Write the failing test**

`tests/test_guardrails.py`:
```python
import httpx
import respx

from app.services.guardrails import check_and_pause


async def seed_day(pool, sent: int, bounced: int, complained: int):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('x', 's', 'welcome', 'v1', 'dispatching') returning id")
    for i in range(sent):
        send_id = await pool.fetchval(
            "insert into sends (campaign_id, ghl_contact_id, email, status, sent_at) "
            "values ($1, $2, $3, 'sent', now()) returning id", cid, f"c{i}", f"u{i}@x.co")
        if i < bounced:
            await pool.execute(
                "insert into events (send_id, type) values ($1, 'email.bounced')", send_id)
        elif i < bounced + complained:
            await pool.execute(
                "insert into events (send_id, type) values ($1, 'email.complained')", send_id)
    return cid


async def test_below_thresholds_no_pause(pool):
    cid = await seed_day(pool, sent=1000, bounced=10, complained=0)  # 1% bounce
    assert await check_and_pause(pool) is False
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "dispatching"


async def test_bounce_breach_pauses(pool):
    cid = await seed_day(pool, sent=1000, bounced=40, complained=0)  # 4% > 3%
    assert await check_and_pause(pool) is True
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "paused"


async def test_complaint_breach_pauses_and_alerts(pool):
    await seed_day(pool, sent=1000, bounced=0, complained=2)  # 0.2% > 0.1%
    with respx.mock:
        alert = respx.post("https://hooks.example.com/alert").mock(
            return_value=httpx.Response(200))
        assert await check_and_pause(pool, alert_webhook_url="https://hooks.example.com/alert") is True
        assert alert.called
        assert b"complaint" in alert.calls[0].request.read()


async def test_low_volume_days_never_trip(pool):
    await seed_day(pool, sent=10, bounced=5, complained=2)  # tiny sample
    assert await check_and_pause(pool) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_guardrails.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/guardrails.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_guardrails.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/guardrails.py tests/test_guardrails.py
git commit -m "feat: daily bounce/complaint kill rule with auto-pause and alert"
```

---

### Task 14: FastAPI app + campaigns router

**Files:**
- Create: `app/db.py`, `app/main.py`, `app/routers/campaigns.py`
- Modify: `tests/conftest.py` (add `client` fixture)
- Test: `tests/test_api_campaigns.py`

- [ ] **Step 1: Add the `client` fixture to `tests/conftest.py`** (append to existing file)

```python
import httpx as _httpx


@pytest.fixture
async def client(pool):
    from app.main import create_app
    from tests.helpers import make_settings

    app = create_app()
    app.state.pool = pool
    app.state.settings = make_settings()
    transport = _httpx.ASGITransport(app=app)
    async with _httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
```

- [ ] **Step 2: Write the failing test**

`tests/test_api_campaigns.py`:
```python
import httpx
import respx

RESEND_API = "https://api.resend.com/emails"


async def create_campaign(client) -> str:
    resp = await client.post("/campaigns", json={
        "name": "July Launch",
        "subject": "The July launch is here",
        "template_ref": "welcome",
        "template_version": "v1",
        "audience_filter": [{"field": "tags", "operator": "eq", "value": "newsletter"}],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_create_campaign(client, pool):
    campaign_id = await create_campaign(client)
    row = await pool.fetchrow("select name, status, subject from campaigns")
    assert str(await pool.fetchval("select id from campaigns")) == campaign_id
    assert row["status"] == "draft" and row["subject"] == "The July launch is here"


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200


@respx.mock
async def test_test_send_goes_to_seed_list_only(client, pool):
    route = respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_t"}))
    campaign_id = await create_campaign(client)
    resp = await client.post(f"/campaigns/{campaign_id}/test")
    assert resp.status_code == 200
    assert resp.json() == {"sent_to": ["seed@growthable.io"]}
    body = route.calls[0].request.read().decode()
    assert "[TEST]" in body and "seed@growthable.io" in body
    assert (await pool.fetchval("select count(*) from sends")) == 0  # no real sends recorded


@respx.mock
async def test_dispatch_and_report_flow(client, pool):
    respx.post(RESEND_API).mock(return_value=httpx.Response(200, json={"id": "em_1"}))
    campaign_id = await create_campaign(client)
    # audience present (as if sync-audience already ran)
    for i in range(2):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email) values ($1, $2)",
            f"c{i}", f"u{i}@x.co")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) "
            "values ($1::uuid, $2)", campaign_id, f"c{i}")
    resp = await client.post(f"/campaigns/{campaign_id}/dispatch")
    assert resp.status_code == 200 and resp.json() == {"queued": 2}
    # simulate a worker pass + one delivered event
    from app.services.dispatch import process_send_queue
    from app.services.resend_client import ResendClient
    from tests.helpers import make_settings
    await process_send_queue(pool, make_settings(), ResendClient("re", rps=10_000, backoff_base=0))
    send_id = await pool.fetchval("select id from sends limit 1")
    await pool.execute(
        "insert into events (send_id, type) values ($1, 'email.delivered')", send_id)
    resp = await client.get(f"/campaigns/{campaign_id}/report")
    report = resp.json()
    assert report["sends"]["sent"] == 2
    assert report["events"]["email.delivered"] == 1


async def test_sync_audience_endpoint_calls_ghl(client, pool, monkeypatch):
    calls = {}

    async def fake_sync(pool_, ghl, campaign_id):
        calls["campaign_id"] = str(campaign_id)
        return {"kept": 5, "dropped": 1}

    import app.routers.campaigns as campaigns_router
    monkeypatch.setattr(campaigns_router, "sync_audience", fake_sync)
    campaign_id = await create_campaign(client)
    resp = await client.post(f"/campaigns/{campaign_id}/sync-audience")
    assert resp.status_code == 200 and resp.json() == {"kept": 5, "dropped": 1}
    assert calls["campaign_id"] == campaign_id
```

- [ ] **Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_api_campaigns.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 4: Write `app/db.py`**

```python
import asyncpg


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)
```

- [ ] **Step 5: Write `app/routers/campaigns.py`**

```python
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.audience import sync_audience
from app.services.dispatch import build_headers, enqueue_campaign_sends, _unsub_url
from app.services.ghl import GHLClient
from app.services.renderer import render_batch
from app.services.resend_client import ResendClient

router = APIRouter()


class CampaignIn(BaseModel):
    name: str
    subject: str
    template_ref: str
    template_version: str
    audience_filter: list[dict] = []
    scheduled_at: str | None = None


async def _get_campaign(request: Request, campaign_id: str):
    try:
        cid = uuid.UUID(campaign_id)
    except ValueError:
        raise HTTPException(404, "campaign not found")
    row = await request.app.state.pool.fetchrow("select * from campaigns where id=$1", cid)
    if row is None:
        raise HTTPException(404, "campaign not found")
    return row


@router.post("/campaigns", status_code=201)
async def create_campaign(request: Request, body: CampaignIn):
    row = await request.app.state.pool.fetchrow(
        "insert into campaigns (name, subject, template_ref, template_version, audience_filter) "
        "values ($1, $2, $3, $4, $5) returning id, status",
        body.name, body.subject, body.template_ref, body.template_version,
        json.dumps(body.audience_filter),
    )
    return {"id": str(row["id"]), "status": row["status"]}


@router.post("/campaigns/{campaign_id}/sync-audience")
async def sync_campaign_audience(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    settings = request.app.state.settings
    ghl = GHLClient(settings.ghl_pi_token, settings.ghl_location_id)
    return await sync_audience(request.app.state.pool, ghl, str(campaign["id"]))


@router.post("/campaigns/{campaign_id}/test")
async def test_send(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    settings = request.app.state.settings
    if not settings.seed_list:
        raise HTTPException(400, "SEED_EMAILS is not configured")
    resend = ResendClient(settings.resend_api_key, rps=settings.send_rps)
    for email in settings.seed_list:
        unsub = _unsub_url(settings, email, campaign["id"])
        rendered = (await render_batch(campaign["template_ref"], [{
            "firstName": "Seed", "unsubUrl": unsub,
        }]))[0]
        await resend.send_email({
            "from": settings.from_email,
            "to": [email],
            "subject": f"[TEST] {campaign['subject']}",
            "html": rendered.html,
            "text": rendered.text,
            "headers": build_headers(settings, unsub),
        })
    return {"sent_to": settings.seed_list}


@router.post("/campaigns/{campaign_id}/dispatch")
async def dispatch_campaign(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    if campaign["status"] == "paused":
        raise HTTPException(409, "campaign is paused by guardrails")
    queued = await enqueue_campaign_sends(request.app.state.pool, campaign["id"])
    return {"queued": queued}


@router.get("/campaigns/{campaign_id}/report")
async def campaign_report(request: Request, campaign_id: str):
    campaign = await _get_campaign(request, campaign_id)
    pool = request.app.state.pool
    sends = await pool.fetchrow(
        """select count(*) as total,
                  count(*) filter (where status='sent') as sent,
                  count(*) filter (where status='queued') as queued,
                  count(*) filter (where status='failed') as failed,
                  count(*) filter (where status='suppressed') as suppressed
           from sends where campaign_id=$1""", campaign["id"])
    events = await pool.fetch(
        """select e.type, count(distinct e.send_id) as n
           from events e join sends s on s.id = e.send_id
           where s.campaign_id=$1 group by e.type""", campaign["id"])
    return {
        "campaign": {"id": str(campaign["id"]), "name": campaign["name"],
                     "status": campaign["status"]},
        "sends": dict(sends),
        "events": {r["type"]: r["n"] for r in events},
    }
```

- [ ] **Step 6: Write `app/main.py`**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import create_pool
from app.routers import campaigns


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not hasattr(app.state, "settings"):  # tests inject state directly
            app.state.settings = get_settings()
            app.state.pool = await create_pool(app.state.settings.database_url)
        yield
        if getattr(app.state, "pool", None) is not None:
            await app.state.pool.close()

    app = FastAPI(title="growthable-email", lifespan=lifespan)
    app.include_router(campaigns.router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


app = create_app()
```
(Note: the module-level `app` is what `uvicorn app.main:app` serves. The lifespan close-guard exists because the test fixture injects a session-scoped pool that conftest owns — but ASGITransport doesn't run lifespan at all, so it's only a prod path.)

- [ ] **Step 7: Run test to verify it passes**

```bash
uv run pytest tests/test_api_campaigns.py -v
```
Expected: 5 PASS

- [ ] **Step 8: Commit**

```bash
git add app tests
git commit -m "feat: FastAPI app with campaign lifecycle endpoints"
```

---

### Task 15: Resend webhook (events in, suppression + write-back out)

**Files:**
- Create: `app/routers/webhooks.py` (resend part; GHL inbound added in Task 16)
- Modify: `app/main.py` (include router)
- Test: `tests/test_webhook_resend.py`

- [ ] **Step 1: Write the failing test**

`tests/test_webhook_resend.py`:
```python
import json

from tests.helpers import make_settings, svix_headers

SECRET = make_settings().resend_webhook_secret


async def seed_send(pool, email="u@x.co", resend_id="em_1"):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('July Launch', 's', 'welcome', 'v1') returning id")
    return await pool.fetchval(
        "insert into sends (campaign_id, ghl_contact_id, email, status, resend_email_id, sent_at) "
        "values ($1, 'c1', $2, 'sent', $3, now()) returning id", cid, email, resend_id)


def event_payload(event_type: str, email_id: str = "em_1", extra: dict | None = None) -> str:
    data = {"email_id": email_id, "to": ["u@x.co"], **(extra or {})}
    return json.dumps({"type": event_type, "created_at": "2026-07-07T00:00:00Z", "data": data})


async def post_event(client, payload: str, headers=None):
    return await client.post("/webhooks/resend", content=payload,
                             headers=headers or svix_headers(SECRET, payload))


async def test_rejects_bad_signature(client, pool):
    payload = event_payload("email.delivered")
    resp = await post_event(client, payload, headers=svix_headers("whsec_" + "x" * 43, payload))
    assert resp.status_code == 401
    assert (await pool.fetchval("select count(*) from events")) == 0


async def test_delivered_persists_event_and_enqueues_tag_job(client, pool):
    send_id = await seed_send(pool)
    resp = await post_event(client, event_payload("email.delivered"))
    assert resp.status_code == 200
    event = await pool.fetchrow("select send_id, type from events")
    assert event["send_id"] == send_id and event["type"] == "email.delivered"
    job = await pool.fetchrow("select data from jobs where name='ghl_writeback'")
    data = json.loads(job["data"])
    assert data == {"kind": "add_tags", "contact_id": "c1", "tags": ["emailed-july-launch"]}


async def test_opened_and_clicked_tag_prefixes(client, pool):
    await seed_send(pool)
    await post_event(client, event_payload("email.opened"))
    await post_event(client, event_payload("email.clicked"))
    rows = await pool.fetch("select data from jobs order by created_at")
    tags = [json.loads(r["data"])["tags"][0] for r in rows]
    assert tags == ["opened-july-launch", "clicked-july-launch"]


async def test_hard_bounce_suppresses_and_dnds(client, pool):
    await seed_send(pool)
    payload = event_payload("email.bounced", extra={"bounce": {"type": "Permanent"}})
    await post_event(client, payload)
    row = await pool.fetchrow("select reason, source, ghl_contact_id from suppressions")
    assert row["reason"] == "hard_bounce" and row["source"] == "resend"
    assert row["ghl_contact_id"] == "c1"
    kinds = {json.loads(r["data"])["kind"] for r in await pool.fetch("select data from jobs")}
    assert kinds == {"set_dnd"}


async def test_soft_bounce_records_event_only(client, pool):
    await seed_send(pool)
    await post_event(client, event_payload("email.bounced", extra={"bounce": {"type": "Transient"}}))
    assert (await pool.fetchval("select count(*) from suppressions")) == 0
    assert (await pool.fetchval("select count(*) from events")) == 1


async def test_complaint_suppresses_dnds_and_tags(client, pool):
    await seed_send(pool)
    await post_event(client, event_payload("email.complained"))
    assert (await pool.fetchval("select reason from suppressions")) == "complaint"
    jobs = [json.loads(r["data"]) for r in await pool.fetch("select data from jobs order by created_at")]
    kinds = {j["kind"] for j in jobs}
    assert kinds == {"set_dnd", "add_tags"}
    tag_job = next(j for j in jobs if j["kind"] == "add_tags")
    assert tag_job["tags"] == ["complained"]


async def test_unknown_email_id_stores_orphan_event(client, pool):
    resp = await post_event(client, event_payload("email.delivered", email_id="em_unknown"))
    assert resp.status_code == 200
    assert (await pool.fetchval("select send_id from events")) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_webhook_resend.py -v
```
Expected: FAIL — 404s (router doesn't exist)

- [ ] **Step 3: Write `app/routers/webhooks.py`**

```python
import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from svix.webhooks import Webhook, WebhookVerificationError

from app.services.jobs import enqueue
from app.services.suppressions import add_suppression

log = logging.getLogger(__name__)
router = APIRouter()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


ENGAGEMENT_TAG_PREFIX = {
    "email.delivered": "emailed",
    "email.opened": "opened",
    "email.clicked": "clicked",
}


@router.post("/webhooks/resend")
async def resend_webhook(request: Request):
    payload = await request.body()
    settings = request.app.state.settings
    try:
        event = Webhook(settings.resend_webhook_secret).verify(
            payload, dict(request.headers))
    except WebhookVerificationError:
        raise HTTPException(401, "invalid signature")

    pool = request.app.state.pool
    event_type = event.get("type", "")
    data = event.get("data") or {}
    email_id = data.get("email_id")
    send = None
    if email_id:
        send = await pool.fetchrow(
            """select s.id, s.ghl_contact_id, s.email, c.name as campaign_name
               from sends s join campaigns c on c.id = s.campaign_id
               where s.resend_email_id = $1""", email_id)

    await pool.execute(
        "insert into events (send_id, type, payload) values ($1, $2, $3)",
        send["id"] if send else None, event_type, json.dumps(event))

    if send is None:
        log.warning("resend event %s for unknown email_id %s", event_type, email_id)
        return {"ok": True}

    slug = slugify(send["campaign_name"])
    if event_type in ENGAGEMENT_TAG_PREFIX:
        await enqueue(pool, "ghl_writeback", {
            "kind": "add_tags", "contact_id": send["ghl_contact_id"],
            "tags": [f"{ENGAGEMENT_TAG_PREFIX[event_type]}-{slug}"]})
    elif event_type == "email.bounced":
        bounce_type = (data.get("bounce") or {}).get("type", "Permanent")
        if bounce_type != "Transient":  # treat unknown as hard (conservative)
            await add_suppression(pool, send["email"], reason="hard_bounce",
                                  source="resend", ghl_contact_id=send["ghl_contact_id"])
            await enqueue(pool, "ghl_writeback",
                          {"kind": "set_dnd", "contact_id": send["ghl_contact_id"]})
    elif event_type == "email.complained":
        await add_suppression(pool, send["email"], reason="complaint",
                              source="resend", ghl_contact_id=send["ghl_contact_id"])
        await enqueue(pool, "ghl_writeback",
                      {"kind": "set_dnd", "contact_id": send["ghl_contact_id"]})
        await enqueue(pool, "ghl_writeback", {
            "kind": "add_tags", "contact_id": send["ghl_contact_id"],
            "tags": ["complained"]})
    return {"ok": True}
```

- [ ] **Step 4: Register the router in `app/main.py`**

In `create_app()`, after the campaigns include:
```python
from app.routers import campaigns, unsub, webhooks   # top of file (unsub arrives in Task 16;
                                                     # for now import webhooks only)
    app.include_router(webhooks.router)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_webhook_resend.py -v
```
Expected: 7 PASS

- [ ] **Step 6: Commit**

```bash
git add app tests
git commit -m "feat: svix-verified resend webhook with suppression + write-back enqueue"
```

---

### Task 16: GHL inbound webhooks + unsubscribe endpoint

**Files:**
- Modify: `app/routers/webhooks.py` (add `/webhooks/ghl/enroll`, `/webhooks/ghl/dnd`)
- Create: `app/routers/unsub.py`
- Modify: `app/main.py` (include unsub router)
- Test: `tests/test_inbound_and_unsub.py`

- [ ] **Step 1: Write the failing test**

`tests/test_inbound_and_unsub.py`:
```python
import json

from app.services.suppressions import add_suppression
from app.services.unsub_tokens import make_token
from tests.helpers import make_settings

AUTH = {"x-webhook-secret": "hook-secret"}


async def seed_campaign(pool, status="ready"):
    return str(await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('drip', 's', 'welcome', 'v1', $1) returning id", status))


async def test_enroll_requires_secret(client, pool):
    cid = await seed_campaign(pool)
    resp = await client.post("/webhooks/ghl/enroll", json={
        "campaign_id": cid, "contact_id": "c1", "email": "a@b.co"})
    assert resp.status_code == 403


async def test_enroll_queues_send_and_activates_campaign(client, pool):
    cid = await seed_campaign(pool)
    resp = await client.post("/webhooks/ghl/enroll", headers=AUTH, json={
        "campaign_id": cid, "contact_id": "c1", "email": "Ada@B.co",
        "first_name": "Ada", "custom": {"plan": "gold"}})
    assert resp.status_code == 200 and resp.json() == {"enrolled": True}
    send = await pool.fetchrow("select email, status from sends")
    assert send["email"] == "ada@b.co" and send["status"] == "queued"
    assert (await pool.fetchval("select status from campaigns")) == "dispatching"
    cached = await pool.fetchrow("select first_name, custom from contacts_cache")
    assert cached["first_name"] == "Ada" and json.loads(cached["custom"]) == {"plan": "gold"}


async def test_enroll_rejects_suppressed(client, pool):
    cid = await seed_campaign(pool)
    await add_suppression(pool, "a@b.co", reason="complaint", source="resend")
    resp = await client.post("/webhooks/ghl/enroll", headers=AUTH, json={
        "campaign_id": cid, "contact_id": "c1", "email": "a@b.co"})
    assert resp.status_code == 200
    assert resp.json() == {"enrolled": False, "reason": "suppressed"}
    assert (await pool.fetchval("select count(*) from sends")) == 0


async def test_dnd_webhook_suppresses(client, pool):
    resp = await client.post("/webhooks/ghl/dnd", headers=AUTH,
                             json={"email": "Gone@x.co", "contact_id": "c9"})
    assert resp.status_code == 200
    row = await pool.fetchrow("select email, reason, source from suppressions")
    assert row["email"] == "gone@x.co" and row["reason"] == "ghl_dnd" and row["source"] == "ghl"


async def test_unsub_get_and_post_suppress_and_queue_dnd(client, pool):
    cid = await seed_campaign(pool)
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email) values ('c1', 'u@x.co')")
    token = make_token("u@x.co", cid, make_settings().unsub_signing_secret)
    resp = await client.post(f"/u/{token}")  # RFC 8058 one-click
    assert resp.status_code == 200
    assert (await pool.fetchval("select reason from suppressions")) == "unsubscribe"
    job = json.loads(await pool.fetchval("select data from jobs where name='ghl_writeback'"))
    assert job == {"kind": "set_dnd", "contact_id": "c1"}
    # GET (human click) is idempotent and renders confirmation HTML
    resp = await client.get(f"/u/{token}")
    assert resp.status_code == 200 and "unsubscribed" in resp.text.lower()
    assert (await pool.fetchval("select count(*) from suppressions")) == 1


async def test_unsub_bad_token_404(client):
    assert (await client.get("/u/garbage")).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_inbound_and_unsub.py -v
```
Expected: FAIL — 404s

- [ ] **Step 3: Add GHL inbound routes to `app/routers/webhooks.py`** (append)

```python
from pydantic import BaseModel

from app.services.suppressions import is_suppressed, normalize


def _check_ghl_secret(request: Request) -> None:
    if request.headers.get("x-webhook-secret") != request.app.state.settings.ghl_webhook_secret:
        raise HTTPException(403, "bad webhook secret")


class EnrollIn(BaseModel):
    campaign_id: str
    contact_id: str
    email: str
    first_name: str = ""
    last_name: str = ""
    custom: dict = {}


@router.post("/webhooks/ghl/enroll")
async def ghl_enroll(request: Request, body: EnrollIn):
    _check_ghl_secret(request)
    pool = request.app.state.pool
    email = normalize(body.email)
    if await is_suppressed(pool, email):
        return {"enrolled": False, "reason": "suppressed"}
    campaign = await pool.fetchrow(
        "select id, status from campaigns where id=$1::uuid", body.campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    if campaign["status"] == "paused":
        return {"enrolled": False, "reason": "campaign paused"}
    await pool.execute(
        """insert into contacts_cache (ghl_contact_id, email, first_name, last_name, custom)
           values ($1, $2, $3, $4, $5)
           on conflict (ghl_contact_id) do update set
               email=excluded.email, first_name=excluded.first_name,
               last_name=excluded.last_name, custom=excluded.custom, synced_at=now()""",
        body.contact_id, email, body.first_name, body.last_name, json.dumps(body.custom))
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2) "
        "on conflict do nothing", campaign["id"], body.contact_id)
    await pool.execute(
        "insert into sends (campaign_id, ghl_contact_id, email) values ($1, $2, $3) "
        "on conflict (campaign_id, ghl_contact_id) do nothing",
        campaign["id"], body.contact_id, email)
    await pool.execute(
        "update campaigns set status='dispatching' where id=$1 and status in ('draft','ready')",
        campaign["id"])
    return {"enrolled": True}


class DndIn(BaseModel):
    email: str
    contact_id: str | None = None


@router.post("/webhooks/ghl/dnd")
async def ghl_dnd(request: Request, body: DndIn):
    _check_ghl_secret(request)
    await add_suppression(request.app.state.pool, body.email, reason="ghl_dnd",
                          source="ghl", ghl_contact_id=body.contact_id)
    return {"ok": True}
```

- [ ] **Step 4: Write `app/routers/unsub.py`**

```python
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.services.jobs import enqueue
from app.services.suppressions import add_suppression
from app.services.unsub_tokens import parse_token

router = APIRouter()

CONFIRMATION_HTML = """<!doctype html>
<html><head><title>Unsubscribed</title></head>
<body style="font-family: Helvetica, Arial, sans-serif; max-width: 480px; margin: 80px auto; text-align: center;">
  <h1>You're unsubscribed</h1>
  <p>{email} won't receive any more marketing email from Growthable.</p>
</body></html>"""


async def _unsubscribe(request: Request, token: str) -> HTMLResponse:
    settings = request.app.state.settings
    parsed = parse_token(token, settings.unsub_signing_secret)
    if parsed is None:
        raise HTTPException(404, "invalid link")
    email, _campaign_id = parsed
    pool = request.app.state.pool
    contact_id = await pool.fetchval(
        "select ghl_contact_id from contacts_cache where email=$1", email)
    already = await pool.fetchval(
        "select exists(select 1 from suppressions where email=$1)", email)
    await add_suppression(pool, email, reason="unsubscribe", source="unsub_page",
                          ghl_contact_id=contact_id)
    if not already and contact_id:
        await enqueue(pool, "ghl_writeback", {"kind": "set_dnd", "contact_id": contact_id})
    return HTMLResponse(CONFIRMATION_HTML.format(email=email))


@router.get("/u/{token}")
async def unsubscribe_get(request: Request, token: str):
    return await _unsubscribe(request, token)


@router.post("/u/{token}")  # RFC 8058 one-click POST target
async def unsubscribe_post(request: Request, token: str):
    return await _unsubscribe(request, token)
```

- [ ] **Step 5: Register unsub router in `app/main.py`**

```python
from app.routers import campaigns, unsub, webhooks
    app.include_router(unsub.router)
```

- [ ] **Step 6: Run test to verify it passes**

```bash
uv run pytest tests/test_inbound_and_unsub.py -v
```
Expected: 6 PASS

- [ ] **Step 7: Commit**

```bash
git add app tests
git commit -m "feat: GHL inbound webhooks and one-click unsubscribe endpoint"
```

---

### Task 17: Write-back worker + worker entrypoint

**Files:**
- Create: `app/services/writeback.py`, `app/worker.py`
- Test: `tests/test_writeback.py`

- [ ] **Step 1: Write the failing test**

`tests/test_writeback.py`:
```python
from app.services.jobs import enqueue
from app.services.writeback import process_writeback_jobs


class FakeGHL:
    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_times = fail_times

    async def add_tags(self, contact_id, tags):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("ghl down")
        self.calls.append(("add_tags", contact_id, tuple(tags)))

    async def set_dnd_email(self, contact_id):
        self.calls.append(("set_dnd", contact_id))


async def test_processes_tag_and_dnd_jobs(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "add_tags", "contact_id": "c1", "tags": ["opened-x"]})
    await enqueue(pool, "ghl_writeback", {"kind": "set_dnd", "contact_id": "c2"})
    ghl = FakeGHL()
    assert await process_writeback_jobs(pool, ghl) == 2
    assert ("add_tags", "c1", ("opened-x",)) in ghl.calls
    assert ("set_dnd", "c2") in ghl.calls
    states = [r["state"] for r in await pool.fetch("select state from jobs")]
    assert states == ["completed", "completed"]


async def test_ghl_failure_retries_job_not_lost(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "set_dnd", "contact_id": "c1"})
    assert await process_writeback_jobs(pool, FakeGHL(fail_times=1)) == 0
    row = await pool.fetchrow("select state, retry_count from jobs")
    assert row["state"] == "created" and row["retry_count"] == 1


async def test_unknown_kind_dead_letters(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "explode"})
    for _ in range(3):
        await process_writeback_jobs(pool, FakeGHL())
        await pool.execute("update jobs set start_after = now()")
    assert (await pool.fetchval("select state from jobs")) == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_writeback.py -v
```
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `app/services/writeback.py`**

```python
import logging

from app.services.jobs import complete_job, fail_job, fetch_job

log = logging.getLogger(__name__)

MAX_JOBS_PER_PASS = 50


async def process_writeback_jobs(pool, ghl, backoff_seconds: int = 60) -> int:
    """Drain up to MAX_JOBS_PER_PASS ghl_writeback jobs. Independent of dispatch —
    a GHL outage retries jobs here and never blocks webhook ingestion (spec §6)."""
    done = 0
    for _ in range(MAX_JOBS_PER_PASS):
        job = await fetch_job(pool, "ghl_writeback")
        if job is None:
            break
        data = job["data"]
        try:
            kind = data["kind"]
            if kind == "add_tags":
                await ghl.add_tags(data["contact_id"], data["tags"])
            elif kind == "set_dnd":
                await ghl.set_dnd_email(data["contact_id"])
            else:
                raise ValueError(f"unknown writeback kind: {kind}")
        except Exception:
            log.exception("writeback job %s failed", job["id"])
            await fail_job(pool, job["id"], backoff_seconds=backoff_seconds)
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
```

- [ ] **Step 4: Write `app/worker.py`**

```python
"""Background worker: run as `python -m app.worker` (separate Render service)."""
import asyncio
import logging

from app.config import get_settings
from app.db import create_pool
from app.services.dispatch import process_send_queue, requeue_stale
from app.services.ghl import GHLClient
from app.services.guardrails import check_and_pause
from app.services.resend_client import ResendClient
from app.services.writeback import process_writeback_jobs

log = logging.getLogger("worker")

TICK_SECONDS = 5


async def run_forever() -> None:
    settings = get_settings()
    pool = await create_pool(settings.database_url)
    ghl = GHLClient(settings.ghl_pi_token, settings.ghl_location_id)
    resend = ResendClient(settings.resend_api_key, rps=settings.send_rps)
    log.info("worker up: cap=%s rps=%s", settings.daily_send_cap, settings.send_rps)
    while True:
        try:
            await requeue_stale(pool)
            breached = await check_and_pause(pool, settings.alert_webhook_url)
            await process_writeback_jobs(pool, ghl)
            if not breached:
                sent = await process_send_queue(pool, settings, resend)
                if sent:
                    log.info("dispatched %s emails", sent)
        except Exception:
            log.exception("worker tick failed")
        await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(run_forever())
```

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_writeback.py -v
```
Expected: 3 PASS

- [ ] **Step 6: Run the whole suite**

```bash
uv run pytest -q
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add app tests
git commit -m "feat: GHL write-back worker and worker entrypoint loop"
```

---

### Task 18: Deploy assets + runbook

**Files:**
- Create: `Dockerfile`, `render.yaml`
- Modify: `README.md` (runbook)

- [ ] **Step 1: Write `Dockerfile`** (one image serves both web and worker; needs Python AND Node because dispatch shells out to `emails/render.tsx`)

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY emails/package.json emails/package-lock.json ./emails/
RUN cd emails && npm ci --omit=dev && npm install --no-save tsx

COPY app ./app
COPY emails ./emails
COPY supabase ./supabase

ENV PATH="/srv/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
(Note: `tsx` is a devDependency for local work but the runtime needs it to execute `render.tsx`, hence the explicit `npm install --no-save tsx`. Generate `uv.lock` with `uv lock` if not present.)

- [ ] **Step 2: Build the image locally to verify**

```bash
uv lock
docker build -t growthable-email .
docker run --rm growthable-email python -c "import app.main; print('web ok')"
docker run --rm growthable-email node --version
```
Expected: `web ok`, `v22.x`

- [ ] **Step 3: Write `render.yaml`**

```yaml
services:
  - type: web
    name: growthable-email-api
    runtime: docker
    plan: starter
    healthCheckPath: /healthz
    envVars: &env
      - key: DATABASE_URL
        sync: false
      - key: RESEND_API_KEY
        sync: false
      - key: RESEND_WEBHOOK_SECRET
        sync: false
      - key: GHL_PI_TOKEN
        sync: false
      - key: GHL_LOCATION_ID
        sync: false
      - key: GHL_WEBHOOK_SECRET
        sync: false
      - key: UNSUB_SIGNING_SECRET
        sync: false
      - key: PUBLIC_BASE_URL
        sync: false
      - key: FROM_EMAIL
        sync: false
      - key: SEND_RPS
        value: "2"
      - key: DAILY_SEND_CAP
        value: "500"
      - key: SEED_EMAILS
        sync: false
      - key: ALERT_WEBHOOK_URL
        sync: false
  - type: worker
    name: growthable-email-worker
    runtime: docker
    plan: starter
    dockerCommand: python -m app.worker
    envVars: *env
```

- [ ] **Step 4: Append the runbook to `README.md`**

```markdown
## Runbook

### One-time setup (build order §11 of docs/spec.md)
1. **Supabase:** create project → run `supabase/migrations/0001_init.sql` in the SQL editor
   (or `supabase db push`). Grab the *connection pooler* URL → `DATABASE_URL`.
2. **Resend domain:** add sending subdomain (e.g. `news.growthable.io`) in Resend →
   add the SPF, DKIM and return-path DNS records they show → verify. NEVER point Resend
   at the subdomain GHL/LC-Email uses (spec §2/§12).
3. **Render:** `render blueprint launch` (or connect repo, Render reads render.yaml).
   Fill in the secret env vars on both services.
4. **Resend webhook:** dashboard → Webhooks → add `https://<api>/webhooks/resend`,
   subscribe to sent/delivered/opened/clicked/bounced/complained → copy the signing
   secret → `RESEND_WEBHOOK_SECRET`.
5. **GHL Private Integration:** Settings → Private Integrations → create with
   contacts read/write scope → `GHL_PI_TOKEN`, `GHL_LOCATION_ID`.
6. **GHL workflows:**
   - DND/unsub sync: workflow on "DND enabled / unsubscribed" → Webhook action →
     POST `https://<api>/webhooks/ghl/dnd` with header `x-webhook-secret: <GHL_WEBHOOK_SECRET>`
     and body `{"email": "{{contact.email}}", "contact_id": "{{contact.id}}"}`.
   - Behavioral enroll: workflow → Webhook action → POST `https://<api>/webhooks/ghl/enroll`
     with the same header and body
     `{"campaign_id": "<uuid>", "contact_id": "{{contact.id}}", "email": "{{contact.email}}", "first_name": "{{contact.first_name}}"}`.
7. **Templates:** replace the placeholder physical address in `emails/components/Layout.tsx`.

### Campaign flow
    POST /campaigns                      {name, subject, template_ref, template_version, audience_filter}
    POST /campaigns/{id}/sync-audience   pulls from GHL, applies ingest drop rules
    POST /campaigns/{id}/test            renders + sends to SEED_EMAILS — check headers, unsub, rendering
    POST /campaigns/{id}/dispatch        fills the queue; the worker drains it under caps
    GET  /campaigns/{id}/report          delivered/open/click/bounce/complaint rollup

### Ramp schedule (spec §2 — adjust DAILY_SEND_CAP on the worker, then redeploy)
| Day | DAILY_SEND_CAP | Segment |
|---|---|---|
| 1–2 | 500 | most engaged / most recent |
| 3–4 | 2000 | engaged |
| 5–7 | 5000–10000 | broaden |
| 8+ | full volume | remainder, engagement-sorted |

Kill rule (automatic): bounce > 3% or complaint > 0.1% on ≥200 sends/day pauses all
dispatching campaigns and posts to ALERT_WEBHOOK_URL. Un-pause by fixing the cause and
setting campaign status back to 'dispatching' in Supabase. Check the report endpoint daily
during ramp. SEND_RPS stays 2 until Resend approves a rate increase.

### Go-live checklist (spec §11.9)
- [ ] Seed test: `POST /campaigns/{id}/test` → inspect in Gmail: DKIM=news subdomain pass,
      List-Unsubscribe header present, one-click unsub works, plain-text part present,
      physical address in footer.
- [ ] Unsub flow: click footer link → confirmation page → suppression row + GHL DND set.
- [ ] Event flow: open/click the seed email → tags appear on the GHL contact.
- [ ] First real cohort: engagement-sorted top 500, DAILY_SEND_CAP=500.
```

- [ ] **Step 5: Final full-suite run**

```bash
uv run pytest -q && (cd emails && npm test)
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add Dockerfile render.yaml README.md uv.lock
git commit -m "feat: docker image, render blueprint, ops runbook"
```

---

## Post-plan verification (execution session)

After all tasks: run `uv run pytest -q` and `cd emails && npm test` one final time; then walk the spec §12 guardrails and confirm each maps to code:
1. Suppression at ingest (`audience.py`) **and** dispatch (`dispatch.py::process_send_queue`) ✓
2. No send without one-click headers (`dispatch.py::build_headers` on every payload) ✓
3. Ramp caps in code (`DAILY_SEND_CAP` in `process_send_queue`) ✓
4. Kill rule auto-pause (`guardrails.py`, called every worker tick) ✓
5. One canonical suppression store (`suppressions` table; Resend/GHL mirrored via jobs) ✓
6. Separate subdomains per ESP (runbook step 2 — operational, not code) ✓



