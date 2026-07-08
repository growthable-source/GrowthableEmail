"""Gemini image generation, stored in Postgres and served at /images/{id}."""
import base64

import httpx

from app.config import Settings

GEMINI_MODEL = "gemini-2.5-flash-image"
API_URL = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")


class ImageGenError(Exception):
    pass


def _find_inline_image(payload: dict) -> tuple[bytes, str]:
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return base64.b64decode(inline["data"]), mime
    raise ImageGenError("gemini returned no image data")


async def generate_image(pool, settings: Settings, prompt: str) -> str:
    """Generate an image and return its public URL."""
    if not settings.gemini_api_key:
        raise ImageGenError("GEMINI_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            API_URL,
            headers={"x-goog-api-key": settings.gemini_api_key,
                     "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]})
    if resp.status_code != 200:
        raise ImageGenError(f"gemini -> {resp.status_code}: {resp.text[:300]}")
    data, mime = _find_inline_image(resp.json())
    image_id = await pool.fetchval(
        "insert into images (mime, bytes) values ($1, $2) returning id", mime, data)
    return f"{settings.public_base_url}/images/{image_id}"
