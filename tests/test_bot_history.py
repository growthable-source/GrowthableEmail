from app.services.bot_base import sanitize_history
from app.services.jobs import enqueue, fetch_job, requeue_stale_jobs


def user_text(text):
    return {"role": "user", "content": text}


def assistant_tool_use():
    return {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "…", "signature": "sig"},
        {"type": "tool_use", "id": "tu_1", "name": "sync_audience", "input": {}}]}


def tool_result():
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "{}"}]}


def test_sanitize_keeps_valid_history():
    msgs = [user_text("hi"), assistant_tool_use(), tool_result()]
    assert sanitize_history(msgs) == msgs


def test_sanitize_drops_orphaned_tool_result_prefix():
    # a tail slice that cut between tool_use and tool_result — the prod 400
    msgs = [tool_result(), {"role": "assistant", "content": []}, user_text("next"),
            assistant_tool_use(), tool_result()]
    fixed = sanitize_history(msgs)
    assert fixed[0] == user_text("next") and len(fixed) == 3


def test_sanitize_drops_assistant_first_prefix():
    msgs = [assistant_tool_use(), tool_result(), user_text("ok")]
    assert sanitize_history(msgs) == [user_text("ok")]


def test_sanitize_all_junk_returns_empty():
    assert sanitize_history([assistant_tool_use(), tool_result()]) == []


async def test_requeue_stale_jobs_recovers_abandoned_active(pool):
    await enqueue(pool, "bot_turn", {"x": 1})
    job = await fetch_job(pool, "bot_turn")  # goes active, never completed
    # simulate a worker that died 20 minutes ago
    await pool.execute(
        "update jobs set start_after=now() - interval '20 minutes' where id=$1", job["id"])
    assert await requeue_stale_jobs(pool) == 1
    row = await pool.fetchrow("select state, retry_count from jobs where id=$1", job["id"])
    assert row["state"] == "created" and row["retry_count"] == 1
    # exhausted retries dead-letter instead of looping forever
    await pool.execute(
        "update jobs set state='active', retry_count=2, "
        "start_after=now() - interval '20 minutes' where id=$1", job["id"])
    await requeue_stale_jobs(pool)
    assert (await pool.fetchval("select state from jobs where id=$1", job["id"])) == "failed"


async def test_requeue_stale_jobs_leaves_fresh_active_alone(pool):
    await enqueue(pool, "bot_turn", {"x": 1})
    await fetch_job(pool, "bot_turn")
    assert await requeue_stale_jobs(pool) == 0
