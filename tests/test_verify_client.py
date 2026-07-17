import httpx
import pytest

from app.services.verify_client import EmailableClient, map_result


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return EmailableClient("key_test", client=httpx.AsyncClient(transport=transport))


async def test_create_batch_returns_id():
    def handler(request):
        assert request.url.path == "/v1/batch"
        return httpx.Response(200, json={"id": "batch_1"})
    client = make_client(handler)
    assert await client.create_batch(["a@x.com", "b@y.com"]) == "batch_1"


async def test_get_batch_pending_returns_none():
    def handler(request):
        return httpx.Response(200, json={"processed": 5, "total": 10})
    client = make_client(handler)
    assert await client.get_batch("batch_1") is None


async def test_get_batch_complete_returns_emails():
    def handler(request):
        return httpx.Response(200, json={"emails": [
            {"email": "a@x.com", "state": "deliverable", "reason": "accepted_email"}]})
    client = make_client(handler)
    result = await client.get_batch("batch_1")
    assert result[0]["email"] == "a@x.com"


@pytest.mark.parametrize("raw,expected", [
    ({"email": "a@x.com", "state": "deliverable", "reason": "accepted_email"},
     ("valid", "accepted_email")),
    ({"email": "a@x.com", "state": "undeliverable", "reason": "rejected_email"},
     ("invalid", "rejected_email")),
    ({"email": "a@x.com", "state": "risky", "reason": "low_deliverability"},
     ("risky", "low_deliverability")),
    ({"email": "a@x.com", "state": "unknown", "reason": "timeout"},
     ("unknown", "timeout")),
    ({"email": "a@x.com", "state": "deliverable", "reason": "accepted_email",
      "role": True}, ("risky", "role")),
    ({"email": "a@x.com", "state": "deliverable", "reason": "accepted_email",
      "disposable": True}, ("invalid", "disposable")),
])
def test_map_result(raw, expected):
    assert map_result(raw) == expected
