import base64

import httpx
import pytest
import respx

from app.services.images import API_URL, ImageGenError, generate_image
from tests.helpers import make_settings

PNG_BYTES = b"\x89PNG-fake-image-bytes"


def gemini_response():
    return {"candidates": [{"content": {"parts": [
        {"text": "here you go"},
        {"inlineData": {"mimeType": "image/png",
                        "data": base64.b64encode(PNG_BYTES).decode()}},
    ]}}]}


@respx.mock
async def test_generate_image_stores_and_returns_url(pool):
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=gemini_response()))
    url = await generate_image(pool, make_settings(), "a navy and pink rocket")
    image_id = url.rsplit("/", 1)[-1]
    assert url == f"http://testserver/images/{image_id}"
    row = await pool.fetchrow("select mime, bytes from images")
    assert row["mime"] == "image/png" and bytes(row["bytes"]) == PNG_BYTES


@respx.mock
async def test_generate_image_error_paths(pool):
    respx.post(API_URL).mock(return_value=httpx.Response(429, text="quota"))
    with pytest.raises(ImageGenError, match="429"):
        await generate_image(pool, make_settings(), "x")
    with pytest.raises(ImageGenError, match="not configured"):
        await generate_image(pool, make_settings(gemini_api_key=""), "x")


@respx.mock
async def test_image_served_by_endpoint(client, pool):
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=gemini_response()))
    url = await generate_image(pool, make_settings(), "x")
    resp = await client.get("/" + url.split("/", 3)[-1])
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES
    assert resp.headers["content-type"] == "image/png"
    assert (await client.get("/images/not-a-uuid")).status_code == 404
