import base64
import hashlib
import hmac
import time

from app.config import Settings

TEST_WEBHOOK_KEY = base64.b64encode(b"0" * 32).decode()


def make_settings(**overrides) -> Settings:
    defaults = dict(
        database_url="postgresql://postgres:test@localhost:54329/postgres",
        resend_api_key="re_test_key",
        resend_webhook_secret=f"whsec_{TEST_WEBHOOK_KEY}",
        ghl_pi_token="pit-test",
        ghl_location_id="loc_test",
        ghl_webhook_secret="hook-secret",
        unsub_signing_secret="unsub-secret",
        public_base_url="http://testserver",
        from_email="Growthable <news@news.growthable.io>",
        send_rps=1000.0,
        daily_send_cap=500,
        seed_emails="seed@growthable.io",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def svix_headers(secret: str, payload: str, msg_id: str = "msg_1") -> dict:
    ts = str(int(time.time()))  # svix rejects timestamps outside its tolerance window
    key = base64.b64decode(secret.split("_", 1)[1])
    to_sign = f"{msg_id}.{ts}.{payload}".encode()
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}
