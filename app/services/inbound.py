"""Inbound replies (Resend receiving).

The email.received webhook carries metadata only; the classify job fetches
the body from GET /emails/receiving/{id}, has Claude read the reply, and
acts: unsubscribe-intent → suppression + DND, interested → alert webhook
(the human takes over). Everything lands in the replies table, which the
outbound engine polls via /outbound/activity to halt sequences.
"""
import json
import logging

import httpx

from app.services.jobs import complete_job, enqueue, fail_job, fetch_job
from app.services.suppressions import add_suppression

log = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """You triage a reply to a B2B cold email (we sell an AI \
receptionist to local businesses). Classify the sender's intent.

Reply subject: {subject}
Reply body (may include quoted original):
{body}

Return ONLY minified JSON:
{{"classification":"interested|not_interested|unsubscribe|ooo|auto_reply|other","summary":str}}

interested: wants to talk, asks a question, positive signal.
not_interested: polite/blunt no, but not asking for removal.
unsubscribe: asks to be removed / stop emailing / legal threat.
ooo: out-of-office autoresponder.
auto_reply: other automated response (ticket systems etc).
summary: one factual sentence of what they said."""


async def handle_received(pool, data: dict) -> None:
    """Fast path inside the webhook: store + enqueue. No network calls."""
    email_id = data.get("email_id")
    if not email_id:
        return
    from_email = (data.get("from") or "").strip().lower()
    to_email = ", ".join(data.get("to") or [])
    inserted = await pool.fetchrow(
        """insert into replies (resend_email_id, from_email, to_email, subject)
           values ($1, $2, $3, $4)
           on conflict (resend_email_id) do nothing returning id""",
        email_id, from_email, to_email, (data.get("subject") or "")[:500])
    if inserted:
        # link to the most recent send to this address, if any
        await pool.execute(
            """update replies r set
                 send_id = s.id, campaign_id = s.campaign_id
               from (select id, campaign_id from sends
                     where email = $2 order by created_at desc limit 1) s
               where r.id = $1""",
            inserted["id"], from_email)
        await enqueue(pool, "classify_reply", {"reply_id": str(inserted["id"])})


async def _fetch_body(settings, resend_email_id: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.resend.com/emails/receiving/{resend_email_id}",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"})
    resp.raise_for_status()
    data = resp.json()
    return (data.get("text") or data.get("html") or "")[:6000]


async def _classify(settings, subject: str, body: str) -> dict:
    if not settings.anthropic_api_key:
        return {"classification": "other", "summary": "(no classifier configured)"}
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=300,
        messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(
            subject=subject or "(none)", body=body or "(empty)")}])
    raw = "".join(b.text for b in msg.content if b.type == "text")
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        start, end = raw.find("{"), raw.rfind("}")
        return json.loads(raw[start:end + 1], strict=False)
    except (ValueError, json.JSONDecodeError):
        return {"classification": "other", "summary": raw[:200]}


async def process_reply_jobs(pool, settings) -> int:
    handled = 0
    while True:
        job = await fetch_job(pool, "classify_reply")
        if job is None:
            return handled
        try:
            reply = await pool.fetchrow(
                "select * from replies where id = $1::uuid", job["data"]["reply_id"])
            if reply is None:
                await complete_job(pool, job["id"])
                continue
            body = await _fetch_body(settings, reply["resend_email_id"])
            verdict = await _classify(settings, reply["subject"], body)
            cls = verdict.get("classification") or "other"
            await pool.execute(
                """update replies set body_text=$2, classification=$3, summary=$4,
                       processed=true where id=$1""",
                reply["id"], body[:4000], cls, (verdict.get("summary") or "")[:500])
            if cls == "unsubscribe":
                await add_suppression(pool, reply["from_email"],
                                      reason="unsubscribe", source="reply")
            if cls == "interested" and settings.alert_webhook_url:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(settings.alert_webhook_url, json={"text":
                        f"🔥 Interested reply from {reply['from_email']}: "
                        f"{verdict.get('summary', '')}\n> {body[:400]}"})
            handled += 1
            await complete_job(pool, job["id"])
        except Exception:                                     # noqa: BLE001
            log.exception("classify_reply failed")
            await fail_job(pool, job["id"])
