"""Thin Emailable API wrapper. Provider-neutral surface: create_batch/get_batch/
map_result are all the pipeline knows, so swapping providers touches only this file."""
import httpx

BASE_URL = "https://api.emailable.com/v1"


class InsufficientCreditsError(Exception):
    """Emailable 402 — the account is out of verification credits. Callers alert
    and arm the credit watch instead of burning job retries."""


def map_result(raw: dict) -> tuple[str, str | None]:
    """Emailable result -> (verdict, reason). Role/disposable flags override state
    (spec: role accounts are risky, disposable domains are invalid)."""
    if raw.get("disposable"):
        return "invalid", "disposable"
    if raw.get("role"):
        return "risky", "role"
    state = raw.get("state")
    if state == "deliverable":
        return "valid", raw.get("reason")
    if state == "undeliverable":
        return "invalid", raw.get("reason")
    if state == "risky":
        return "risky", raw.get("reason")
    return "unknown", raw.get("reason")


class EmailableClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30)

    async def create_batch(self, emails: list[str]) -> str:
        resp = await self._client.post(f"{BASE_URL}/batch", json={
            "emails": ",".join(emails), "api_key": self._api_key})
        if resp.status_code == 402:
            raise InsufficientCreditsError("emailable: insufficient credits")
        resp.raise_for_status()
        return resp.json()["id"]

    async def get_credits(self) -> int | None:
        """Available credits on the account, or None if the endpoint errored —
        the credit watch treats None as 'still unknown, poll again'."""
        try:
            resp = await self._client.get(f"{BASE_URL}/account",
                                          params={"api_key": self._api_key})
            resp.raise_for_status()
            return resp.json().get("available_credits")
        except httpx.HTTPError:
            return None

    async def get_batch(self, batch_id: str) -> list[dict] | None:
        """None while the batch is still processing, else the per-email results."""
        resp = await self._client.get(f"{BASE_URL}/batch",
                                      params={"id": batch_id, "api_key": self._api_key})
        resp.raise_for_status()
        body = resp.json()
        return body.get("emails")  # absent until complete
