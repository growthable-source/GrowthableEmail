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
