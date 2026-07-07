import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from app.config import Settings
from app.services.audience import sync_audience
from app.services.dispatch import send_seed
from app.services.jobs import complete_job, fail_job, fetch_job
from app.services.reports import campaign_report
from app.services.resend_client import ResendClient

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_LOOPS = 8
HISTORY_LIMIT = 40
MAX_JOBS_PER_PASS = 10

SYSTEM_PROMPT = """You are Growthable's email campaign assistant, working inside Slack.
You help build and send email campaigns through the GHL->Resend pipeline.

Workflow you must follow, in order:
1. Understand what the user wants to send and to whom. Audiences are GHL tag filters —
   use list_ghl_tags to see what exists; confirm the tag with the user.
2. Draft the campaign: a subject line and a COMPLETE HTML email document, authored
   exactly per the EMAIL BRAND GUIDE appended below. Use template "custom" with
   content {"html_body": "<!DOCTYPE html>...full document..."} — pick the closest of
   the guide's four production templates and edit content only; structure, spacing
   and colors stay. Additional hard rules on top of the guide:
   - Personalization: {{first_name}} (fallback "there") is substituted per recipient.
   - {{unsubscribe_url}} and {{preferences_url}} are substituted automatically per
     recipient and MUST appear in the footer, along with the postal address line
     'Growthable LLC · 27 Red Ash Drive, Woonona NSW 2517, Australia'. Campaigns
     missing either are rejected by the tools.
   - Only reference images that actually exist: the three brand asset URLs in the
     guide, YouTube thumbnails (https://img.youtube.com/vi/VIDEO_ID/maxresdefault.jpg
     linking to the video — email cannot embed players), or URLs the user gives you.
     NEVER invent an image URL like the guide's example screenshot — ask the user or
     design without it.
   - Show the user your copy (subject, preheader, key lines) in chat before creating
     the campaign; iterate until they're happy.
   (template "newsletter" with structured props {"headline", "sections", "cta"} still
   exists for bare-bones text updates, but "custom" per the guide is the default.)
   Write tight, useful copy — no hype. Show the user your draft copy in chat before
   creating the campaign, and iterate until they're happy. After a seed test, offer to
   iterate on the design with update_campaign (which requires a fresh seed test).
3. create_campaign, then sync_audience and report the audience size.
4. send_seed_test and tell the user to check their inbox.
5. Only after the user confirms the seed email looks good: propose_send. Ask when it
   should go out (immediately or a scheduled time). propose_send posts approval buttons —
   a human must click Send; you can never dispatch directly.

Rules:
- Never skip the seed test. Never call propose_send before send_seed_test succeeded.
- Daily send cap and deliverability kill rules are enforced by the pipeline; if asked,
  the current cap is in your context below.
- Keep Slack replies short and skimmable. Use plain language.
- If a tool returns an error, explain it briefly and suggest the fix."""


BRAND_GUIDE = (Path(__file__).parent / "brand_guide.md").read_text()

REQUIRED_ADDRESS_MARKER = "Woonona"


def validate_custom_html(content: dict) -> str | None:
    """Compliance gate for bot-authored full-document emails (spec §12)."""
    html = (content or {}).get("html_body") or ""
    if not html.strip():
        return "custom template requires content.html_body (a complete HTML document)"
    if "{{unsubscribe_url}}" not in html:
        return "html_body must include {{unsubscribe_url}} in the footer"
    if REQUIRED_ADDRESS_MARKER not in html:
        return ("footer must include the postal address line: "
                "Growthable LLC · 27 Red Ash Drive, Woonona NSW 2517, Australia")
    return None


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required}}


CONTENT_SCHEMA = {
    "type": "object",
    "description": "For template 'newsletter': headline + sections (+ optional cta). "
                   "For template 'custom': html_body (+ optional preheader).",
    "properties": {
        "preheader": {"type": "string"},
        "headline": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "heading": {"type": "string"},
            "paragraphs": {"type": "array", "items": {"type": "string"}}},
            "required": ["paragraphs"]}},
        "cta": {"type": "object", "properties": {
            "label": {"type": "string"}, "url": {"type": "string"}},
            "required": ["label", "url"]},
        "html_body": {"type": "string",
                      "description": "custom template only: email-safe inline-styled HTML"},
    },
}

TEMPLATE_SCHEMA = {"type": "string", "enum": ["newsletter", "custom"],
                   "description": "newsletter = structured blocks; custom = bespoke html_body"}

TOOLS = [
    _tool("list_ghl_tags", "List available GHL contact tags for audience targeting.", {}, []),
    _tool("create_campaign", "Create a campaign.",
          {"name": {"type": "string"}, "subject": {"type": "string"},
           "tag": {"type": "string", "description": "GHL tag for the audience filter"},
           "template": TEMPLATE_SCHEMA,
           "content": CONTENT_SCHEMA},
          ["name", "subject", "tag", "template", "content"]),
    _tool("update_campaign", "Update a draft campaign's subject, template and/or content.",
          {"campaign_id": {"type": "string"}, "subject": {"type": "string"},
           "template": TEMPLATE_SCHEMA,
           "content": CONTENT_SCHEMA},
          ["campaign_id"]),
    _tool("sync_audience", "Pull the campaign's audience from GHL (applies drop rules).",
          {"campaign_id": {"type": "string"}}, ["campaign_id"]),
    _tool("send_seed_test", "Render and send the campaign to the seed list.",
          {"campaign_id": {"type": "string"}}, ["campaign_id"]),
    _tool("get_report", "Send/delivery/open/click/bounce rollup for a campaign.",
          {"campaign_id": {"type": "string"}}, ["campaign_id"]),
    _tool("propose_send",
          "Post the approval buttons for dispatch. Requires a prior successful seed test. "
          "when_iso: ISO-8601 datetime with timezone offset, omit to send immediately.",
          {"campaign_id": {"type": "string"}, "when_iso": {"type": "string"}},
          ["campaign_id"]),
]


def approval_blocks(campaign_id: str, name: str, subject: str, audience: int,
                    when_iso: str | None) -> list:
    value = json.dumps({"campaign_id": campaign_id, "when": when_iso})
    summary = (f"*Ready to send:* {name}\n*Subject:* {subject}\n"
               f"*Audience:* {audience} contacts\n*When:* {when_iso or 'immediately'}")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": "approve_send",
             "text": {"type": "plain_text", "text": "Send"}, "value": value},
            {"type": "button", "style": "danger", "action_id": "cancel_send",
             "text": {"type": "plain_text", "text": "Cancel"}, "value": value},
        ]},
    ]


class BotEngine:
    def __init__(self, pool, settings: Settings, ghl, slack, resend, client=None):
        self._pool = pool
        self._settings = settings
        self._ghl = ghl
        self._slack = slack
        self._resend = resend or ResendClient(settings.resend_api_key, rps=settings.send_rps)
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    def _system(self) -> str:
        now = datetime.now(ZoneInfo(self._settings.bot_timezone))
        return (f"{SYSTEM_PROMPT}\n\nCurrent time: {now.isoformat()} "
                f"({self._settings.bot_timezone}). Daily send cap: "
                f"{self._settings.daily_send_cap}.\n\n"
                f"=== EMAIL BRAND GUIDE ===\n\n{BRAND_GUIDE}")

    async def handle_turn(self, data: dict) -> None:
        channel, thread_ts = data["channel"], data["thread_ts"]
        row = await self._pool.fetchrow(
            "select messages from bot_sessions where thread_ts=$1", thread_ts)
        messages = json.loads(row["messages"]) if row else []
        messages.append({"role": "user", "content": f"<@{data['user']}>: {data['text']}"})
        self._turn_context = {"channel": channel, "thread_ts": thread_ts}

        # session row must exist before tools (create_campaign links to it)
        await self._pool.execute(
            "insert into bot_sessions (thread_ts, channel) values ($1, $2) "
            "on conflict (thread_ts) do nothing", thread_ts, channel)

        try:
            for _ in range(MAX_LOOPS):
                response = await self._client.messages.create(
                    model=MODEL, max_tokens=16000,
                    thinking={"type": "adaptive"},
                    system=self._system(), tools=TOOLS, messages=messages)
                messages.append({"role": "assistant",
                                 "content": [b.model_dump(mode="json")
                                             for b in response.content]})
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    reply = "".join(b.text for b in response.content if b.type == "text")
                    await self._slack.post_message(channel, text=reply or "(no reply)",
                                                   thread_ts=thread_ts)
                    break
                results = []
                for tu in tool_uses:
                    try:
                        out = await self._run_tool(tu.name, tu.input)
                        is_error = isinstance(out, dict) and "error" in out
                    except Exception as exc:
                        log.exception("bot tool %s failed", tu.name)
                        out, is_error = {"error": str(exc)[:500]}, True
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": json.dumps(out), "is_error": is_error})
                messages.append({"role": "user", "content": results})
        except Exception as exc:
            log.exception("bot turn failed for thread %s", thread_ts)
            await self._slack.post_message(
                channel, text=f"⚠️ Something went wrong on my end: {str(exc)[:300]}. "
                              "Tag me again to retry.", thread_ts=thread_ts)

        await self._pool.execute(
            """insert into bot_sessions (thread_ts, channel, messages, updated_at)
               values ($1, $2, $3, now())
               on conflict (thread_ts) do update set
                   messages=excluded.messages, updated_at=now()""",
            thread_ts, channel, json.dumps(messages[-HISTORY_LIMIT:]))

    async def _run_tool(self, name: str, args: dict):
        pool = self._pool
        if name == "list_ghl_tags":
            return {"tags": await self._ghl.list_tags()}
        if name == "create_campaign":
            audience_filter = [{"field": "tags", "operator": "eq", "value": args["tag"]}]
            template = args.get("template", "custom")
            if template not in ("newsletter", "custom"):
                return {"error": f"unknown template {template!r}"}
            if template == "custom":
                error = validate_custom_html(args.get("content"))
                if error:
                    return {"error": error}
            campaign_id = await pool.fetchval(
                "insert into campaigns (name, subject, template_ref, template_version, "
                "audience_filter, content) values ($1, $2, $3, 'v1', $4, $5) "
                "returning id",
                args["name"], args["subject"], template, json.dumps(audience_filter),
                json.dumps(args["content"]))
            await pool.execute(
                "update bot_sessions set campaign_id=$1 where thread_ts=$2",
                campaign_id, self._turn_context["thread_ts"])
            return {"campaign_id": str(campaign_id), "status": "draft"}
        if name == "update_campaign":
            if "content" in args and "html_body" in args["content"]:
                error = validate_custom_html(args["content"])
                if error:
                    return {"error": error}
            if "subject" in args:
                await pool.execute("update campaigns set subject=$2 where id=$1::uuid",
                                   args["campaign_id"], args["subject"])
            if "template" in args:
                if args["template"] not in ("newsletter", "custom"):
                    return {"error": f"unknown template {args['template']!r}"}
                await pool.execute(
                    "update campaigns set template_ref=$2, seed_tested_at=null where id=$1::uuid",
                    args["campaign_id"], args["template"])
            if "content" in args:
                await pool.execute(
                    "update campaigns set content=$2, seed_tested_at=null where id=$1::uuid",
                    args["campaign_id"], json.dumps(args["content"]))
            return {"updated": True, "note": "content change resets the seed-test requirement"}
        if name == "sync_audience":
            return await sync_audience(pool, self._ghl, args["campaign_id"])
        if name == "send_seed_test":
            campaign = await pool.fetchrow(
                "select * from campaigns where id=$1::uuid", args["campaign_id"])
            if campaign is None:
                return {"error": "campaign not found"}
            sent_to = await send_seed(pool, self._settings, self._resend, campaign)
            return {"sent_to": sent_to}
        if name == "get_report":
            import uuid as _uuid
            return await campaign_report(pool, _uuid.UUID(args["campaign_id"]))
        if name == "propose_send":
            campaign = await pool.fetchrow(
                "select * from campaigns where id=$1::uuid", args["campaign_id"])
            if campaign is None:
                return {"error": "campaign not found"}
            if campaign["seed_tested_at"] is None:
                return {"error": "seed test required before sending — call send_seed_test "
                                 "and have the user check the email first"}
            audience = await pool.fetchval(
                "select count(*) from campaign_contacts where campaign_id=$1", campaign["id"])
            blocks = approval_blocks(str(campaign["id"]), campaign["name"],
                                     campaign["subject"], audience, args.get("when_iso"))
            await self._slack.post_message(
                self._turn_context["channel"], text="Campaign ready for approval",
                blocks=blocks, thread_ts=self._turn_context["thread_ts"])
            return {"posted": True, "note": "approval buttons posted; a human must click Send"}
        raise ValueError(f"unknown tool: {name}")


async def process_bot_turns(pool, engine: BotEngine, max_jobs: int = MAX_JOBS_PER_PASS) -> int:
    done = 0
    for _ in range(max_jobs):
        job = await fetch_job(pool, "bot_turn")
        if job is None:
            break
        try:
            await engine.handle_turn(job["data"])
        except Exception:
            log.exception("bot_turn job %s failed", job["id"])
            await fail_job(pool, job["id"])
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
