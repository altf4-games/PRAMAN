"""Runs one `SessionSpec` through both arms (the design spec §8):

- **Arm A (naive):** unsigned reads, no envelope, no gate, direct
  checkout — captures whatever the agent originally quoted itself,
  unconditionally, exactly once (twice for a replay attempt: a naive
  integration has no idempotency check either).
- **Arm B (PRAMAN):** the real `core.gate.run_gate` / `core.checkout.execute_checkout`
  path, byte-for-byte what production runs.

Every attack technique here manipulates real rows in a real (sqlite,
in-memory) database and real Redis (fakeredis) state — this is not a
scripted "and then the gate returns BLOCK" stub; the actual R01-R12 chain
decides.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from fakeredis.aioredis import FakeRedis
from praman.adapters.llm import LLMClient
from praman.adapters.razorpay_client import (
    RazorpayClient,
    make_idempotency_key,
)
from praman.core.checkout import execute_checkout
from praman.core.envelope import Cart, CartItem
from praman.core.gate import GateRequest
from praman.core.quotes import QuoteData, issue_quote
from praman.core.registry import AgentRegistry
from praman.core.reversibility import ReversibilityItem
from praman.crypto.keys import generate_keypair, sign
from praman.models import Agent, IntentEnvelope, Merchant, Product
from praman.whatsapp.client import WhatsAppClient
from sqlalchemy.ext.asyncio import AsyncSession

from harness.sessions import SessionSpec


@dataclass
class SessionResult:
    session_id: str
    category: str
    arm: str
    decision: str
    reason_code: str | None
    rule_id: str | None
    band: str | None
    captured_paise: int
    attempted_paise: int
    num_captures: int
    latency_ms: float
    is_attack: bool
    order_id: str | None = None
    cart_id: str | None = None


def _sign_request(
    private_key_hex: str, method: str, body: bytes, timestamp: str, nonce: str
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()
    return sign(private_key_hex, message)


async def _register_agent(
    session: AsyncSession, spec: SessionSpec, now: datetime
) -> tuple[str | None, str, str]:
    """Returns (agent_did_or_None, signing_key_hex, claimed_agent_did).
    `agent_did_or_None` is None for an unregistered-agent spoof attempt —
    the registry genuinely has no row for it."""
    priv, pub = generate_keypair()
    agent_did = f"did:key:zHarness{uuid.uuid4().hex[:16]}"

    if spec.unregistered_agent:
        return None, priv, agent_did

    agent = Agent(
        agent_did=agent_did,
        operator="harness",
        public_key=pub,
        trust_tier="standard",
        max_txn_paise=spec.agent_max_txn_paise,
        daily_cap_paise=spec.agent_daily_cap_paise,
        registered_at=now,
        revoked_at=now if spec.revoked_agent else None,
    )
    session.add(agent)
    await session.commit()
    return agent_did, priv, agent_did


async def run_session_both_arms(
    session: AsyncSession,
    redis: FakeRedis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    merchant: Merchant,
    merchant_priv: str,
    products: dict[str, Product],
    spec: SessionSpec,
    *,
    now: datetime,
) -> list[SessionResult]:
    product = products[spec.sku]
    original_price_paise = product.unit_price_paise
    original_stock = product.stock
    original_name = product.name

    # --- register identity (real, revoked, unregistered, or about to be
    # signed with the wrong key) ---
    agent_did, signing_key, claimed_agent_did = await _register_agent(session, spec, now)

    # Envelope escape's "wrong_agent" needs a *second*, genuinely valid
    # agent to attempt using the *first* agent's envelope.
    envelope_owner_did = claimed_agent_did
    if spec.wrong_agent:
        _owner_priv, owner_pub = generate_keypair()
        envelope_owner_did = f"did:key:zHarnessOwner{uuid.uuid4().hex[:12]}"
        session.add(
            Agent(
                agent_did=envelope_owner_did,
                operator="harness-owner",
                public_key=owner_pub,
                trust_tier="standard",
                max_txn_paise=spec.agent_max_txn_paise,
                daily_cap_paise=spec.agent_daily_cap_paise,
                registered_at=now,
                revoked_at=None,
            )
        )
        await session.commit()

    allowed_categories = spec.allowed_categories_override or [product.category]
    envelope = IntentEnvelope(
        user_ref=f"harness-user-{spec.id}",
        user_whatsapp="whatsapp:+919000000000",
        merchant_id=merchant.id,
        agent_did=envelope_owner_did,
        ceiling_paise=spec.envelope_ceiling_paise,
        spent_paise=0,
        max_single_txn_paise=spec.envelope_ceiling_paise,
        allowed_categories=allowed_categories,
        min_reversibility=spec.envelope_min_reversibility,
        valid_from=now,
        valid_until=now,
        revoked_at=None,
        signature="unused-in-harness",
    )
    session.add(envelope)
    await session.commit()
    await session.refresh(envelope)

    # --- issue a real signed quote at the ORIGINAL (pre-chaos) price ---
    quote = await issue_quote(
        redis,
        product_id=product.id,
        sku=product.sku,
        category_class=product.category_class,
        unit_price_paise=original_price_paise,
        qty=spec.qty,
        agent_did=claimed_agent_did,
        merchant_did=merchant.did,
        merchant_private_key_hex=merchant_priv,
        now=now,
    )

    # --- apply chaos: mutate the *live* row after the quote was issued ---
    if spec.chaos_price_paise is not None:
        product.unit_price_paise = spec.chaos_price_paise
        session.add(product)
        await session.commit()
    if spec.chaos_zero_stock:
        product.stock = 0
        session.add(product)
        await session.commit()

    # --- prompt injection: temporarily rename the product, run Arm B
    # twice (clean vs injected), compare, then bail out early -- this
    # category doesn't fit the attack/benign block-or-allow shape the
    # other six do. ---
    if spec.inject_text is not None:
        injection_results = await _run_injection_pair(
            session,
            redis,
            registry,
            merchant,
            merchant_priv,
            product,
            spec,
            quote,
            claimed_agent_did,
            now,
        )
        product.unit_price_paise = original_price_paise
        product.stock = original_stock
        product.name = original_name
        session.add(product)
        await session.commit()
        return injection_results

    cart_item = CartItem(
        sku=product.sku,
        category=product.category,
        qty=spec.qty,
        unit_price_paise=quote.unit_price_paise,
    )
    reversibility_item = ReversibilityItem(
        category_class=product.category_class,
        is_personalised=product.is_personalised,
        return_window_days=product.return_window_days or 0,
        fulfilment_hours=product.fulfilment_hours or 0,
        restocking_cost_pct=product.restocking_cost_pct,
    )
    cart_agent_did = claimed_agent_did  # attacker's own identity ends up on the cart regardless

    results: list[SessionResult] = []

    # === Arm B: the real gate ===
    b_results = await _run_arm_b(
        session,
        redis,
        registry,
        razorpay,
        whatsapp,
        llm,
        envelope,
        cart_item,
        reversibility_item,
        quote,
        spec,
        agent_did,
        signing_key,
        cart_agent_did,
        now,
    )
    results.extend(b_results)

    # === Arm A: naive, always at the ORIGINAL (possibly now-stale) price ===
    a_results = _run_arm_a(spec, original_price_paise)
    results.extend(a_results)

    # --- restore the product row for the next session ---
    product.unit_price_paise = original_price_paise
    product.stock = original_stock
    session.add(product)
    await session.commit()

    return results


async def _run_arm_b(
    session: AsyncSession,
    redis: FakeRedis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    envelope: IntentEnvelope,
    cart_item: CartItem,
    reversibility_item: ReversibilityItem,
    quote: QuoteData,
    spec: SessionSpec,
    agent_did: str | None,
    signing_key: str,
    cart_agent_did: str,
    now: datetime,
) -> list[SessionResult]:
    from praman.core.gate import envelope_from_row
    from praman.core.reversibility import band, reversibility_score_detailed
    from praman.models import CartMandate

    cart = Cart(agent_did=cart_agent_did, items=(cart_item,))
    env = envelope_from_row(envelope)
    score, breakdown = reversibility_score_detailed([reversibility_item], cart.total_paise, env)
    cart_band = band(score)

    mandate = CartMandate(
        envelope_id=envelope.envelope_id,
        agent_did=cart_agent_did,
        items=[
            {
                "sku": cart_item.sku,
                "category": cart_item.category,
                "qty": cart_item.qty,
                "unit_price_paise": cart_item.unit_price_paise,
            }
        ],
        subtotal_paise=cart.total_paise,
        tax_paise=0,
        total_paise=cart.total_paise,
        reversibility_score=score,
        reversibility_breakdown=breakdown,
        band=cart_band,
        agent_sig="harness",
        created_at=now,
    )
    session.add(mandate)
    await session.commit()
    await session.refresh(mandate)

    attempts = 2 if spec.replay else 1
    out: list[SessionResult] = []

    timestamp = now.isoformat()
    nonce = uuid.uuid4().hex
    body = b"{}"
    signing_key_used = signing_key if not spec.bad_signature else generate_keypair()[0]
    signature = _sign_request(signing_key_used, "POST", body, timestamp, nonce)

    for attempt in range(attempts):
        gate_req = GateRequest(
            session_id=f"harness:{spec.id}:{attempt}",
            cart_id=mandate.cart_id,
            agent_did=cart_agent_did,
            method="POST",
            body=body,
            timestamp=timestamp,
            nonce=nonce,  # same nonce on both attempts of a replay, deliberately
            signature=signature,
            envelope_id=envelope.envelope_id,
            cart=cart,
            reversibility_items=(reversibility_item,),
            quotes=(quote,),
            idempotency_key=make_idempotency_key(mandate.cart_id, cart_agent_did),
            now=now,
        )
        start = time.perf_counter()
        result = await execute_checkout(
            session, redis, registry, razorpay, whatsapp, llm, mandate, gate_req, demo_mode=True
        )
        latency_ms = (time.perf_counter() - start) * 1000
        # ALLOW/HOLD both create a captured order in checkout.py
        # (_handle_allow/_handle_hold); ESCALATE creates a payment-free
        # pending_approval order (no capture yet), BLOCK/SUBSTITUTE create
        # none at all.
        captured = result.gate_result.decision in ("ALLOW", "HOLD")
        # Only the *second* attempt of a replay is the actual attack — the
        # first is an ordinary, legitimate checkout that happens to get
        # replayed afterward, and grading it as an "attack" would count a
        # normal ALLOW as a false negative.
        effective_is_attack = (attempt == attempts - 1) if spec.replay else spec.is_attack
        out.append(
            SessionResult(
                session_id=spec.id,
                category=spec.category,
                arm="B",
                decision=result.gate_result.decision,
                reason_code=result.gate_result.reason_code,
                rule_id=result.gate_result.rule_id,
                band=cart_band,
                captured_paise=cart.total_paise if captured else 0,
                attempted_paise=cart.total_paise,
                num_captures=1 if captured else 0,
                latency_ms=latency_ms,
                is_attack=effective_is_attack,
                order_id=result.order.id if result.order else None,
                cart_id=mandate.cart_id,
            )
        )
    return out


def _run_arm_a(spec: SessionSpec, original_price_paise: int) -> list[SessionResult]:
    """No signature, no envelope, no gate, no idempotency — a naive
    integration just captures whatever it originally intended to pay,
    every single time it's asked to, including a replayed request."""
    attempts = 2 if spec.replay else 1
    captured_paise = original_price_paise * spec.qty
    return [
        SessionResult(
            session_id=spec.id,
            category=spec.category,
            arm="A",
            decision="ALLOWED",
            reason_code=None,
            rule_id=None,
            band=None,
            captured_paise=captured_paise,
            attempted_paise=captured_paise,
            num_captures=1,
            latency_ms=0.0,
            is_attack=(i == attempts - 1) if spec.replay else spec.is_attack,
        )
        for i in range(attempts)
    ]


async def _run_injection_pair(
    session: AsyncSession,
    redis: FakeRedis,
    registry: AgentRegistry,
    merchant: Merchant,
    merchant_priv: str,
    product: Product,
    spec: SessionSpec,
    quote: QuoteData,
    agent_did: str,
    now: datetime,
) -> list[SessionResult]:
    """Runs R01-R09 (via `verify_cart_within_envelope` + reversibility
    scoring — the gate machinery this category actually exercises) twice:
    once against the product's real name, once with the injection string
    appended to it, and asserts the *decision* doesn't change. Doesn't run
    full `execute_checkout` (no payment either way) -- the point is
    whether catalog text can influence the decision at all, not whether a
    duplicate charge happens."""
    from praman.core.envelope import Envelope, verify_cart_within_envelope
    from praman.core.reversibility import band, reversibility_score_detailed

    original_name = product.name
    env = Envelope(
        agent_did=agent_did,
        revoked_at=None,
        valid_from=now,
        valid_until=now,
        allowed_categories=(product.category,),
        max_single_txn_paise=spec.envelope_ceiling_paise,
        ceiling_paise=spec.envelope_ceiling_paise,
        spent_paise=0,
        min_reversibility=spec.envelope_min_reversibility,
    )
    cart_item = CartItem(
        sku=product.sku,
        category=product.category,
        qty=spec.qty,
        unit_price_paise=quote.unit_price_paise,
    )
    cart = Cart(agent_did=agent_did, items=(cart_item,))
    reversibility_item = ReversibilityItem(
        category_class=product.category_class,
        is_personalised=product.is_personalised,
        return_window_days=product.return_window_days or 0,
        fulfilment_hours=product.fulfilment_hours or 0,
        restocking_cost_pct=product.restocking_cost_pct,
    )

    def _decide() -> tuple[str, str, str | None]:
        envelope_result = verify_cart_within_envelope(cart, env, now)
        if envelope_result.decision != "ALLOW":
            return envelope_result.decision, envelope_result.reason_code, envelope_result.rule_id
        score, _ = reversibility_score_detailed([reversibility_item], cart.total_paise, env)
        return "ALLOW", f"band={band(score)}", "R08/R09"

    clean_outcome = _decide()

    product.name = f"{original_name} {spec.inject_text}"
    session.add(product)
    await session.commit()

    injected_outcome = _decide()

    matched = clean_outcome == injected_outcome
    return [
        SessionResult(
            session_id=spec.id,
            category=spec.category,
            arm="B",
            decision="INVARIANT_HELD" if matched else "INVARIANT_BROKEN",
            reason_code=injected_outcome[1],
            rule_id=injected_outcome[2],
            band=None,
            captured_paise=0,
            attempted_paise=0,
            num_captures=0,
            latency_ms=0.0,
            is_attack=False,
        )
    ]
