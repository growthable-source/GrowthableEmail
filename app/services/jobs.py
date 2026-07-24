import json


async def enqueue(pool, name: str, data: dict, start_after_seconds: int = 0) -> None:
    await pool.execute(
        "insert into jobs (name, data, start_after) "
        "values ($1, $2, now() + make_interval(secs => $3))",
        name, json.dumps(data), start_after_seconds,
    )


async def fetch_job(pool, name: str) -> dict | None:
    # start_after doubles as the claim timestamp so stale actives can be spotted
    row = await pool.fetchrow(
        """update jobs set state='active', start_after=now()
           where id = (select id from jobs
                       where name = $1 and state = 'created' and start_after <= now()
                       order by created_at
                       limit 1
                       for update skip locked)
           returning id, name, data, retry_count, retry_limit""",
        name,
    )
    if row is None:
        return None
    job = dict(row)
    job["data"] = json.loads(job["data"])
    return job


async def complete_job(pool, job_id) -> None:
    await pool.execute(
        "update jobs set state='completed', completed_at=now() where id=$1", job_id
    )


async def requeue_stale_jobs(pool, stale_minutes: int = 15) -> int:
    """Return jobs abandoned mid-flight (worker crash/redeploy) to the queue,
    dead-lettering ones that keep dying."""
    result = await pool.execute(
        """update jobs set
               retry_count = retry_count + 1,
               state = case when retry_count + 1 >= retry_limit then 'failed' else 'created' end,
               completed_at = case when retry_count + 1 >= retry_limit then now() else null end
           where state = 'active'
             and start_after < now() - make_interval(mins => $1)""",
        stale_minutes,
    )
    return int(result.split()[-1])


async def fail_job(pool, job_id, backoff_seconds: int = 60) -> None:
    """Retry with exponential backoff until retry_limit, then dead-letter as 'failed'."""
    await pool.execute(
        """update jobs set
               retry_count = retry_count + 1,
               state = case when retry_count + 1 >= retry_limit then 'failed' else 'created' end,
               completed_at = case when retry_count + 1 >= retry_limit then now() else null end,
               start_after = now() + make_interval(secs => $2 * power(2, retry_count))
           where id = $1""",
        job_id, backoff_seconds,
    )
