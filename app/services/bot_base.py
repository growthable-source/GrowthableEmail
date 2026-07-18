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


def cache_marked(messages: list) -> list:
    """Copy of `messages` with a cache breakpoint on the final content block, so each
    request extends the previous one's cached prefix (prompt caching is prefix-match;
    the marker must sit on the newest content). The originals stay unmarked — markers
    must not be persisted or they'd burn breakpoints on stale positions."""
    last = messages[-1]
    content = last["content"]
    blocks = ([{"type": "text", "text": content}] if isinstance(content, str)
              else [dict(b) for b in content])
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return messages[:-1] + [{**last, "content": blocks}]


def sanitize_history(messages: list) -> list:
    """Make a (possibly truncated) history valid for the API: it must open with a
    plain user text message. A blind tail slice can start mid assistant-turn or
    with an orphaned tool_result, which the API rejects with a 400 — and once
    persisted, that poisons every later turn in the thread."""
    for i, m in enumerate(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return messages[i:]
    return []


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
        # Must stay byte-identical across requests: it heads the cached prefix, and any
        # variation (a timestamp, a per-request id) invalidates the prompt cache. The
        # current time travels in each user message instead — see handle_turn.
        return (f"{self.system_prompt}\n\nYour timezone is {self._settings.bot_timezone}. "
                f"Each user message is prefixed with the current time in brackets.")

    async def _run_tool(self, name: str, args: dict):
        raise NotImplementedError

    async def handle_turn(self, data: dict) -> None:
        channel, thread_ts = data["channel"], data["thread_ts"]
        row = await self._pool.fetchrow(
            "select messages from bot_sessions where thread_ts=$1", thread_ts)
        # sanitize on load too: repairs sessions persisted by older builds
        messages = sanitize_history(json.loads(row["messages"])) if row else []
        now = datetime.now(ZoneInfo(self._settings.bot_timezone))
        messages.append({"role": "user",
                         "content": f"[{now.isoformat(timespec='seconds')}] "
                                    f"<@{data['user']}>: {data['text']}"})
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
                    # tools render before system, so this one breakpoint caches both
                    system=[{"type": "text", "text": self._system(),
                             "cache_control": {"type": "ephemeral"}}],
                    tools=self.tools, messages=cache_marked(messages))
                usage = getattr(response, "usage", None)
                if usage is not None:
                    log.debug("claude usage: input=%s cache_read=%s cache_write=%s",
                              usage.input_tokens, usage.cache_read_input_tokens,
                              usage.cache_creation_input_tokens)
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
            thread_ts, channel, json.dumps(sanitize_history(messages[-HISTORY_LIMIT:])))


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
