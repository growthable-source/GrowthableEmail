from app.services.jobs import enqueue
from app.services.writeback import process_writeback_jobs


class FakeGHL:
    def __init__(self, fail_times=0):
        self.calls = []
        self._fail_times = fail_times

    async def add_tags(self, contact_id, tags):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("ghl down")
        self.calls.append(("add_tags", contact_id, tuple(tags)))

    async def set_dnd_email(self, contact_id):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("ghl down")
        self.calls.append(("set_dnd", contact_id))


async def test_processes_tag_and_dnd_jobs(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "add_tags", "contact_id": "c1", "tags": ["opened-x"]})
    await enqueue(pool, "ghl_writeback", {"kind": "set_dnd", "contact_id": "c2"})
    ghl = FakeGHL()
    assert await process_writeback_jobs(pool, ghl) == 2
    assert ("add_tags", "c1", ("opened-x",)) in ghl.calls
    assert ("set_dnd", "c2") in ghl.calls
    states = [r["state"] for r in await pool.fetch("select state from jobs")]
    assert states == ["completed", "completed"]


async def test_ghl_failure_retries_job_not_lost(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "set_dnd", "contact_id": "c1"})
    assert await process_writeback_jobs(pool, FakeGHL(fail_times=1)) == 0
    row = await pool.fetchrow("select state, retry_count from jobs")
    assert row["state"] == "created" and row["retry_count"] == 1


async def test_unknown_kind_dead_letters(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "explode"})
    for _ in range(3):
        await process_writeback_jobs(pool, FakeGHL())
        await pool.execute("update jobs set start_after = now()")
    assert (await pool.fetchval("select state from jobs")) == "failed"


async def test_drains_large_queue_in_one_pass(pool):
    for i in range(25):
        await enqueue(pool, "ghl_writeback",
                      {"kind": "add_tags", "contact_id": f"c{i}", "tags": ["email-invalid"]})
    ghl = FakeGHL()
    assert await process_writeback_jobs(pool, ghl) == 25
    assert len(ghl.calls) == 25
    assert await pool.fetchval(
        "select count(*) from jobs where state='completed'") == 25
