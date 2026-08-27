"""Pydantic request/response schemas for the REST surface. Populated
incrementally as each phase's routes are built.

Note on signed requests (`quote_request`, `cart_confirm`, `checkout_execute`,
`substitution_accept`): the agent's Ed25519 signature covers
`(method, sha256(raw_request_body), timestamp, nonce)` — see
`core/registry.py::verify_agent_request`. Deliberately NOT modeled as
fields on these bodies: a signature that covers "the request body" can't
also live *inside* that same body (verifying it would require rehashing a
body containing the very hash you're trying to check — self-referential).
Instead `timestamp`/`nonce`/`signature` travel as the
`X-Praman-Timestamp` / `X-Praman-Nonce` / `X-Praman-Signature` headers,
and the raw bytes each route hashes (`await request.body()`) are exactly
the JSON payload below, nothing more — the same separation Twilio and
Razorpay's own webhook signing use (signature in a header, hash over the
untouched body).
"""

from __future__ import annotations

from pydantic import BaseModel


class ChainVerifyResponse(BaseModel):
    ok: bool
    first_bad_index: int | None
    expected: str | None
    actual: str | None
    checked: int


# --- catalog / policy (read-only) ---


class ProductOut(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    category_class: str
    unit_price_paise: int
    stock: int | None
    return_window_days: int | None
    fulfilment_hours: int | None
    is_personalised: bool


class PolicyOut(BaseModel):
    merchant_id: str
    name: str
    did: str
    max_txn_paise: int | None
    cooling_off_hold: bool | None


# --- agent registration (pragmatic addition — not in the spec's MCP table,
# but something has to create the `agents` rows a real deployment's agents
# would arrive with pre-registered; kept REST-only, not wrapped by MCP) ---


class AgentRegisterIn(BaseModel):
    operator: str
    trust_tier: str = "standard"
    max_txn_paise: int
    daily_cap_paise: int
    public_key: str | None = None  # omit to have the server generate a demo keypair


class AgentRegisterOut(BaseModel):
    agent_did: str
    public_key: str
    private_key: str | None  # only set when the server generated the keypair


# --- envelope ---


class EnvelopeSubmitIn(BaseModel):
    merchant_id: str
    agent_did: str
    user_ref: str
    user_whatsapp: str
    ceiling_paise: int
    max_single_txn_paise: int
    allowed_categories: list[str]
    min_reversibility: float = 0.0
    valid_hours: int = 24


class EnvelopeOut(BaseModel):
    envelope_id: str
    merchant_id: str
    agent_did: str
    ceiling_paise: int
    spent_paise: int
    max_single_txn_paise: int
    allowed_categories: list[str]
    min_reversibility: float
    valid_from: str
    valid_until: str
    signature: str


# --- quotes ---


class QuoteRequestIn(BaseModel):
    product_id: str
    agent_did: str
    qty: int


class QuoteOut(BaseModel):
    quote_id: str
    product_id: str
    sku: str
    category: str
    agent_did: str
    merchant_did: str
    unit_price_paise: int
    qty: int
    total_paise: int
    stock_held: bool
    issued_at: str
    expires_at: str
    nonce: str
    signature: str


class QuoteIn(BaseModel):
    quote_id: str
    product_id: str
    sku: str
    agent_did: str
    merchant_did: str
    unit_price_paise: int
    qty: int
    total_paise: int
    stock_held: bool
    issued_at: str
    expires_at: str
    nonce: str
    signature: str


# --- cart ---


class CartConfirmIn(BaseModel):
    envelope_id: str
    agent_did: str
    quotes: list[QuoteIn]


class CartConfirmOut(BaseModel):
    cart_id: str
    subtotal_paise: int
    total_paise: int
    reversibility_score: float
    reversibility_breakdown: dict[str, float]
    band: str
    envelope_check_decision: str
    envelope_check_reason_code: str


# --- checkout ---


class CheckoutExecuteIn(BaseModel):
    cart_id: str
    agent_did: str
    quotes: list[QuoteIn]


class SubstitutionCandidateOut(BaseModel):
    product_id: str
    sku: str
    name: str
    unit_price_paise: int


class CheckoutExecuteOut(BaseModel):
    decision: str
    reason_code: str
    detail: str
    remedy: str
    order_id: str | None
    order_status: str | None
    substitution_offer: list[SubstitutionCandidateOut]
    substitution_rationale: str | None


class SubstitutionAcceptIn(BaseModel):
    cart_id: str
    agent_did: str
    accepted_product_id: str


class OrderOut(BaseModel):
    id: str
    cart_id: str
    status: str
    razorpay_order_id: str | None
    cooling_off_until: str | None
    dispatched_at: str | None
    cancelled_at: str | None
    refunded_at: str | None


class OrderUndoIn(BaseModel):
    user_ref: str


class OrderUndoOut(BaseModel):
    cancelled: bool
