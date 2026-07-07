from app.services.unsub_tokens import make_token, parse_token

SECRET = "unsub-secret"


def test_roundtrip():
    token = make_token("Ada@Example.COM", "camp-123", SECRET)
    assert parse_token(token, SECRET) == ("ada@example.com", "camp-123")


def test_tampered_token_rejected():
    token = make_token("ada@example.com", "camp-123", SECRET)
    payload, sig = token.split(".")
    assert parse_token(f"{payload}x.{sig}", SECRET) is None
    assert parse_token(token, "other-secret") is None


def test_garbage_rejected():
    assert parse_token("not-a-token", SECRET) is None
    assert parse_token("a.b.c", SECRET) is None
    assert parse_token("", SECRET) is None
