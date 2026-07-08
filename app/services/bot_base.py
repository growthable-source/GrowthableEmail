"""Shared Claude tool-loop plumbing for Slack bot personas."""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from app.config import Settings
from app.services.jobs import complete_job, fail_job, fetch_job

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_LOOPS = 8
HISTORY_LIMIT = 40
MAX_JOBS_PER_PASS = 10


class BaseBot:
    """Subclasses define `system_prompt`, `tools`, and `_run_tool`."""

    system_prompt: str = ""
    tools: list = []

    def __init__(self, pool, settings: Settings, ghl, slack, client=None):
        self._pool = pool
        self._settings = settings
        self._ghl = ghl
        self._slack = slack
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    def _system(self) -> str:
        now = datetime.now(ZoneInfo(self._settings.bot_timezone))
        return (f"{self.system_prompt}\n\nCurrent time: {now.isoformat()} "
                f"({self._settings.bot_timezone}).")

    async def _run_tool(self, name: str, args: dict):
        raise NotImplementedError

    async def handle_turn(self, data: dict) -> None:
        channel, thread_ts = data["channel"], data["thread_ts"]
        row = await self._pool.fetchrow(
            "select messages from bot_sessions where thread_ts=$1", thread_ts)
        messages = json.loads(row["messages"]) if row else []
        messages.append({"role": "user", "content": f"<@{data['user']}>: {data['text']}"})
        self._turn_context = {"channel": channel, "thread_ts": thread_ts}

        # session row must exist before tools (they may link rows to it)
        await self._pool.execute(
            "insert into bot_sessions (thread_ts, channel) values ($1, $2) "
            "on conflict (thread_ts) do nothing", thread_ts, channel)

        try:
            for _ in range(MAX_LOOPS):
                response = await self._client.messages.create(
                    model=MODEL, max_tokens=16000,
                    thinking={"type": "adaptive"},
                    system=self._system(), tools=self.tools, messages=messages)
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


async def process_bot_turns(pool, engines: dict[str, BaseBot],
                            max_jobs: int = MAX_JOBS_PER_PASS) -> int:
    """Drain bot_turn jobs, dispatching each to the engine for its channel."""
    done = 0
    for _ in range(max_jobs):
        job = await fetch_job(pool, "bot_turn")
        if job is None:
            break
        engine = engines.get(job["data"].get("channel"))
        if engine is None:
            log.warning("bot_turn for unconfigured channel %s dropped",
                        job["data"].get("channel"))
            await complete_job(pool, job["id"])
            continue
        try:
            await engine.handle_turn(job["data"])
        except Exception:
            log.exception("bot_turn job %s failed", job["id"])
            await fail_job(pool, job["id"])
            continue
        await complete_job(pool, job["id"])
        done += 1
    return done
