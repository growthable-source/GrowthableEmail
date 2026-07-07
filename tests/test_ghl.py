import httpx
import pytest
import respx

from app.services.ghl import GHLClient, GHLError

BASE = "https://services.leadconnectorhq.com"


def make_client(**kw) -> GHLClient:
    return GHLClient(token="pit-test", location_id="loc1", rps=10_000, backoff_base=0, **kw)


@respx.mock
async def test_search_contacts_paginates_and_parses():
    page1 = {
        "contacts": [
            {"id": "c1", "email": "Ada@Example.com", "firstNameLowerCase": "ada",
             "lastNameLowerCase": "lovelace", "tags": ["vip"], "dnd": False,
             "customFields": [{"id": "f1", "value": "gold"}], "searchAfter": [1, "c1"]},
            {"id": "c2", "email": "c2@x.co", "dnd": False, "searchAfter": [2, "c2"]},
        ],
        "total": 3,
    }
    page2 = {"contacts": [{"id": "c3", "email": "c3@x.co", "dnd": True, "searchAfter": [3, "c3"]}],
             "total": 3}
    respx.post(f"{BASE}/contacts/search").mock(side_effect=[
        httpx.Response(200, json=page1), httpx.Response(200, json=page2),
    ])
    client = make_client()
    contacts = [c async for c in client.search_contacts(
        filters=[{"field": "tags", "operator": "eq", "value": "vip"}], page_limit=2)]
    assert [c["ghl_contact_id"] for c in contacts] == ["c1", "c2", "c3"]
    assert contacts[0]["email"] == "ada@example.com"
    assert contacts[0]["first_name"] == "ada"
    assert contacts[0]["custom"] == {"f1": "gold"}
    assert contacts[2]["dnd"] is True
    body = respx.calls[0].request.read().decode()
    assert '"locationId": "loc1"' in body or '"locationId":"loc1"' in body
    # second request carries searchAfter cursor from last contact of page 1
    assert "searchAfter" in respx.calls[1].request.read().decode()


@respx.mock
async def test_add_tags_and_set_dnd():
    tag_route = respx.post(f"{BASE}/contacts/c1/tags").mock(return_value=httpx.Response(200, json={}))
    dnd_route = respx.put(f"{BASE}/contacts/c1").mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    await client.add_tags("c1", ["opened-launch"])
    await client.set_dnd_email("c1")
    assert tag_route.called and b"opened-launch" in tag_route.calls[0].request.read()
    dnd_body = dnd_route.calls[0].request.read().decode()
    assert "dndSettings" in dnd_body and "Email" in dnd_body


@respx.mock
async def test_retries_on_429_then_succeeds():
    route = respx.post(f"{BASE}/contacts/c1/tags").mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json={}),
    ])
    await make_client().add_tags("c1", ["x"])
    assert route.call_count == 2


@respx.mock
async def test_hard_4xx_raises_immediately():
    route = respx.post(f"{BASE}/contacts/c1/tags").mock(return_value=httpx.Response(422, json={"msg": "bad"}))
    with pytest.raises(GHLError):
        await make_client().add_tags("c1", ["x"])
    assert route.call_count == 1
