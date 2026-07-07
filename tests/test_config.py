from tests.helpers import make_settings


def test_settings_construct_and_defaults():
    s = make_settings()
    assert s.send_rps == 1000.0
    assert s.daily_send_cap == 500
    assert s.alert_webhook_url is None


def test_seed_list_parses_csv():
    s = make_settings(seed_emails="a@x.co, b@x.co ,")
    assert s.seed_list == ["a@x.co", "b@x.co"]


def test_from_domain_extracted():
    s = make_settings(from_email="Growthable <news@news.growthable.io>")
    assert s.from_domain == "news.growthable.io"


def test_env_vars_override(monkeypatch):
    for key, val in {
        "DATABASE_URL": "postgresql://env/db", "RESEND_API_KEY": "re_env",
        "RESEND_WEBHOOK_SECRET": "whsec_env", "GHL_PI_TOKEN": "pit-env",
        "GHL_LOCATION_ID": "loc-env", "GHL_WEBHOOK_SECRET": "hs",
        "UNSUB_SIGNING_SECRET": "us", "PUBLIC_BASE_URL": "https://x",
        "FROM_EMAIL": "a <a@b.co>", "DAILY_SEND_CAP": "2000",
    }.items():
        monkeypatch.setenv(key, val)
    from app.config import Settings
    s = Settings()
    assert s.daily_send_cap == 2000
    assert s.resend_api_key == "re_env"
