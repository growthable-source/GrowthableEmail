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
    ideal_send_hour: int = 10  # local hour timed sends target in each contact's timezone
    seed_emails: str = ""
    alert_webhook_url: str | None = None
    # Comma-separated Slack member IDs (e.g. "U0123ABC") tagged personally on
    # critical ops alerts (credit exhaustion) in addition to <!channel>.
    alert_mention_ids: str = ""
    slack_enabled: bool = False
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel_id: str = ""
    slack_social_channel_id: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    postal_address: str = ""   # CAN-SPAM physical address for outbound footers
    bot_timezone: str = "Australia/Sydney"
    daily_report_hour: int = 8  # local hour (bot_timezone) the daily digest posts after
    emailable_api_key: str = ""
    verify_approval_threshold: int = 1000  # verify runs above this need a human button-click
    verify_cost_per_email: float = 0.0038  # USD, for the approval message estimate
    weekly_review_enabled: bool = True
    weekly_review_dow: int = 0   # 0=Monday (bot_timezone)
    weekly_review_hour: int = 9  # local hour the weekly review kicks off after
    resonance_api_key: str = ""
    resonance_api_url: str = ""

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
