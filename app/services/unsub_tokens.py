import base64
import hashlib
import hmac


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest())


def make_token(email: str, campaign_id: str, secret: str) -> str:
    payload_b64 = _b64(f"{email.strip().lower()}|{campaign_id}".encode())
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def parse_token(token: str, secret: str) -> tuple[str, str] | None:
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    if not hmac.compare_digest(sig, _sign(payload_b64, secret)):
        return None
    try:
        email, campaign_id = _unb64(payload_b64).decode().split("|", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    return email, campaign_id
