import re
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    resend_api_key: str
    resend_webhook_secret: str
    ghl_pi_token: str
    ghl_location_id: str
    ghl_webhook_secret: str
    unsub_signing_secret: str
    api_key: str
    public_base_url: str
    from_email: str
    send_rps: float = 2.0
    daily_send_cap: int = 500
    seed_emails: str = ""
    alert_webhook_url: str | None = None
    slack_enabled: bool = False
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""
    slack_social_channel_id: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    bot_timezone: str = "Australia/Sydney"
    daily_report_hour: int = 8  # local hour (bot_timezone) the daily digest posts after

    @property
    def seed_list(self) -> list[str]:
        return [e.strip() for e in self.seed_emails.split(",") if e.strip()]

    @property
    def from_domain(self) -> str:
        match = re.search(r"@([\w.-]+)", self.from_email)
        return match.group(1) if match else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
