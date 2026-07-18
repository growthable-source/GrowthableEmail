import json

import httpx
import respx

from app.services.broadcast import (broadcast_full_html, process_broadcast_campaigns,
                                    render_broadcast_html)
from app.services.resend_client import ResendClient
from tests.helpers import make_settings, verify_all_contacts

API = "https://api.resend.com"

HTML_BODY = ("<!DOCTYPE html><html><body><p>Hi {{first_name}},</p>"
             "<p>Big news.</p>"
             "<a href='{{unsubscribe_url}}'>Unsubscribe</a> · Woonona"
             "</body></html>")


def make_resend() -> ResendClient:
    return ResendClient("re_test", rps=10_000, backoff_base=0)


async def seed_broadcast_campaign(pool, status="dispatching", n_contacts=2,
                                  channel=None, thread_ts=None):
    cid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, status, "
        "send_via, content, channel, thread_ts) "
        "values ('July', 'Big', 'custom', 'v1', $1, 'broadcast', $2, $3, $4) returning id",
        status, json.dumps({"html_body": HTML_BODY}), channel, thread_ts)
    for i in range(n_contacts):
        await pool.execute(
            "insert into contacts_cache (ghl_contact_id, email, first_name) "
            "values ($1, $2, $3)", f"c{i}", f"u{i}@x.co", f"Name{i}")
        await pool.execute(
            "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, $2)",
            cid, f"c{i}")
    await verify_all_contacts(pool)
    return cid


def mock_resend_routes(*import_statuses):
    def status_response(request):
        status = import_statuses[min(status_response.calls, len(import_statuses) - 1)] \
            if import_statuses else "completed"
        status_response.calls += 1
        return httpx.Response(200, json={"id": "imp_1", "status": status})
    status_response.calls = 0
    return {
        "segment": respx.post(f"{API}/segments").mock(
            return_value=httpx.Response(200, json={"id": "seg_1"})),
        "import": respx.post(f"{API}/contacts/imports").mock(
            return_value=httpx.Response(200, json={"id": "imp_1"})),
        "import_status": respx.get(f"{API}/contacts/imports/imp_1").mock(
            side_effect=status_response),
        "broadcast": respx.post(f"{API}/broadcasts").mock(
            return_value=httpx.Response(200, json={"id": "bc_1"})),
    }


def test_broadcast_full_html_translates_tokens():
    html = broadcast_full_html(HTML_BODY)
    assert "{{{contact.first_name|there}}}" in html
    assert "{{{RESEND_UNSUBSCRIBE_URL}}}" in html
    assert "{{first_name}}" not in html and "{{unsubscribe_url}}" not in html


async def test_render_broadcast_html_requires_unsub(pool):
    campaign = {"template_ref": "custom",
                "content": json.dumps({"html_body": "<html>no unsub</html>"})}
    import pytest

    from app.services.renderer import RenderError
    with pytest.raises(RenderError):
        await render_broadcast_html(campaign)


@respx.mock
async def test_full_flow_import_then_broadcast(pool):
    routes = mock_resend_routes("processing", "completed")
    cid = await seed_broadcast_campaign(pool)
    # third contact is suppressed → must be excluded from the CSV and sends
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email) values ('c9', 'sup@x.co')")
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, 'c9')", cid)
    await pool.execute(
        "insert into suppressions (email, reason, source) values "
        "('sup@x.co', 'complaint', 'resend')")
    await verify_all_contacts(pool)  # suppression, not verification, must exclude c9

    # pass 1: creates segment + starts the CSV import; import still processing
    created = await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert created == 0
    assert routes["segment"].called and routes["import"].called
    row = await pool.fetchrow(
        "select resend_segment_id, resend_import_id, resend_broadcast_id, status "
        "from campaigns where id=$1", cid)
    assert row["resend_segment_id"] == "seg_1" and row["resend_import_id"] == "imp_1"
    assert row["resend_broadcast_id"] is None and row["status"] == "dispatching"
    import_body = routes["import"].calls[0].request.read()
    assert b"u0@x.co" in import_body and b"u1@x.co" in import_body
    assert b"sup@x.co" not in import_body
    assert b"seg_1" in import_body  # import lands in the campaign's segment

    # pass 2: import completed → broadcast created against the segment
    created = await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert created == 1
    payload = json.loads(routes["broadcast"].calls[0].request.read())
    assert payload["segment_id"] == "seg_1" and payload["send"] is True
    assert payload["subject"] == "Big"
    assert "{{{contact.first_name|there}}}" in payload["html"]
    assert "{{{RESEND_UNSUBSCRIBE_URL}}}" in payload["html"]
    row = await pool.fetchrow(
        "select resend_broadcast_id, status from campaigns where id=$1", cid)
    assert row["resend_broadcast_id"] == "bc_1" and row["status"] == "completed"
    sends = await pool.fetch("select email, status, via from sends order by email")
    assert [(s["email"], s["status"], s["via"]) for s in sends] == [
        ("u0@x.co", "sent", "broadcast"), ("u1@x.co", "sent", "broadcast")]

    # pass 3: nothing left to do
    assert await process_broadcast_campaigns(pool, make_settings(), make_resend()) == 0


@respx.mock
async def test_import_still_processing_waits(pool):
    routes = mock_resend_routes("processing")
    await seed_broadcast_campaign(pool)
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    created = await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert created == 0 and not routes["broadcast"].called
    assert (await pool.fetchval("select status from campaigns")) == "dispatching"


@respx.mock
async def test_import_failed_pauses_campaign(pool):
    mock_resend_routes("failed")
    await seed_broadcast_campaign(pool)
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert (await pool.fetchval("select status from campaigns")) == "paused"


@respx.mock
async def test_empty_audience_pauses_campaign(pool):
    mock_resend_routes()
    await seed_broadcast_campaign(pool, n_contacts=0)
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert (await pool.fetchval("select status from campaigns")) == "paused"


@respx.mock
async def test_scheduled_campaign_imports_early_but_does_not_send(pool):
    routes = mock_resend_routes()
    await seed_broadcast_campaign(pool, status="scheduled")
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert routes["import"].called and not routes["broadcast"].called
    row = await pool.fetchrow("select status, resend_import_id from campaigns")
    assert row["status"] == "scheduled" and row["resend_import_id"] == "imp_1"


@respx.mock
async def test_broadcast_sends_do_not_count_against_daily_cap(pool):
    from app.services.dispatch import enqueue_campaign_sends, process_send_queue
    routes = mock_resend_routes()
    await seed_broadcast_campaign(pool)  # 2 broadcast recipients
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    await process_broadcast_campaigns(pool, make_settings(), make_resend())
    assert routes["broadcast"].called

    # queue-path campaign still dispatches under a cap the broadcast would have blown
    respx.post(f"{API}/emails").mock(return_value=httpx.Response(200, json={"id": "em_q"}))
    qid = await pool.fetchval(
        "insert into campaigns (name, subject, template_ref, template_version, content) "
        "values ('Drip', 'd', 'custom', 'v1', $1) returning id",
        json.dumps({"html_body": HTML_BODY}))
    await pool.execute(
        "insert into contacts_cache (ghl_contact_id, email) values ('q1', 'drip@x.co')")
    await pool.execute(
        "insert into campaign_contacts (campaign_id, ghl_contact_id) values ($1, 'q1')", qid)
    await verify_all_contacts(pool)
    await enqueue_campaign_sends(pool, qid)
    settings = make_settings(daily_send_cap=2)  # 2 broadcast sends already recorded today
    sent = await process_send_queue(pool, settings, make_resend())
    assert sent == 1
