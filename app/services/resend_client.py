import asyncio
import json

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
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._limiter = RateLimiter(rps)
        self._backoff_base = backoff_base

    async def _request(self, method: str, path: str, *, json_body: dict | None = None,
                       data: dict | None = None, files: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.wait()
            try:
                async with httpx.AsyncClient(base_url=API_URL, headers=self._headers,
                                             timeout=60) as client:
                    resp = await client.request(method, path, json=json_body,
                                                data=data, files=files)
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
            return resp.json()
        raise TransientSendError(
            f"resend {method} {path} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    async def send_email(self, payload: dict) -> str:
        """POST /emails; returns the Resend email id."""
        return (await self._request("POST", "/emails", json_body=payload))["id"]

    async def create_segment(self, name: str) -> str:
        """POST /segments; returns the segment id."""
        return (await self._request("POST", "/segments", json_body={"name": name}))["id"]

    async def create_contact_import(self, csv_bytes: bytes, column_map: dict,
                                    segment_id: str, on_conflict: str = "upsert") -> str:
        """POST /contacts/imports (bulk CSV, multipart); returns the import id."""
        result = await self._request(
            "POST", "/contacts/imports",
            data={"column_map": json.dumps(column_map),
                  "segments": json.dumps([{"id": segment_id}]),
                  "on_conflict": on_conflict},
            files={"file": ("contacts.csv", csv_bytes, "text/csv")})
        return result["id"]

    async def get_contact_import(self, import_id: str) -> dict:
        """GET /contacts/imports/{id}; returns the import object (status, counts)."""
        return await self._request("GET", f"/contacts/imports/{import_id}")

    async def create_broadcast(self, payload: dict) -> str:
        """POST /broadcasts; returns the broadcast id."""
        return (await self._request("POST", "/broadcasts", json_body=payload))["id"]
