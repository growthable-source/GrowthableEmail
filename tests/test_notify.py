import json

from app.services.notify import notify_campaign_going_out, notify_post_going_out


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return "999.001"


async def test_notify_campaign_going_out_tags_channel(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, "
        "thread_ts, channel) values ('July Launch', 's', 'newsletter', 'v1', "
        "'100.1', 'C0TEST') returning id")
    slack = FakeSlack()
    await notify_campaign_going_out(pool, slack, cid)
    assert slack.posts == [{"channel": "C0TEST",
                            "text": "<!channel> 🚀 *July Launch* is going out now.",
                            "thread_ts": "100.1"}]


async def test_notify_campaign_going_out_noop_without_channel(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('API Campaign', 's', 'newsletter', 'v1') returning id")
    slack = FakeSlack()
    await notify_campaign_going_out(pool, slack, cid)
    assert slack.posts == []
    # unknown id also no-ops
    import uuid
    await notify_campaign_going_out(pool, slack, uuid.uuid4())
    assert slack.posts == []


async def test_notify_post_going_out_tags_channel_with_preview(pool):
    pid = await pool.fetchval(
        "insert into social_posts (thread_ts, channel, account_ids, content) "
        "values ('500.1', 'C0SOCIAL', array['acc1'], $1) returning id",
        json.dumps({"text": "Big announcement today.", "media": []}))
    slack = FakeSlack()
    await notify_post_going_out(pool, slack, pid)
    assert len(slack.posts) == 1
    post = slack.posts[0]
    assert post["channel"] == "C0SOCIAL" and post["thread_ts"] == "500.1"
    assert "<!channel>" in post["text"] and "Big announcement today." in post["text"]


async def test_notify_post_going_out_noop_without_channel(pool):
    pid = await pool.fetchval(
        "insert into social_posts (thread_ts, account_ids, content) "
        "values ('500.1', array['acc1'], $1) returning id",
        json.dumps({"text": "x", "media": []}))
    slack = FakeSlack()
    await notify_post_going_out(pool, slack, pid)
    assert slack.posts == []
