"""Social media persona: drafts posts, generates images, publishes via GHL Social
Planner after button-click approval."""
import json
import logging

from app.services.bot import BRAND_GUIDE
from app.services.bot_base import BaseBot
from app.services.images import generate_image

log = logging.getLogger(__name__)

# §1 (who we are) through §3 (voice) of the email brand guide apply to social too.
BRAND_VOICE = BRAND_GUIDE.split("## 4.")[0]

SOCIAL_SYSTEM_PROMPT = """You are Growthable's social media assistant, working inside
Slack. You draft and schedule posts through GoHighLevel's Social Planner, which
publishes to whichever social accounts are connected there.

Workflow, in order:
1. list_social_accounts and confirm with the user which accounts this post targets.
2. Draft the copy. Follow the brand voice section below. Platform norms:
   - LinkedIn: up to ~1300 chars shown before "see more" — put the hook in line one;
     no hashtag walls (0-3 tasteful ones); line breaks for scannability.
   - Facebook/Instagram: shorter, hook first; Instagram REQUIRES an image.
   - X/Twitter: 280 chars hard limit.
   - Google Business Profile: plain, informative, one CTA link.
   One idea per post. Numbers beat adjectives. One wink max.
3. Images: use generate_image for AI-generated visuals (describe style: brand navy
   #34475B and pink #F03E6A, clean, modern, no text overlays unless asked — text in
   AI images often renders badly). Or use the brand assets / URLs the user gives you.
   Show the image URL in the thread so the user can eyeball it.
4. draft_post to save the draft (it also posts a preview to the thread).
5. Only after the user approves the preview: propose_publish. Ask when it should go
   out. It posts approval buttons — a human must click Publish; you never publish
   directly. If the user wants changes after drafting, use update_post.

Rules:
- Never invent stats, testimonials, or product claims — only the proof points in the
  brand section below or facts the user gives you.
- Never post the same copy verbatim to every platform if the user asked for multiple
  platforms — adapt lengths and hooks.
- Keep Slack replies short. Show drafts as plain text, quoted.
"""


def _tool(name, description, properties, required):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required}}


SOCIAL_TOOLS = [
    _tool("list_social_accounts",
          "List social accounts connected in GHL Social Planner.", {}, []),
    _tool("generate_image",
          "Generate an image with AI; returns a hosted public URL.",
          {"prompt": {"type": "string", "description": "detailed visual description"}},
          ["prompt"]),
    _tool("draft_post",
          "Save a post draft and show a preview in the thread.",
          {"account_ids": {"type": "array", "items": {"type": "string"}},
           "text": {"type": "string"},
           "media_urls": {"type": "array", "items": {"type": "string"}}},
          ["account_ids", "text"]),
    _tool("update_post",
          "Update a draft post's text, accounts, or media.",
          {"post_id": {"type": "string"}, "text": {"type": "string"},
           "account_ids": {"type": "array", "items": {"type": "string"}},
           "media_urls": {"type": "array", "items": {"type": "string"}}},
          ["post_id"]),
    _tool("propose_publish",
          "Post the Publish/Cancel approval buttons for a draft. when_iso: ISO-8601 "
          "datetime with timezone offset; omit to publish immediately on approval.",
          {"post_id": {"type": "string"}, "when_iso": {"type": "string"}},
          ["post_id"]),
]


def publish_blocks(post_id: str, text: str, accounts: int, when_iso: str | None) -> list:
    value = json.dumps({"post_id": post_id, "when": when_iso})
    preview = text if len(text) <= 400 else text[:400] + "…"
    summary = (f"*Ready to publish to {accounts} account(s)*\n"
               f"*When:* {when_iso or 'immediately'}\n>{preview}")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": "approve_post",
             "text": {"type": "plain_text", "text": "Publish"}, "value": value},
            {"type": "button", "style": "danger", "action_id": "cancel_post",
             "text": {"type": "plain_text", "text": "Cancel"}, "value": value},
        ]},
    ]


class SocialBot(BaseBot):
    system_prompt = SOCIAL_SYSTEM_PROMPT
    tools = SOCIAL_TOOLS

    def _system(self) -> str:
        return f"{super()._system()}\n\n=== BRAND (who we are, tokens, voice) ===\n\n{BRAND_VOICE}"

    async def _run_tool(self, name: str, args: dict):
        pool = self._pool
        if name == "list_social_accounts":
            return {"accounts": await self._ghl.list_social_accounts()}
        if name == "generate_image":
            url = await generate_image(pool, self._settings, args["prompt"])
            return {"image_url": url}
        if name == "draft_post":
            if not args["account_ids"]:
                return {"error": "account_ids must not be empty"}
            content = {"text": args["text"], "media": args.get("media_urls") or []}
            post_id = await pool.fetchval(
                "insert into social_posts (thread_ts, account_ids, content) "
                "values ($1, $2, $3) returning id",
                self._turn_context["thread_ts"], args["account_ids"],
                json.dumps(content))
            media_note = "".join(f"\n📎 {u}" for u in content["media"])
            await self._slack.post_message(
                self._turn_context["channel"],
                text=f"Draft saved:\n>{args['text']}{media_note}",
                thread_ts=self._turn_context["thread_ts"])
            return {"post_id": str(post_id), "status": "draft"}
        if name == "update_post":
            row = await pool.fetchrow(
                "select * from social_posts where id=$1::uuid", args["post_id"])
            if row is None:
                return {"error": "post not found"}
            if row["status"] != "draft":
                return {"error": f"post is {row['status']}; only drafts can be edited"}
            content = json.loads(row["content"])
            if "text" in args:
                content["text"] = args["text"]
            if "media_urls" in args:
                content["media"] = args["media_urls"]
            account_ids = args.get("account_ids") or row["account_ids"]
            await pool.execute(
                "update social_posts set content=$2, account_ids=$3 where id=$1",
                row["id"], json.dumps(content), account_ids)
            return {"updated": True}
        if name == "propose_publish":
            row = await pool.fetchrow(
                "select * from social_posts where id=$1::uuid", args["post_id"])
            if row is None:
                return {"error": "post not found"}
            if row["status"] != "draft":
                return {"error": f"post already {row['status']}"}
            content = json.loads(row["content"])
            await self._slack.post_message(
                self._turn_context["channel"], text="Post ready for approval",
                blocks=publish_blocks(str(row["id"]), content["text"],
                                      len(row["account_ids"]), args.get("when_iso")),
                thread_ts=self._turn_context["thread_ts"])
            return {"posted": True, "note": "approval buttons posted; a human must click Publish"}
        raise ValueError(f"unknown tool: {name}")
