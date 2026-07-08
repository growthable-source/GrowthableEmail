import json

import app.services.social_bot as social_bot_module
from app.services.social_bot import SocialBot
from tests.helpers import make_settings
from tests.test_bot import FakeAnthropic, FakeSlack, text_block, tool_block

TURN = {"channel": "C0SOCIAL", "thread_ts": "500.1", "user": "URYAN", "text": "<@UBOT> post"}


class FakeGHLSocial:
    def __init__(self):
        self.posts = []

    async def list_social_accounts(self):
        return [{"id": "acc1", "platform": "linkedin", "name": "Growthable"},
                {"id": "acc2", "platform": "facebook", "name": "Growthable"}]


def make_engine(pool, responses, slack=None):
    slack = slack or FakeSlack()
    engine = SocialBot(pool=pool, settings=make_settings(), ghl=FakeGHLSocial(),
                       slack=slack, client=FakeAnthropic(responses))
    return engine, slack


async def test_list_accounts_tool(pool):
    engine, slack = make_engine(pool, [
        [tool_block("list_social_accounts", {})],
        [text_block("You have LinkedIn and Facebook connected.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert [a["platform"] for a in result["accounts"]] == ["linkedin", "facebook"]


async def test_draft_post_saves_row_and_previews(pool):
    engine, slack = make_engine(pool, [
        [tool_block("draft_post", {"account_ids": ["acc1"],
                                   "text": "Stop answering the same questions.",
                                   "media_urls": ["https://x/img.png"]})],
        [text_block("Draft ready — happy with it?")],
    ])
    await engine.handle_turn(TURN)
    row = await pool.fetchrow("select * from social_posts")
    assert row["status"] == "draft" and row["account_ids"] == ["acc1"]
    assert json.loads(row["content"]) == {"text": "Stop answering the same questions.",
                                          "media": ["https://x/img.png"]}
    assert row["thread_ts"] == "500.1"
    preview = slack.posts[0]
    assert "Stop answering" in preview["text"] and "https://x/img.png" in preview["text"]


async def test_generate_image_tool_uses_service(pool, monkeypatch):
    async def fake_generate(pool_, settings, prompt):
        assert "rocket" in prompt
        return "http://testserver/images/abc"

    monkeypatch.setattr(social_bot_module, "generate_image", fake_generate)
    engine, slack = make_engine(pool, [
        [tool_block("generate_image", {"prompt": "a rocket"})],
        [text_block("Image ready.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert result == {"image_url": "http://testserver/images/abc"}


async def test_propose_publish_posts_buttons_for_draft_only(pool):
    post_id = await pool.fetchval(
        "insert into social_posts (thread_ts, account_ids, content) "
        "values ('500.1', array['acc1','acc2'], $1) returning id",
        json.dumps({"text": "Big news.", "media": []}))
    engine, slack = make_engine(pool, [
        [tool_block("propose_publish", {"post_id": str(post_id),
                                        "when_iso": "2030-01-01T09:00:00+10:00"})],
        [text_block("Awaiting approval.")],
    ])
    await engine.handle_turn(TURN)
    buttons = next(p for p in slack.posts if p["blocks"])
    action_ids = [e["action_id"] for e in buttons["blocks"][-1]["elements"]]
    assert action_ids == ["approve_post", "cancel_post"]
    value = json.loads(buttons["blocks"][-1]["elements"][0]["value"])
    assert value == {"post_id": str(post_id), "when": "2030-01-01T09:00:00+10:00"}
    # non-draft posts are rejected
    await pool.execute("update social_posts set status='published' where id=$1", post_id)
    engine2, slack2 = make_engine(pool, [
        [tool_block("propose_publish", {"post_id": str(post_id)})],
        [text_block("Already out.")],
    ])
    await engine2.handle_turn(TURN)
    result = json.loads(engine2._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "already published" in result["error"]


async def test_update_post_edits_draft(pool):
    post_id = await pool.fetchval(
        "insert into social_posts (thread_ts, account_ids, content) "
        "values ('500.1', array['acc1'], $1) returning id",
        json.dumps({"text": "old", "media": []}))
    engine, slack = make_engine(pool, [
        [tool_block("update_post", {"post_id": str(post_id), "text": "new copy",
                                    "media_urls": ["https://x/new.png"]})],
        [text_block("Updated.")],
    ])
    await engine.handle_turn(TURN)
    row = await pool.fetchrow("select content from social_posts")
    assert json.loads(row["content"]) == {"text": "new copy", "media": ["https://x/new.png"]}
