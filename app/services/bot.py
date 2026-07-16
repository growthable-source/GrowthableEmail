import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.services.audience import sync_audience
from app.services.bot_base import BaseBot, process_bot_turns  # noqa: F401 (re-export)
from app.services.dispatch import send_seed
from app.services.jobs import enqueue
from app.services.reports import campaign_report
from app.services.resend_client import ResendClient

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Growthable's email campaign assistant, working inside Slack.
You help build and send email campaigns through the GHL->Resend pipeline.

Workflow you must follow, in order:
1. Understand what the user wants to send and to whom. Audiences are GHL tag filters —
   use list_ghl_tags to see what exists; confirm the tag with the user.
   For HIGH-INTENT audiences, offer build_engaged_segment: it finds every contact with
   GHL conversation activity in the last N days and tags them (e.g. 'engaged-90d'),
   which then becomes the campaign's audience tag. Tagging runs in the background —
   check segment_progress before syncing the audience.
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
3. create_campaign, then sync_audience and report the audience size AND the country
   breakdown it returns (plus how many contacts have an explicit timezone).
4. send_seed_test and tell the user to check their inbox.
5. Only after the user confirms the seed email looks good: propose_send. Ask two things:
   - WHEN it should go out (immediately or a scheduled time).
   - HOW: all at once (one Resend broadcast — no volume limit), or RAMPED with per_day
     and/or per_hour limits. Ramped sends are timezone-targeted: each contact gets the
     email at the ideal local hour (default 10am) in their timezone, resolved from the
     GHL contact's timezone field, else their country, else assumed US. Sending
     continues for up to 8 hours past the ideal hour each day, then rolls to the next
     day — so with only a per_hour limit, roughly 8×per_hour can go out per day.
     Recommend a ramp for the first large send on this domain (deliverability), and
     use the country breakdown to tell the user when their audience will receive it.
   propose_send posts approval buttons — a human must click Send; you can never
   dispatch directly.

Rules:
- Never skip the seed test. Never call propose_send before send_seed_test succeeded.
- There is NO hard cap on campaign size: a broadcast sends everything at once, and a
  ramp uses whatever per_day/per_hour the user picks. Never tell the user a campaign
  is impossible because of a cap. The daily send cap in your context only limits the
  per-email drip queue (GHL enrollments); deliverability kill rules still pause
  everything on bounce spikes.
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
    _tool("build_engaged_segment",
          "Tag every contact with GHL conversation activity in the last N days "
          "(high-intent segment). Tagging runs via background jobs; use "
          "segment_progress to check completion before sync_audience.",
          {"days": {"type": "integer", "minimum": 1, "maximum": 365},
           "tag": {"type": "string", "description": "tag to apply, e.g. engaged-90d"}},
          ["days", "tag"]),
    _tool("segment_progress",
          "Counts of pending/completed background tagging jobs.", {}, []),
    _tool("propose_send",
          "Post the approval buttons for dispatch. Requires a prior successful seed test. "
          "when_iso: ISO-8601 datetime with timezone offset, omit to send immediately. "
          "Omit per_day AND per_hour to send everything at once as one Resend broadcast. "
          "Set per_day and/or per_hour to ramp instead: sends then go out at the ideal "
          "local hour in each contact's timezone (GHL timezone, else country, else US).",
          {"campaign_id": {"type": "string"}, "when_iso": {"type": "string"},
           "per_day": {"type": "integer", "minimum": 1,
                       "description": "max emails per day for this campaign"},
           "per_hour": {"type": "integer", "minimum": 1,
                        "description": "max emails per hour for this campaign"}},
          ["campaign_id"]),
]


def approval_blocks(campaign_id: str, name: str, subject: str, audience: int,
                    when_iso: str | None, per_day: int | None = None,
                    per_hour: int | None = None) -> list:
    value = json.dumps({"campaign_id": campaign_id, "when": when_iso,
                        "per_day": per_day, "per_hour": per_hour})
    if per_day or per_hour:
        limits = " · ".join(filter(None, [f"{per_day}/day" if per_day else None,
                                          f"{per_hour}/hour" if per_hour else None]))
        how = f"ramped ({limits}), at the ideal local time per contact"
    else:
        how = "one broadcast, all at once"
    summary = (f"*Ready to send:* {name}\n*Subject:* {subject}\n"
               f"*Audience:* {audience} contacts\n*How:* {how}\n"
               f"*When:* {when_iso or 'immediately'}")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": "approve_send",
             "text": {"type": "plain_text", "text": "Send"}, "value": value},
            {"type": "button", "style": "danger", "action_id": "cancel_send",
             "text": {"type": "plain_text", "text": "Cancel"}, "value": value},
        ]},
    ]


class BotEngine(BaseBot):
    """Email campaign persona."""

    system_prompt = SYSTEM_PROMPT
    tools = TOOLS

    def __init__(self, pool, settings: Settings, ghl, slack, resend, client=None):
        super().__init__(pool, settings, ghl, slack, client=client)
        self._resend = resend or ResendClient(settings.resend_api_key, rps=settings.send_rps)

    def _system(self) -> str:
        return (f"{super()._system()} Daily send cap (drip queue only — campaign "
                f"broadcasts are uncapped): {self._settings.daily_send_cap}."
                f"\n\n=== EMAIL BRAND GUIDE ===\n\n{BRAND_GUIDE}")

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
                "audience_filter, content, thread_ts, channel) "
                "values ($1, $2, $3, 'v1', $4, $5, $6, $7) returning id",
                args["name"], args["subject"], template, json.dumps(audience_filter),
                json.dumps(args["content"]), self._turn_context["thread_ts"],
                self._turn_context["channel"])
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
        if name == "build_engaged_segment":
            days, tag = int(args["days"]), args["tag"].strip()
            if not 1 <= days <= 365:
                return {"error": "days must be between 1 and 365"}
            if not tag:
                return {"error": "tag must not be empty"}
            cutoff_ms = int((datetime.now(timezone.utc).timestamp() - days * 86400) * 1000)
            seen: set[str] = set()
            capped = False
            async for convo in self._ghl.search_conversations(cutoff_ms):
                contact_id = convo.get("contact_id")
                if contact_id:
                    seen.add(contact_id)
                if len(seen) >= 10_000:
                    capped = True
                    break
            for contact_id in seen:
                await enqueue(pool, "ghl_writeback",
                              {"kind": "add_tags", "contact_id": contact_id, "tags": [tag]})
            minutes = max(1, len(seen) // 480)
            return {"contacts_found": len(seen), "tag": tag, "capped_at_10k": capped,
                    "note": f"tagging in background (~{minutes} min); check "
                            "segment_progress, then use this tag as the audience"}
        if name == "segment_progress":
            rows = await pool.fetch(
                "select state, count(*) as n from jobs where name='ghl_writeback' "
                "group by state")
            return {r["state"]: r["n"] for r in rows} or {"idle": True}
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
                                     campaign["subject"], audience, args.get("when_iso"),
                                     args.get("per_day"), args.get("per_hour"))
            await self._slack.post_message(
                self._turn_context["channel"], text="Campaign ready for approval",
                blocks=blocks, thread_ts=self._turn_context["thread_ts"])
            return {"posted": True, "note": "approval buttons posted; a human must click Send"}
        raise ValueError(f"unknown tool: {name}")
