from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.services.jobs import enqueue
from app.services.suppressions import add_suppression
from app.services.unsub_tokens import parse_token

router = APIRouter()

CONFIRMATION_HTML = """<!doctype html>
<html><head><title>Unsubscribed</title></head>
<body style="font-family: Helvetica, Arial, sans-serif; max-width: 480px; margin: 80px auto; text-align: center;">
  <h1>You're unsubscribed</h1>
  <p>{email} won't receive any more marketing email from Growthable.</p>
</body></html>"""


async def _unsubscribe(request: Request, token: str) -> HTMLResponse:
    settings = request.app.state.settings
    parsed = parse_token(token, settings.unsub_signing_secret)
    if parsed is None:
        raise HTTPException(404, "invalid link")
    email, _campaign_id = parsed
    pool = request.app.state.pool
    contact_id = await pool.fetchval(
        "select ghl_contact_id from contacts_cache where email=$1", email)
    already = await pool.fetchval(
        "select exists(select 1 from suppressions where email=$1)", email)
    await add_suppression(pool, email, reason="unsubscribe", source="unsub_page",
                          ghl_contact_id=contact_id)
    if not already and contact_id:
        await enqueue(pool, "ghl_writeback", {"kind": "set_dnd", "contact_id": contact_id})
    return HTMLResponse(CONFIRMATION_HTML.format(email=email))


@router.get("/u/{token}")
async def unsubscribe_get(request: Request, token: str):
    return await _unsubscribe(request, token)


@router.post("/u/{token}")  # RFC 8058 one-click POST target
async def unsubscribe_post(request: Request, token: str):
    return await _unsubscribe(request, token)
