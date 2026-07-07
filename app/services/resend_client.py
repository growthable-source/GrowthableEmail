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
