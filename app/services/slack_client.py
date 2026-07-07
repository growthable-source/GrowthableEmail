import asyncio
import hashlib
import hmac
import time

import httpx

from app.services.ratelimit import RateLimiter

API_URL = "https://slack.com/api"
MAX_ATTEMPTS = 3


class SlackError(Exception):
    pass


def verify_slack_signature(signing_secret: str, timestamp: str | None, body: bytes,
                           signature: str | None, tolerance_seconds: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance_seconds:
        return False
    base = f"v0:{ts}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


class SlackClient:
    def __init__(self, token: str, rps: float = 1.0, backoff_base: float = 1.0):
        self._headers = {"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"}
        self._limiter = RateLimiter(rps)
        self._backoff_base = backoff_base

    async def _call(self, method: str, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.wait()
            try:
                async with httpx.AsyncClient(base_url=API_URL, headers=self._headers,
                                             timeout=30) as client:
                    resp = await client.post(f"/{method}", json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = SlackError(f"{method} -> {resp.status_code}")
                await asyncio.sleep(self._backoff_base * 2 ** attempt)
                continue
            data = resp.json()
            if not data.get("ok"):
                raise SlackError(f"{method} -> {data.get('error')}")
            return data
        raise SlackError(f"{method} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    async def post_message(self, channel: str, text: str | None = None,
                           blocks: list | None = None, thread_ts: str | None = None) -> str:
        payload: dict = {"channel": channel}
        if text is not None:
            payload["text"] = text
        if blocks is not None:
            payload["blocks"] = blocks
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        return (await self._call("chat.postMessage", payload))["ts"]

    async def update_message(self, channel: str, ts: str, text: str | None = None,
                             blocks: list | None = None) -> None:
        payload: dict = {"channel": channel, "ts": ts}
        if text is not None:
            payload["text"] = text
        if blocks is not None:
            payload["blocks"] = blocks
        await self._call("chat.update", payload)
