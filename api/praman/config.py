"""Central configuration. One `Settings` object, loaded once, injected
everywhere else — nothing outside this module reads `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Reversibility Ladder weights (the design spec §5) ---
# Named constants, never inline literals, per the non-negotiable rule.
REVERSIBILITY_WEIGHT_UNWIND = 0.35
REVERSIBILITY_WEIGHT_CLASS = 0.25
REVERSIBILITY_WEIGHT_SPEED = 0.15
REVERSIBILITY_WEIGHT_RESTOCK = 0.10
REVERSIBILITY_WEIGHT_VALUE = 0.15

RETURN_WINDOW_NORMALISATION_DAYS = 14
FULFILMENT_NORMALISATION_HOURS = 336  # 14 days
RESTOCKING_COST_NORMALISATION_PCT = 0.30

# f_unwind (core/reversibility.py): real-world "returnless refund" economics
# for perishable/consumable/digital items — a merchant refunding without
# asking for the item back once the physical/logistics cost of a real
# return would exceed what's recovered. Retail research on this places the
# threshold around $15-30 (return processing commonly runs 20-65% of item
# value once shipping/labor/restocking are counted) for DTC-scale
# operations; a kirana-scale merchant with no formal reverse-logistics
# infrastructure at all absorbs small losses as ordinary practice at a
# correspondingly lower absolute rupee ceiling. ₹1,000 is the deliberate,
# documented choice here — see ARCHITECTURE.md's reversibility formula
# entry for the sources and reasoning, not a number picked to fit any
# particular test result.
UNWIND_FREE_CEILING_PAISE = 100_000  # ₹1,000
UNWIND_FREE_CATEGORY_CLASSES = frozenset({"perishable", "consumable", "digital"})

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
# Bespoke items are the ones that trigger R08 merchant-approval escalation
# (hard-zero reversibility), and a real human needs real time to see the
# WhatsApp/Telegram message and tap Approve -- MERCHANT_APPROVAL_TIMEOUT_S
# below gives them 15 minutes for exactly that reason. Flattening every
# class's demo-mode TTL to 30s (the plain QUOTE_TTL_DEMO_MODE_S) silently
# broke that: the quote is dead long before a human can react, so R05
# (QUOTE_EXPIRED) fires on the gate re-run and the approval can never
# actually succeed. Found live -- a real approved order still got BLOCKed.
QUOTE_TTL_BESPOKE_DEMO_MODE_S = 5 * 60

COOLING_OFF_WINDOW_S = 30 * 60
COOLING_OFF_WINDOW_DEMO_MODE_S = 60

MERCHANT_APPROVAL_TIMEOUT_S = 15 * 60

AGENT_CLOCK_SKEW_TOLERANCE_S = 60

# --- Gate: velocity (R10) ---
# "No more than N ALLOWed transactions per agent within a rolling window" —
# guards against envelope drain by salami-slicing (the design spec §8 threat
# model). Not specified numerically by the spec; chosen as a reasonable
# default and named here rather than inlined, per the non-negotiable rule.
VELOCITY_WINDOW_S = 60
VELOCITY_MAX_TRANSACTIONS = 5
VELOCITY_WINDOW_DEMO_MODE_S = 30
VELOCITY_MAX_TRANSACTIONS_DEMO_MODE = 3

# --- Gate: idempotency (R12) ---
IDEMPOTENCY_KEY_TTL_S = 24 * 60 * 60

# --- Gate: daily cap tracking (R11) ---
# Slightly over 24h so a request right at day-boundary doesn't lose its
# counter mid-check.
DAILY_CAP_TTL_S = 25 * 60 * 60


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
    twilio_use_fake: bool = False
    public_base_url: str = "http://localhost:8000"

    # Telegram — added after live phone testing found this Twilio account's
    # Trial tier can't fetch Message/Media resources at all (see
    # ARCHITECTURE.md's "Post-Phase-7" section), a restriction with no
    # code-level workaround. Telegram's Bot API has no equivalent approval
    # gate: freeform replies and media downloads both work immediately on a
    # brand-new bot token. `whatsapp/telegram_client.py` implements the same
    # `WhatsAppClient`-shaped Protocol Twilio does, so none of the
    # onboarding/approvals/cooling-off business logic changed.
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_use_fake: bool = False

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
