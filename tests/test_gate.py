"""Acceptance (CLAUDE.md Phase 5): one test per rule firing in isolation;
an ordering test proving R04 precedes R08; a fail-closed test injecting an
exception mid-chain.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from praman.core.envelope import Cart, CartItem
from praman.core.gate import (
    GateRequest,
    deserialize_gate_request,
    run_gate,
    serialize_gate_request,
)
from praman.core.quotes import QuoteData, issue_quote
from praman.core.registry import LocalRegistry
from praman.crypto.keys import generate_keypair, sign
from praman.models import Agent, GateDecision, IntentEnvelope, LedgerEvent, Merchant, Product
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@dataclass
class Scenario:
    merchant: Merchant
    merchant_priv: str
    agent: Agent
    agent_priv: str
    product: Product
    envelope: IntentEnvelope
    quote: QuoteData


def _sign_request(
    private_key_hex: str, method: str, body: bytes, timestamp: str, nonce: str
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()
    return sign(private_key_hex, message)


async def _build_scenario(
    session: AsyncSession,
    redis: fakeredis.aioredis.FakeRedis,
    *,
    agent_max_txn_paise: int = 1_000_000,
    agent_daily_cap_paise: int = 10_000_000,
    agent_revoked: bool = False,
    envelope_allowed_categories: list[str] | None = None,
    envelope_max_single_txn_paise: int = 1_000_000,
    envelope_ceiling_paise: int = 1_000_000,
    envelope_spent_paise: int = 0,
    envelope_min_reversibility: float = 0.0,
    envelope_revoked: bool = False,
    envelope_valid_until: datetime | None = None,
    product_unit_price_paise: int = 1000,
    product_stock: int | None = 100,
    product_category_class: str = "consumable",
    product_is_personalised: bool = False,
    quote_qty: int = 1,
) -> Scenario:
    merchant_priv, merchant_pub = generate_keypair()
    merchant = Merchant(
        name="Test Merchant",
        did="did:key:zMerchantTest",
        public_key=merchant_pub,
        private_key_enc="unused-in-tests",
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=NOW,
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)

    agent_priv, agent_pub = generate_keypair()
    agent = Agent(
        agent_did=f"did:key:zAgent{uuid.uuid4().hex[:8]}",
        operator="Test Operator",
        public_key=agent_pub,
        trust_tier="standard",
        max_txn_paise=agent_max_txn_paise,
        daily_cap_paise=agent_daily_cap_paise,
        registered_at=NOW - timedelta(days=1),
        revoked_at=NOW - timedelta(hours=1) if agent_revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    product = Product(
        merchant_id=merchant.id,
        sku="test-sku",
        name="Test Product",
        category="pulses",
        category_class=product_category_class,
        unit_price_paise=product_unit_price_paise,
        stock=product_stock,
        return_window_days=14,
        fulfilment_hours=24,
        restocking_cost_pct=0.0,
        is_personalised=product_is_personalised,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)

    envelope = IntentEnvelope(
        user_ref="user-1",
        user_whatsapp="whatsapp:+919999999999",
        merchant_id=merchant.id,
        agent_did=agent.agent_did,
        ceiling_paise=envelope_ceiling_paise,
        spent_paise=envelope_spent_paise,
        max_single_txn_paise=envelope_max_single_txn_paise,
        allowed_categories=envelope_allowed_categories or ["pulses"],
        min_reversibility=envelope_min_reversibility,
        valid_from=NOW - timedelta(hours=1),
        valid_until=envelope_valid_until or (NOW + timedelta(hours=1)),
        revoked_at=NOW - timedelta(minutes=1) if envelope_revoked else None,
        signature="unused-in-tests",
    )
    session.add(envelope)
    await session.commit()
    await session.refresh(envelope)

    quote = await issue_quote(
        redis,
        product_id=product.id,
        sku=product.sku,
        category_class=product_category_class,
        unit_price_paise=product_unit_price_paise,
        qty=quote_qty,
        agent_did=agent.agent_did,
        merchant_did=merchant.did,
        merchant_private_key_hex=merchant_priv,
        now=NOW,
    )

    return Scenario(merchant, merchant_priv, agent, agent_priv, product, envelope, quote)


def _cart(scenario: Scenario, *, category: str = "pulses", qty: int = 1) -> Cart:
    return Cart(
        agent_did=scenario.agent.agent_did,
        items=(
            CartItem(
                sku=scenario.product.sku,
                category=category,
                qty=qty,
                unit_price_paise=scenario.quote.unit_price_paise,
            ),
        ),
    )


def _reversibility_items(scenario: Scenario, **overrides: object) -> tuple:
    from praman.core.reversibility import ReversibilityItem

    defaults: dict[str, object] = {
        "category_class": scenario.product.category_class,
        "is_personalised": scenario.product.is_personalised,
        "return_window_days": scenario.product.return_window_days,
        "fulfilment_hours": scenario.product.fulfilment_hours,
        "restocking_cost_pct": scenario.product.restocking_cost_pct,
    }
    defaults.update(overrides)
    return (ReversibilityItem(**defaults),)  # type: ignore[arg-type]


def _request(
    scenario: Scenario,
    session: AsyncSession,
    *,
    cart: Cart | None = None,
    reversibility_items: tuple | None = None,
    quotes: tuple[QuoteData, ...] | None = None,
    envelope_id: str | None = None,
    signature_override: str | None = None,
    now: datetime = NOW,
    body: bytes = b"{}",
) -> GateRequest:
    nonce = uuid.uuid4().hex
    timestamp = NOW.isoformat()
    signature = signature_override or _sign_request(
        scenario.agent_priv, "POST", body, timestamp, nonce
    )
    return GateRequest(
        session_id=f"session-{uuid.uuid4().hex[:8]}",
        cart_id=None,
        agent_did=scenario.agent.agent_did,
        method="POST",
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        envelope_id=envelope_id or scenario.envelope.envelope_id,
        cart=cart or _cart(scenario),
        reversibility_items=reversibility_items or _reversibility_items(scenario),
        quotes=quotes or (scenario.quote,),
        idempotency_key=uuid.uuid4().hex,
        now=now,
    )


# --- Baseline: everything passes ---


async def test_all_rules_pass_results_in_allow(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "ALLOW"
    assert result.reason_code == "OK"


async def test_allow_decision_persists_gate_decision_row(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    await run_gate(db_session, redis, registry, req)

    result = await db_session.execute(
        select(GateDecision).where(GateDecision.session_id == req.session_id)
    )
    rows = list(result.scalars().all())
    assert len(rows) == 1
    assert rows[0].decision == "ALLOW"
    assert rows[0].latency_ms >= 0


async def test_allow_decision_emits_ledger_event(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    await run_gate(db_session, redis, registry, req)

    result = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.session_id == req.session_id)
    )
    events = list(result.scalars().all())
    assert len(events) == 1
    assert events[0].event_type == "GATE_DECISION"
    assert events[0].payload_json["decision"] == "ALLOW"


# --- R01: signature ---


async def test_r01_fires_on_invalid_signature(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session, signature_override="00" * 64)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_SIG_INVALID"
    assert result.rule_id == "R01"


# --- R02: registered & not revoked ---


async def test_r02_fires_on_revoked_agent(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, agent_revoked=True)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_REVOKED"
    assert result.rule_id == "R02"


# --- R03: envelope exists ---


async def test_r03_fires_on_unknown_envelope(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session, envelope_id="does-not-exist")

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_INVALID"
    assert result.rule_id == "R03"


# --- R04: verify_cart_within_envelope ---


async def test_r04_fires_on_category_denied(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, envelope_allowed_categories=["spices"])
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "CATEGORY_DENIED"
    assert result.rule_id == "R04"


# --- R05 / R06 / R07: quote checks ---


async def test_r05_fires_on_expired_quote(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    # Issue a genuinely expired quote (signed with an early `now`, TTL
    # already elapsed) rather than tampering `expires_at` post-hoc — that
    # would just invalidate the signature and fire QUOTE_SIG_INVALID
    # instead, since signature is checked before expiry in verify_quote.
    expired_quote = await issue_quote(
        redis,
        product_id=scenario.product.id,
        sku=scenario.product.sku,
        category_class=scenario.product.category_class,
        unit_price_paise=scenario.quote.unit_price_paise,
        qty=scenario.quote.qty,
        agent_did=scenario.agent.agent_did,
        merchant_did=scenario.merchant.did,
        merchant_private_key_hex=scenario.merchant_priv,
        now=NOW - timedelta(minutes=5),
    )
    req = _request(scenario, db_session, quotes=(expired_quote,))

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "QUOTE_EXPIRED"
    assert result.rule_id == "R05"


async def test_r06_fires_on_price_drift(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, product_unit_price_paise=1000)
    scenario.product.unit_price_paise = 1500  # live price now differs from the quote
    db_session.add(scenario.product)
    await db_session.commit()
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "PRICE_DRIFT"
    assert result.rule_id == "R06"


async def test_r07_fires_out_of_stock_and_substitutes(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, product_stock=10, quote_qty=5)
    scenario.product.stock = 1  # live stock dropped below the quoted qty
    db_session.add(scenario.product)
    await db_session.commit()
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session, cart=_cart(scenario, qty=5))

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "SUBSTITUTE"
    assert result.reason_code == "OUT_OF_STOCK"
    assert result.rule_id == "R07"


# --- R08: reversibility vs envelope minimum ---


async def test_r08_fires_step_up_required(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, envelope_min_reversibility=0.99)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "ESCALATE"
    assert result.reason_code == "STEP_UP_REQUIRED"
    assert result.rule_id == "R08"


async def test_r08_human_present_bypasses_step_up(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    # Simulates the gate re-run after a merchant's WhatsApp Approve.
    scenario = await _build_scenario(db_session, redis, envelope_min_reversibility=0.99)
    registry = LocalRegistry(db_session)
    req = replace(_request(scenario, db_session), human_present=True)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "ALLOW"


async def test_human_present_retry_reuses_the_original_nonce_without_replay_block(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    # First pass escalates (consuming the nonce). The merchant-approval
    # retry reuses that SAME signed request (the server can't re-sign as
    # the agent) — it must not trip R01's nonce-replay check.
    scenario = await _build_scenario(db_session, redis, envelope_min_reversibility=0.99)
    registry = LocalRegistry(db_session)
    original_req = _request(scenario, db_session)

    first = await run_gate(db_session, redis, registry, original_req)
    assert first.decision == "ESCALATE"

    retry_req = replace(original_req, human_present=True)
    second = await run_gate(db_session, redis, registry, retry_req)

    assert second.decision == "ALLOW"


async def test_gate_request_serialization_round_trips_and_re_runs(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    # Simulates persisting a GateRequest to Order.pending_gate_request and
    # rehydrating it later — the actual whatsapp/approvals.py flow.
    scenario = await _build_scenario(db_session, redis, envelope_min_reversibility=0.99)
    registry = LocalRegistry(db_session)
    original_req = _request(scenario, db_session)

    first = await run_gate(db_session, redis, registry, original_req)
    assert first.decision == "ESCALATE"

    serialized = serialize_gate_request(original_req)
    # round-trip through JSON, exactly as a JSON DB column would
    import json

    round_tripped = json.loads(json.dumps(serialized))
    rehydrated = deserialize_gate_request(round_tripped, now=NOW)

    assert rehydrated.human_present is True
    assert rehydrated.agent_did == original_req.agent_did
    assert rehydrated.cart.items == original_req.cart.items
    assert rehydrated.quotes[0].quote_id == original_req.quotes[0].quote_id

    second = await run_gate(db_session, redis, registry, rehydrated)
    assert second.decision == "ALLOW"


# --- R09: amber band -> cooling off hold ---


async def test_r09_fires_cooling_off_hold_for_amber_band(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, product_category_class="durable")
    registry = LocalRegistry(db_session)
    amber_items = _reversibility_items(
        scenario,
        category_class="durable",
        return_window_days=10,
        fulfilment_hours=48,
        restocking_cost_pct=0.10,
    )
    req = _request(scenario, db_session, reversibility_items=amber_items)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "HOLD"
    assert result.reason_code == "COOLING_OFF_OPEN"
    assert result.rule_id == "R09"


# --- R10: velocity ---


async def test_r10_fires_velocity_exceeded(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    from praman.config import VELOCITY_MAX_TRANSACTIONS_DEMO_MODE

    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)

    last_result = None
    for _ in range(VELOCITY_MAX_TRANSACTIONS_DEMO_MODE + 1):
        req = _request(scenario, db_session)
        last_result = await run_gate(db_session, redis, registry, req, demo_mode=True)

    assert last_result is not None
    assert last_result.decision == "BLOCK"
    assert last_result.reason_code == "VELOCITY_EXCEEDED"
    assert last_result.rule_id == "R10"


# --- R11: trust tier + daily cap ---


async def test_r11_fires_tier_ceiling_on_single_txn(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, agent_max_txn_paise=500)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)  # cart total is 1000, exceeds tier max of 500

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "ESCALATE"
    assert result.reason_code == "TIER_CEILING"
    assert result.rule_id == "R11"


async def test_r11_fires_tier_ceiling_on_daily_cap(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, agent_daily_cap_paise=500)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)  # cart total is 1000, exceeds daily cap of 500

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "ESCALATE"
    assert result.reason_code == "TIER_CEILING"
    assert result.rule_id == "R11"


# --- R12: idempotency ---


async def test_r12_fires_duplicate_attempt(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    idempotency_key = uuid.uuid4().hex

    req1 = _request(scenario, db_session)
    req1 = replace(req1, idempotency_key=idempotency_key)
    first = await run_gate(db_session, redis, registry, req1)
    assert first.decision == "ALLOW"

    req2 = _request(scenario, db_session)
    req2 = replace(req2, idempotency_key=idempotency_key)
    second = await run_gate(db_session, redis, registry, req2)

    assert second.decision == "BLOCK"
    assert second.reason_code == "DUPLICATE_ATTEMPT"
    assert second.rule_id == "R12"


# --- Ordering ---


async def test_ordering_r04_precedes_r08(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    # Both R04 (category denied) AND R08 (reversibility below minimum)
    # would fire on this request — R04 must win since it's checked first.
    scenario = await _build_scenario(
        db_session,
        redis,
        envelope_allowed_categories=["spices"],  # will fail R04
        envelope_min_reversibility=0.99,  # would also fail R08
    )
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.reason_code == "CATEGORY_DENIED"
    assert result.rule_id == "R04"


async def test_ordering_r01_precedes_r03(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    # Both R01 (bad signature) and R03 (unknown envelope) would fire —
    # R01 must win since it's checked first.
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session, envelope_id="does-not-exist", signature_override="00" * 64)

    result = await run_gate(db_session, redis, registry, req)

    assert result.reason_code == "AGENT_SIG_INVALID"
    assert result.rule_id == "R01"


# --- Fail-closed ---


def _raise_simulated_failure(*args: object, **kwargs: object) -> None:
    raise RuntimeError("simulated failure mid-chain")


async def test_fail_closed_on_unhandled_exception_mid_chain(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    import praman.core.gate as gate_module

    # R04 (verify_cart_within_envelope) would normally ALLOW this request —
    # forcing it to raise proves the failure is caught and reported as
    # BLOCK, not propagated or silently treated as ALLOW.
    monkeypatch.setattr(gate_module, "verify_cart_within_envelope", _raise_simulated_failure)

    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    result = await run_gate(db_session, redis, registry, req)

    assert result.decision == "BLOCK"
    assert result.reason_code == "INTERNAL_ERROR"


async def test_fail_closed_decision_still_persists_with_ledger_event(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    import praman.core.gate as gate_module

    monkeypatch.setattr(gate_module, "verify_cart_within_envelope", _raise_simulated_failure)

    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    req = _request(scenario, db_session)

    await run_gate(db_session, redis, registry, req)

    decision_result = await db_session.execute(
        select(GateDecision).where(GateDecision.session_id == req.session_id)
    )
    decisions = list(decision_result.scalars().all())
    assert len(decisions) == 1
    assert decisions[0].decision == "BLOCK"
    assert decisions[0].reason_code == "INTERNAL_ERROR"

    ledger_result = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.session_id == req.session_id)
    )
    events = list(ledger_result.scalars().all())
    assert len(events) == 1
    assert events[0].payload_json["decision"] == "BLOCK"
    assert events[0].payload_json["reason_code"] == "INTERNAL_ERROR"
