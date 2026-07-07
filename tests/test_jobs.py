from app.services.jobs import complete_job, enqueue, fail_job, fetch_job


async def test_enqueue_fetch_complete(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "add_tags", "contact_id": "c1"})
    job = await fetch_job(pool, "ghl_writeback")
    assert job["data"]["kind"] == "add_tags"
    assert (await pool.fetchval("select state from jobs where id=$1", job["id"])) == "active"
    # another fetch while active gets nothing (SKIP LOCKED semantics + state filter)
    assert await fetch_job(pool, "ghl_writeback") is None
    await complete_job(pool, job["id"])
    assert (await pool.fetchval("select state from jobs where id=$1", job["id"])) == "completed"


async def test_fail_retries_with_backoff_then_dead(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "set_dnd"})
    for expected_retry in (1, 2, 3):
        job = await fetch_job(pool, "ghl_writeback")
        assert job is not None, f"retry {expected_retry} should be fetchable"
        await fail_job(pool, job["id"], backoff_seconds=0)
        row = await pool.fetchrow("select state, retry_count from jobs where id=$1", job["id"])
        if expected_retry < 3:
            assert row["state"] == "created" and row["retry_count"] == expected_retry
        else:
            assert row["state"] == "failed"
    assert await fetch_job(pool, "ghl_writeback") is None


async def test_start_after_delays_fetch(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "x"}, start_after_seconds=3600)
    assert await fetch_job(pool, "ghl_writeback") is None
