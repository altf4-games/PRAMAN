"""SQLAlchemy models — the full data model from CLAUDE.md §3.

Ids are UUID4 hex strings (portable across Postgres and the sqlite used in
tests). Money is always `int` paise. Timestamps are timezone-aware UTC.
JSON columns use the portable `JSON` type (works on both Postgres and
sqlite) rather than Postgres-specific JSONB, since tests run on sqlite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from praman.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    did: Mapped[str] = mapped_column(String(200), unique=True)
    public_key: Mapped[str] = mapped_column(Text)
    private_key_enc: Mapped[str] = mapped_column(Text)
    whatsapp_number: Mapped[str] = mapped_column(String(32), unique=True)
    onboarding_state: Mapped[str] = mapped_column(String(32), default="NEW")
    agent_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    products: Mapped[list[Product]] = relationship(back_populates="merchant")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    agent_did: Mapped[str] = mapped_column(String(200), unique=True)
    operator: Mapped[str] = mapped_column(String(200))
    public_key: Mapped[str] = mapped_column(Text)
    trust_tier: Mapped[str] = mapped_column(String(32), default="standard")
    max_txn_paise: Mapped[int] = mapped_column(BigInteger)
    daily_cap_paise: Mapped[int] = mapped_column(BigInteger)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100))
    category_class: Mapped[str] = mapped_column(String(32))
    unit_price_paise: Mapped[int] = mapped_column(BigInteger)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    return_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fulfilment_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restocking_cost_pct: Mapped[float] = mapped_column(Float, default=0.0)
    is_personalised: Mapped[bool] = mapped_column(Boolean, default=False)
    field_confidence: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32))
    source_media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    merchant: Mapped[Merchant] = relationship(back_populates="products")


class Quote(Base):
    __tablename__ = "quotes"

    quote_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    agent_did: Mapped[str] = mapped_column(String(200))
    unit_price_paise: Mapped[int] = mapped_column(BigInteger)
    qty: Mapped[int] = mapped_column(Integer)
    total_paise: Mapped[int] = mapped_column(BigInteger)
    stock_held: Mapped[bool] = mapped_column(Boolean, default=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signature: Mapped[str] = mapped_column(Text)


class IntentEnvelope(Base):
    __tablename__ = "intent_envelopes"

    envelope_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_ref: Mapped[str] = mapped_column(String(200))
    user_whatsapp: Mapped[str] = mapped_column(String(32))
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    agent_did: Mapped[str] = mapped_column(String(200))
    ceiling_paise: Mapped[int] = mapped_column(BigInteger)
    spent_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    max_single_txn_paise: Mapped[int] = mapped_column(BigInteger)
    allowed_categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    min_reversibility: Mapped[float] = mapped_column(Float, default=0.0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signature: Mapped[str] = mapped_column(Text)


class CartMandate(Base):
    __tablename__ = "cart_mandates"

    cart_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("intent_envelopes.envelope_id"))
    agent_did: Mapped[str] = mapped_column(String(200))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    subtotal_paise: Mapped[int] = mapped_column(BigInteger)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    total_paise: Mapped[int] = mapped_column(BigInteger)
    reversibility_score: Mapped[float] = mapped_column(Float)
    reversibility_breakdown: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    band: Mapped[str] = mapped_column(String(16))
    merchant_sig: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_sig: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GateDecision(Base):
    __tablename__ = "gate_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(64))
    cart_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str] = mapped_column(String(64))
    rule_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    detail: Mapped[str] = mapped_column(Text)
    remedy: Mapped[str] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[float] = mapped_column(Float)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    cart_id: Mapped[str] = mapped_column(ForeignKey("cart_mandates.cart_id"))
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    cooling_off_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stepup_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stepup_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stepup_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The exact signed agent request that produced this order, serialized
    # (see core/gate.py's serialize_gate_request/deserialize_gate_request).
    # Only ever set for an ESCALATE (pending_approval) order — it's what
    # lets a merchant's WhatsApp Approve re-run the gate from R01 against
    # the agent's *original* signature, since the server can never re-sign
    # as the agent itself.
    pending_gate_request: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(8))
    wa_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    media_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LedgerEvent(Base):
    """The hash-chained ledger. `chain_hash = sha256(prev_chain_hash || payload_hash)`,
    genesis `prev_hash = '0' * 64`. See `core/ledger.py`."""

    __tablename__ = "ledger_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_did: Mapped[str | None] = mapped_column(String(200), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    chain_hash: Mapped[str] = mapped_column(String(64))
