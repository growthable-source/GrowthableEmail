import json

from app.services.social_dispatch import notify_due_social_posts


async def seed_post(pool, status="scheduled", schedule_offset="- interval '1 minute'",
                    notified=False):
    return await pool.fetchval(
        f"""insert into social_posts (thread_ts, channel, account_ids, content, status,
                                      schedule_at, notified_at)
            values ('500.1', 'C0SOCIAL', array['acc1'], $1, $2,
                    now() {schedule_offset}, {"now()" if notified else "null"})
            returning id""",
        json.dumps({"text": "x", "media": []}), status)


async def test_flags_due_scheduled_posts_once(pool):
    due = await seed_post(pool)
    future = await seed_post(pool, schedule_offset="+ interval '1 hour'")
    already_published = await seed_post(pool, status="published")
    already_notified = await seed_post(pool, notified=True)

    assert await notify_due_social_posts(pool) == [due]
    assert (await pool.fetchval(
        "select notified_at is not null from social_posts where id=$1", due))
    assert await notify_due_social_posts(pool) == []  # idempotent
    for pid in (future, already_published, already_notified):
        assert pid not in [due]
