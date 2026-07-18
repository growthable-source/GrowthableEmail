import copy
import json
from types import SimpleNamespace

from app.services.bot import BotEngine, process_bot_turns
from app.services.jobs import enqueue
from tests.helpers import make_settings


class Block(SimpleNamespace):
    def model_dump(self, mode="json"):
        return {k: v for k, v in vars(self).items()}


def text_block(t):
    return Block(type="text", text=t)


def tool_block(name, input, id="tu_1"):
    return Block(type="tool_use", name=name, input=input, id=id)


class FakeAnthropic:
    """Scripted responses; records requests."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **kwargs):
        # snapshot: the engine mutates the same messages list after the call
        self.requests.append(copy.deepcopy(kwargs))
        content = self._responses.pop(0)
        return SimpleNamespace(content=content, stop_reason="tool_use" if any(
            b.type == "tool_use" for b in content) else "end_turn")


class FakeSlack:
    def __init__(self):
        self.posts = []

    async def post_message(self, channel, text=None, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "blocks": blocks,
                           "thread_ts": thread_ts})
        return "999.001"


class FakeGHL:
    async def list_tags(self):
        return ["newsletter", "vip"]


CONTENT = {"headline": "Hello", "sections": [{"paragraphs": ["World."]}]}


def make_engine(pool, responses, slack=None):
    slack = slack or FakeSlack()
    engine = BotEngine(pool=pool, settings=make_settings(), ghl=FakeGHL(), slack=slack,
                       resend=None, client=FakeAnthropic(responses))
    return engine, slack


TURN = {"channel": "C0TEST", "thread_ts": "100.1", "user": "URYAN", "text": "<@UBOT> hi"}


async def test_plain_reply_posts_to_thread_and_saves_session(pool):
    engine, slack = make_engine(pool, [[text_block("Hello Ryan! What campaign?")]])
    await engine.handle_turn(TURN)
    assert slack.posts[0]["thread_ts"] == "100.1"
    assert "What campaign" in slack.posts[0]["text"]
    session = await pool.fetchrow("select messages from bot_sessions where thread_ts='100.1'")
    messages = json.loads(session["messages"])
    assert messages[0]["role"] == "user" and messages[-1]["role"] == "assistant"


async def test_tool_call_creates_campaign_and_links_session(pool):
    engine, slack = make_engine(pool, [
        [tool_block("create_campaign", {"name": "July", "subject": "Big",
                                        "tag": "newsletter", "template": "newsletter",
                                        "content": CONTENT})],
        [text_block("Created!")],
    ])
    await engine.handle_turn(TURN)
    campaign = await pool.fetchrow("select * from campaigns")
    assert campaign["template_ref"] == "newsletter"
    assert json.loads(campaign["content"]) == CONTENT
    assert campaign["thread_ts"] == "100.1" and campaign["channel"] == "C0TEST"
    assert json.loads(campaign["audience_filter"]) == [
        {"field": "tags", "operator": "eq", "value": "newsletter"}]
    assert (await pool.fetchval("select campaign_id from bot_sessions")) == campaign["id"]
    # tool result went back in ONE user message
    second_request = engine._client.requests[1]
    tool_results = second_request["messages"][-1]
    assert tool_results["role"] == "user"
    assert tool_results["content"][0]["type"] == "tool_result"


async def test_propose_send_requires_seed_test(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('x', 's', 'newsletter', 'v1') returning id")
    engine, slack = make_engine(pool, [
        [tool_block("propose_send", {"campaign_id": str(cid)})],
        [text_block("You need a seed test first.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "seed test" in result["error"].lower()
    assert all(p["blocks"] is None for p in slack.posts)  # no approval buttons posted


async def test_propose_send_posts_approval_buttons_after_seed(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, seed_tested_at) "
        "values ('July', 'Big', 'newsletter', 'v1', now()) returning id")
    engine, slack = make_engine(pool, [
        [tool_block("propose_send", {"campaign_id": str(cid),
                                     "when_iso": "2026-07-10T09:00:00+10:00"})],
        [text_block("Awaiting approval.")],
    ])
    await engine.handle_turn(TURN)
    buttons = next(p for p in slack.posts if p["blocks"])
    action_ids = [e["action_id"] for e in buttons["blocks"][-1]["elements"]]
    assert action_ids == ["approve_send", "cancel_send"]
    value = json.loads(buttons["blocks"][-1]["elements"][0]["value"])
    assert value == {"campaign_id": str(cid), "when": "2026-07-10T09:00:00+10:00",
                     "per_day": None, "per_hour": None}


async def test_claude_error_posts_apology_and_completes_job(pool):
    class ExplodingClient:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise RuntimeError("api down")
    slack = FakeSlack()
    engine = BotEngine(pool=pool, settings=make_settings(), ghl=FakeGHL(), slack=slack,
                       resend=None, client=ExplodingClient())
    await engine.handle_turn(TURN)
    assert "wrong" in slack.posts[0]["text"].lower() or "error" in slack.posts[0]["text"].lower()


async def test_process_bot_turns_drains_queue(pool):
    await enqueue(pool, "bot_turn", TURN)
    engine, slack = make_engine(pool, [[text_block("hi")]])
    assert await process_bot_turns(pool, {"C0TEST": engine}) == 1
    assert (await pool.fetchval("select state from jobs")) == "completed"


VALID_DOC = ("<!DOCTYPE html><html><body><h1>Hi {{first_name}}</h1>"
             "<p>Growthable LLC · 1942 Broadway St STE 314C, Boulder CO 80302, US · "
             '<a href="{{unsubscribe_url}}">Unsubscribe</a></p></body></html>')


async def test_create_campaign_with_custom_template(pool):
    engine, slack = make_engine(pool, [
        [tool_block("create_campaign", {
            "name": "Video promo", "subject": "Watch this", "tag": "test",
            "template": "custom", "content": {"html_body": VALID_DOC}})],
        [text_block("Created!")],
    ])
    await engine.handle_turn(TURN)
    row = await pool.fetchrow("select template_ref, content from campaigns")
    assert row["template_ref"] == "custom"
    assert "html_body" in json.loads(row["content"])


async def test_create_campaign_rejects_non_compliant_html(pool):
    engine, slack = make_engine(pool, [
        [tool_block("create_campaign", {
            "name": "Bad", "subject": "s", "tag": "test", "template": "custom",
            "content": {"html_body": "<html><body>no footer at all</body></html>"}})],
        [text_block("Fixing.")],
    ])
    await engine.handle_turn(TURN)
    assert (await pool.fetchval("select count(*) from campaigns")) == 0
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "unsubscribe_url" in result["error"]


async def test_build_engaged_segment_tags_unique_contacts(pool):
    class ConvoGHL(FakeGHL):
        async def search_conversations(self, last_message_after_ms, page_limit=50):
            for cv in [{"contact_id": "c1", "last_message_date": 99},
                       {"contact_id": "c2", "last_message_date": 98},
                       {"contact_id": "c1", "last_message_date": 97},  # duplicate contact
                       {"contact_id": None, "last_message_date": 96}]:  # no contact id
                yield cv

    slack = FakeSlack()
    engine = BotEngine(pool=pool, settings=make_settings(), ghl=ConvoGHL(), slack=slack,
                       resend=None, client=FakeAnthropic([
                           [tool_block("build_engaged_segment", {"days": 90, "tag": "engaged-90d"})],
                           [text_block("Segment building.")],
                       ]))
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert result["contacts_found"] == 2 and result["tag"] == "engaged-90d"
    jobs = [json.loads(r["data"]) for r in await pool.fetch(
        "select data from jobs where name='ghl_writeback'")]
    assert sorted(j["contact_id"] for j in jobs) == ["c1", "c2"]
    assert all(j["tags"] == ["engaged-90d"] for j in jobs)


async def test_segment_progress_reports_job_states(pool):
    await enqueue(pool, "ghl_writeback", {"kind": "add_tags", "contact_id": "c1", "tags": ["x"]})
    engine, slack = make_engine(pool, [
        [tool_block("segment_progress", {})],
        [text_block("1 pending.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert result == {"created": 1}


async def test_propose_send_blocked_until_verified(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, seed_tested_at) "
        "values ('July', 'Big', 'newsletter', 'v1', now()) returning id")
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email) values ('c1', 'u@x.co')")
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, 'c1')", cid)
    engine, slack = make_engine(pool, [
        [tool_block("propose_send", {"campaign_id": str(cid)})],
        [text_block("Verification still pending.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "unverified" in result["error"]
    assert all(p["blocks"] is None for p in slack.posts)  # no approval buttons posted


async def test_propose_send_audience_counts_only_verified_valid(pool):
    from app.services.verification import upsert_verdicts
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, seed_tested_at) "
        "values ('July', 'Big', 'newsletter', 'v1', now()) returning id")
    for i, verdict in enumerate(["valid", "risky"]):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email) values ($1, $2)",
            f"c{i}", f"u{i}@x.co")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
            cid, f"c{i}")
        await upsert_verdicts(pool, [(f"u{i}@x.co", verdict, "x")])
    engine, slack = make_engine(pool, [
        [tool_block("propose_send", {"campaign_id": str(cid)})],
        [text_block("Awaiting approval.")],
    ])
    await engine.handle_turn(TURN)
    buttons = next(p for p in slack.posts if p["blocks"])
    # both contacts verified (no gate error), but only the valid one is counted
    assert "*Audience:* 1 contacts" in buttons["blocks"][0]["text"]["text"]


async def test_verification_status_tool(pool):
    from app.services.verification import upsert_verdicts
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version) "
        "values ('July', 'Big', 'newsletter', 'v1') returning id")
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email) values ('c1', 'u@x.co')")
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, 'c1')", cid)
    await upsert_verdicts(pool, [("u@x.co", "valid", "ok")])
    engine, slack = make_engine(pool, [
        [tool_block("verification_status", {"campaign_id": str(cid)})],
        [text_block("All verified.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert result == {"valid": 1, "unverified": 0}


async def test_resume_campaign_posts_confirm_buttons(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('July', 'Big', 'newsletter', 'v1', 'paused') returning id")
    engine, slack = make_engine(pool, [
        [tool_block("resume_campaign", {"campaign_id": str(cid)})],
        [text_block("Confirm to resume.")],
    ])
    await engine.handle_turn(TURN)
    buttons = next(p for p in slack.posts if p["blocks"])
    action_ids = [e["action_id"] for e in buttons["blocks"][-1]["elements"]]
    assert action_ids == ["approve_resume", "cancel_resume"]
    # still paused until a human clicks
    assert (await pool.fetchval("select status from campaigns where id=$1", cid)) == "paused"


async def test_resume_campaign_rejects_unpaused(pool):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status) "
        "values ('July', 'Big', 'newsletter', 'v1', 'ready') returning id")
    engine, slack = make_engine(pool, [
        [tool_block("resume_campaign", {"campaign_id": str(cid)})],
        [text_block("Not paused.")],
    ])
    await engine.handle_turn(TURN)
    result = json.loads(engine._client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "not paused" in result["error"]
    assert all(p["blocks"] is None for p in slack.posts)
