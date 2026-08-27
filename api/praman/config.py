"""Central configuration. One `Settings` object, loaded once, injected
everywhere else — nothing outside this module reads `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Reversibility Ladder weights (CLAUDE.md §5) ---
# Named constants, never inline literals, per the non-negotiable rule.
REVERSIBILITY_WEIGHT_RETURN = 0.35
REVERSIBILITY_WEIGHT_CLASS = 0.25
REVERSIBILITY_WEIGHT_SPEED = 0.15
REVERSIBILITY_WEIGHT_RESTOCK = 0.10
REVERSIBILITY_WEIGHT_VALUE = 0.15

RETURN_WINDOW_NORMALISATION_DAYS = 14
FULFILMENT_NORMALISATION_HOURS = 336  # 14 days
RESTOCKING_COST_NORMALISATION_PCT = 0.30

CATEGORY_CLASS_SCORES: dict[str, float] = {
    "perishable": 0.95,
    "consumable": 0.90,
    "digital": 0.70,
    "durable": 0.55,
    "service": 0.35,
    "bespoke": 0.05,
}

BAND_GREEN_THRESHOLD = 0.75
BAND_AMBER_THRESHOLD = 0.40

CONFIDENCE_THRESHOLD = 0.75

# --- Quote TTLs (seconds), by category_class ---
QUOTE_TTL_PERISHABLE_CONSUMABLE_S = 60
QUOTE_TTL_DURABLE_S = 600
QUOTE_TTL_BESPOKE_S = 900
QUOTE_TTL_DEMO_MODE_S = 30

COOLING_OFF_WINDOW_S = 30 * 60
COOLING_OFF_WINDOW_DEMO_MODE_S = 60

MERCHANT_APPROVAL_TIMEOUT_S = 15 * 60

AGENT_CLOCK_SKEW_TOLERANCE_S = 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    demo_mode: bool = True
    app_secret: str = "praman-dev-secret-change-in-prod"

    database_url: str = "sqlite+aiosqlite:///./praman.db"
    redis_url: str = "redis://localhost:6379/0"

    razorpay_key_id: str = "rzp_test_placeholder"
    razorpay_key_secret: str = "placeholder"
    razorpay_webhook_secret: str = "placeholder"
    razorpay_use_fake: bool = False

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"

    llm_provider: str = "fake"  # gemini | openai | anthropic | fake
    llm_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    @field_validator("razorpay_key_id")
    @classmethod
    def _reject_live_keys(cls, v: str) -> str:
        if v and not v.startswith("rzp_test_"):
            raise ValueError(
                "RAZORPAY_KEY_ID must be a TEST MODE key (rzp_test_...). "
                "Live keys are never permitted in this repo."
            )
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
