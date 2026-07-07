import httpx
import pytest
import respx

from app.services.resend_client import HardSendError, ResendClient, TransientSendError

API = "https://api.resend.com"


def make_client() -> ResendClient:
    return ResendClient(api_key="re_test", rps=10_000, backoff_base=0)


@respx.mock
async def test_send_email_returns_id_and_sends_auth():
    route = respx.post(f"{API}/emails").mock(
        return_value=httpx.Response(200, json={"id": "email_123"})
    )
    email_id = await make_client().send_email({
        "from": "a <a@b.co>", "to": ["x@y.co"], "subject": "s", "html": "<p>h</p>",
    })
    assert email_id == "email_123"
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer re_test"
    assert b'"subject"' in req.read()


@respx.mock
async def test_retries_5xx_then_succeeds():
    route = respx.post(f"{API}/emails").mock(side_effect=[
        httpx.Response(500), httpx.Response(200, json={"id": "email_1"}),
    ])
    assert await make_client().send_email({"to": ["x@y.co"]}) == "email_1"
    assert route.call_count == 2


@respx.mock
async def test_exhausted_retries_raise_transient():
    respx.post(f"{API}/emails").mock(return_value=httpx.Response(429))
    with pytest.raises(TransientSendError):
        await make_client().send_email({"to": ["x@y.co"]})


@respx.mock
async def test_validation_error_raises_hard():
    route = respx.post(f"{API}/emails").mock(
        return_value=httpx.Response(422, json={"message": "Invalid `to`"})
    )
    with pytest.raises(HardSendError):
        await make_client().send_email({"to": ["bad"]})
    assert route.call_count == 1
