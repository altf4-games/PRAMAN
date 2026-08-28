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
    # Populated only when `status` is "awaiting_payment"/"awaiting_payment_amber"
    # — everything the frontend needs to open Razorpay's real Checkout.js
    # widget without a second round-trip. `razorpay_key_id` is the
    # publishable key id, safe to expose client-side (it's what
    # Checkout.js itself requires).
    amount_paise: int | None = None
    razorpay_key_id: str | None = None


class CheckoutConfirmIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class CheckoutConfirmOut(BaseModel):
    order_id: str
    status: str
    dispatched_at: str | None
    cooling_off_until: str | None


class OrderUndoIn(BaseModel):
    user_ref: str


class OrderUndoOut(BaseModel):
    cancelled: bool


# --- merchants (frontend plumbing — the /onboard and /live pages need a
# merchant picker; not one of the design spec's ten MCP tools) ---


class MerchantOut(BaseModel):
    id: str
    name: str
    did: str
    whatsapp_number: str
    onboarding_state: str
    agent_policy: dict[str, object]


# --- review queue (frontend plumbing for /catalog) ---


class ReviewProductOut(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    unit_price_paise: int
    field_confidence: dict[str, float]
    source: str
    source_media_url: str | None


# --- approvals inbox (frontend plumbing for /approvals, mirroring the
# WhatsApp inbox `whatsapp/approvals.py` already drives) ---


class ApprovalOut(BaseModel):
    order_id: str
    cart_id: str
    merchant_id: str
    item_summary: str
    total_paise: int
    reason_code: str
    reversibility_score: float
    reversibility_breakdown: dict[str, float]
    band: str
    created_at: str
    deadline: str


class ApprovalDecideIn(BaseModel):
    decision: str  # "approve" | "decline"


class ApprovalDecideOut(BaseModel):
    decision: str
    order_status: str


# --- dispute pack (Phase 8 preview — pulled forward because /dispute/[id]
# needs real content to render, not a stub) ---


class DisputePackOut(BaseModel):
    cart_id: str
    envelope: dict[str, object]
    cart_mandate: dict[str, object]
    order: dict[str, object] | None
    gate_trail: list[dict[str, object]]
    quote_provenance: list[dict[str, object]]
    reversibility_breakdown: dict[str, float]
    band: str
    ledger: dict[str, object]
    merchant_did: str | None
    pack_hash: str
    merchant_signature: str | None


# --- metrics (frontend plumbing for `/` 's live counter and `/metrics`) ---


class MetricsOut(BaseModel):
    sessions_gated: int
    orders_by_status: dict[str, int]
    orders_by_band: dict[str, int]
    disputes_resolvable: int
    # Cumulative count of every R08/R11 ESCALATE decision this gate has
    # ever made, distinct from `orders_by_status["pending_approval"]`
    # (a live snapshot of what's *currently* awaiting a merchant, which
    # reads as zero the moment every past escalation has been resolved —
    # a real clarity gap found live: a resolved escalation reading as
    # "escalation never happened" to a first-time viewer).
    escalations_ever: int
