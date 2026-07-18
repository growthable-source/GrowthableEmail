import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

# Supabase's session-mode pooler caps the project at 15 clients. Web (5) +
# worker (5) leaves headroom for deploy overlap, when old and new instances
# of both services hold connections simultaneously (2026-07-18 boot failures).
MAX_POOL_SIZE = 5


async def create_pool(dsn: str, attempts: int = 6) -> asyncpg.Pool:
    """Retry pool creation: during a deploy the outgoing instance still holds
    its connections for a minute — booting must outwait that, not crash."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=MAX_POOL_SIZE)
        except Exception:
            if attempt == attempts:
                raise
            log.exception("db pool creation failed (attempt %s/%s) — retrying",
                          attempt, attempts)
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("unreachable")
