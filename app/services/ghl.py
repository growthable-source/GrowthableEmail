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

    async def _request(self, method: str, path: str, json_body: dict | None = None,
                       params: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.wait()
            try:
                async with httpx.AsyncClient(base_url=BASE_URL, headers=self._headers,
                                             timeout=30) as client:
                    resp = await client.request(method, path, json=json_body, params=params)
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

    async def search_conversations(self, last_message_after_ms: int,
                                   page_limit: int = 50) -> AsyncIterator[dict]:
        """Conversations newest-first, stopping once older than the cutoff (epoch ms)."""
        start_after = None
        while True:
            params: dict = {"locationId": self.location_id, "limit": page_limit,
                            "sortBy": "last_message_date", "sort": "desc"}
            if start_after is not None:
                params["startAfterDate"] = start_after
            data = await self._request("GET", "/conversations/search", params=params)
            conversations = data.get("conversations") or []
            if not conversations:
                return
            for raw in conversations:
                last_message = raw.get("lastMessageDate") or 0
                if last_message < last_message_after_ms:
                    return
                yield {
                    "contact_id": raw.get("contactId"),
                    "last_message_date": last_message,
                    "last_message_direction": raw.get("lastMessageDirection"),
                    "last_message_type": raw.get("lastMessageType"),
                }
            start_after = conversations[-1].get("lastMessageDate")
            if len(conversations) < page_limit or not start_after:
                return

    async def list_social_accounts(self) -> list[dict]:
        data = await self._request(
            "GET", f"/social-media-posting/{self.location_id}/accounts")
        results = data.get("results") or data
        accounts = results.get("accounts") or []
        return [{"id": a.get("id") or a.get("_id"),
                 "platform": a.get("platform") or a.get("type"),
                 "name": a.get("name")} for a in accounts]

    async def create_social_post(self, account_ids: list[str], summary: str,
                                 media_urls: list[str] | None = None,
                                 schedule_at_iso: str | None = None) -> str | None:
        body: dict = {"accountIds": list(account_ids), "summary": summary,
                      "type": "post"}
        if media_urls:
            body["media"] = [{"url": url} for url in media_urls]
        if schedule_at_iso:
            body["status"] = "scheduled"
            body["scheduleDate"] = schedule_at_iso
        else:
            body["status"] = "published"
        data = await self._request(
            "POST", f"/social-media-posting/{self.location_id}/posts", body)
        post = (data.get("results") or {}).get("post") or data.get("post") or {}
        return post.get("_id") or post.get("id")

    async def list_tags(self) -> list[str]:
        data = await self._request("GET", f"/locations/{self.location_id}/tags")
        return [t["name"] for t in data.get("tags") or []]

    async def add_tags(self, contact_id: str, tags: list[str]) -> None:
        await self._request("POST", f"/contacts/{contact_id}/tags", {"tags": tags})

    async def set_dnd_email(self, contact_id: str) -> None:
        await self._request("PUT", f"/contacts/{contact_id}", {
            "dndSettings": {"Email": {"status": "active",
                                      "message": "Suppressed by email pipeline"}},
        })
