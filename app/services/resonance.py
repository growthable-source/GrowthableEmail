"""Resonance (in-house notetaker) client for the weekly review's meeting feed.

ASSUMED API CONTRACT — adjust when Ryan supplies the real docs:
    GET {RESONANCE_API_URL}/meetings?days=N
    Authorization: Bearer {RESONANCE_API_KEY}
    -> [{"title": ..., "date": ..., "summary": ..., "topics": [...]}, ...]
Anything else the API returns is passed through untouched; the bot reads JSON.
"""
import httpx


class ResonanceClient:
    def __init__(self, api_url: str, api_key: str,
                 client: httpx.AsyncClient | None = None):
        self._url = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=30, headers={"Authorization": f"Bearer {api_key}"})

    async def recent_meetings(self, days: int = 7) -> list[dict]:
        resp = await self._client.get(f"{self._url}/meetings",
                                      params={"days": days})
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, list) else body.get("meetings", [])
