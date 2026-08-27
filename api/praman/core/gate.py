"""The Policy Gate — R01 through R12, in order, first non-ALLOW wins.
Every gate decision persists, ALLOW included (the design spec's non-negotiable
rule), and the whole body runs fail-closed: any unhandled exception is
caught, logged, and reported as a BLOCK, never silently swallowed into an
ALLOW.

```
R01 agent signature valid ........................ BLOCK      AGENT_SIG_INVALID
R02 agent registered & not revoked ............... BLOCK      AGENT_REVOKED
R03 envelope valid & unrevoked .................... BLOCK      ENVELOPE_INVALID
R04 verify_cart_within_envelope .................. BLOCK      (own reason code)
R05 quotes fresh & unconsumed .................... BLOCK      QUOTE_EXPIRED
R06 live price == quoted price ................... BLOCK      PRICE_DRIFT
R07 live stock available ......................... SUBSTITUTE OUT_OF_STOCK
R08 reversibility >= env.min_reversibility ....... ESCALATE   STEP_UP_REQUIRED
R09 band == amber ................................ HOLD       COOLING_OFF_OPEN
R10 velocity within rolling window ............... BLOCK      VELOCITY_EXCEEDED
R11 within trust tier + daily cap ................ ESCALATE   TIER_CEILING
R12 idempotency key unseen ....................... BLOCK      DUPLICATE_ATTEMPT
                                                    else       ALLOW
```

R01/R02 (signature + registration) are resolved together by
`registry.verify_agent_request`, which already tags its failures with the
right rule_id. R04 is `envelope.verify_cart_within_envelope`, unchanged.
R05-R07 are `quotes.verify_quote`, run once per cart item. R08/R09 are
`reversibility.reversibility_score_detailed` interpreted against the
envelope's policy and the fixed band thresholds.

R08's `human_present=True` on `GateRequest` satisfies the step-up
requirement without changing the reversibility score itself — this is how
a merchant's WhatsApp Approve (`whatsapp/approvals.py`) lets a request
that was escalated the first time through the gate pass on re-run. It also
skips R01's nonce-replay check (see `registry.verify_agent_request`'s
`skip_nonce_check` docstring for why that's safe only here): the re-run
reuses the agent's original signed request rather than a new one, since
the server can't re-sign as the agent.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.config import (
    DAILY_CAP_TTL_S,
    IDEMPOTENCY_KEY_TTL_S,
    VELOCITY_MAX_TRANSACTIONS,
    VELOCITY_MAX_TRANSACTIONS_DEMO_MODE,
    VELOCITY_WINDOW_DEMO_MODE_S,
    VELOCITY_WINDOW_S,
)
from praman.core.envelope import Cart, CartItem, Envelope, verify_cart_within_envelope
from praman.core.gate_types import GateResult, allow
from praman.core.ledger import append_event
from praman.core.quotes import QuoteData, verify_quote
from praman.core.registry import AgentRegistry, verify_agent_request
from praman.core.reversibility import ReversibilityItem, band, reversibility_score_detailed
from praman.models import GateDecision, IntentEnvelope, Merchant, Product
from praman.timeutil import as_aware_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GateRequest:
    session_id: str
    cart_id: str | None
    agent_did: str
    method: str
    body: bytes
    timestamp: str
    nonce: str
    signature: str
    envelope_id: str
    cart: Cart
    reversibility_items: tuple[ReversibilityItem, ...]  # parallel to cart.items
    quotes: tuple[QuoteData, ...]  # parallel to cart.items
    idempotency_key: str
    now: datetime
    human_present: bool = False


def envelope_from_row(row: IntentEnvelope) -> Envelope:
    return Envelope(
        agent_did=row.agent_did,
        revoked_at=as_aware_utc(row.revoked_at) if row.revoked_at is not None else None,
        valid_from=as_aware_utc(row.valid_from),
        valid_until=as_aware_utc(row.valid_until),
        allowed_categories=tuple(row.allowed_categories),
        max_single_txn_paise=row.max_single_txn_paise,
        ceiling_paise=row.ceiling_paise,
        spent_paise=row.spent_paise,
        min_reversibility=row.min_reversibility,
    )


def serialize_gate_request(req: GateRequest) -> dict[str, object]:
    """Persists the exact signed request that produced an ESCALATE, so a
    merchant's later WhatsApp Approve can re-run the gate against it (see
    the module docstring on `human_present`). `body` is hex-encoded since
    JSON has no bytes type; everything else round-trips as plain JSON."""
    return {
        "session_id": req.session_id,
        "cart_id": req.cart_id,
        "agent_did": req.agent_did,
        "method": req.method,
        "body_hex": req.body.hex(),
        "timestamp": req.timestamp,
        "nonce": req.nonce,
        "signature": req.signature,
        "envelope_id": req.envelope_id,
        "cart": {
            "agent_did": req.cart.agent_did,
            "items": [
                {
                    "sku": item.sku,
                    "category": item.category,
                    "qty": item.qty,
                    "unit_price_paise": item.unit_price_paise,
                }
                for item in req.cart.items
            ],
        },
        "reversibility_items": [
            {
                "category_class": item.category_class,
                "is_personalised": item.is_personalised,
                "return_window_days": item.return_window_days,
                "fulfilment_hours": item.fulfilment_hours,
                "restocking_cost_pct": item.restocking_cost_pct,
            }
            for item in req.reversibility_items
        ],
        "quotes": [
            {
                "quote_id": q.quote_id,
                "product_id": q.product_id,
                "sku": q.sku,
                "agent_did": q.agent_did,
                "merchant_did": q.merchant_did,
                "unit_price_paise": q.unit_price_paise,
                "qty": q.qty,
                "total_paise": q.total_paise,
                "stock_held": q.stock_held,
                "issued_at": q.issued_at.isoformat(),
                "expires_at": q.expires_at.isoformat(),
                "nonce": q.nonce,
                "signature": q.signature,
                "consumed_at": q.consumed_at.isoformat() if q.consumed_at else None,
            }
            for q in req.quotes
        ],
        "idempotency_key": req.idempotency_key,
    }


def deserialize_gate_request(data: dict[str, object], *, now: datetime) -> GateRequest:
    """Inverse of `serialize_gate_request`. `now` is always the *current*
    time at re-run, never the originally-persisted one — R04's expiry
    check and R05's quote-freshness check must see real elapsed time."""
    cart_data = data["cart"]
    assert isinstance(cart_data, dict)
    cart_items = cast(list[dict[str, Any]], cart_data["items"])
    cart = Cart(
        agent_did=str(cart_data["agent_did"]),
        items=tuple(
            CartItem(
                sku=str(i["sku"]),
                category=str(i["category"]),
                qty=int(i["qty"]),
                unit_price_paise=int(i["unit_price_paise"]),
            )
            for i in cart_items
        ),
    )
    reversibility_items_data = cast(list[dict[str, Any]], data["reversibility_items"])
    reversibility_items = tuple(
        ReversibilityItem(
            category_class=str(i["category_class"]),
            is_personalised=bool(i["is_personalised"]),
            return_window_days=int(i["return_window_days"]),
            fulfilment_hours=int(i["fulfilment_hours"]),
            restocking_cost_pct=float(i["restocking_cost_pct"]),
        )
        for i in reversibility_items_data
    )
    quotes_data = cast(list[dict[str, Any]], data["quotes"])
    quotes = tuple(
        QuoteData(
            quote_id=str(q["quote_id"]),
            product_id=str(q["product_id"]),
            sku=str(q["sku"]),
            agent_did=str(q["agent_did"]),
            merchant_did=str(q["merchant_did"]),
            unit_price_paise=int(q["unit_price_paise"]),
            qty=int(q["qty"]),
            total_paise=int(q["total_paise"]),
            stock_held=bool(q["stock_held"]),
            issued_at=datetime.fromisoformat(str(q["issued_at"])),
            expires_at=datetime.fromisoformat(str(q["expires_at"])),
            nonce=str(q["nonce"]),
            signature=str(q["signature"]),
            consumed_at=datetime.fromisoformat(str(q["consumed_at"])) if q["consumed_at"] else None,
        )
        for q in quotes_data
    )
    return GateRequest(
        session_id=str(data["session_id"]),
        cart_id=str(data["cart_id"]) if data["cart_id"] is not None else None,
        agent_did=str(data["agent_did"]),
        method=str(data["method"]),
        body=bytes.fromhex(str(data["body_hex"])),
        timestamp=str(data["timestamp"]),
        nonce=str(data["nonce"]),
        signature=str(data["signature"]),
        envelope_id=str(data["envelope_id"]),
        cart=cart,
        reversibility_items=reversibility_items,
        quotes=quotes,
        idempotency_key=str(data["idempotency_key"]),
        now=now,
        human_present=True,
    )


def _block(reason_code: str, detail: str, remedy: str, rule_id: str) -> GateResult:
    return GateResult(
        decision="BLOCK", reason_code=reason_code, detail=detail, remedy=remedy, rule_id=rule_id
    )


def _escalate(reason_code: str, detail: str, remedy: str, rule_id: str) -> GateResult:
    return GateResult(
        decision="ESCALATE", reason_code=reason_code, detail=detail, remedy=remedy, rule_id=rule_id
    )


async def _velocity_count(redis: Redis, agent_did: str, window_s: int, now: datetime) -> int:
    key = f"velocity:{agent_did}"
    cutoff = (now - timedelta(seconds=window_s)).timestamp()
    await redis.zremrangebyscore(key, "-inf", cutoff)
    count = await redis.zcard(key)
    return int(count)


async def _record_velocity(redis: Redis, agent_did: str, window_s: int, now: datetime) -> None:
    key = f"velocity:{agent_did}"
    # A unique member per call, not a value derived from `now` — the same
    # `now` is commonly reused across multiple requests/tests, and ZADD
    # would silently collapse same-member entries into one.
    await redis.zadd(key, {uuid.uuid4().hex: now.timestamp()})
    await redis.expire(key, window_s)


async def _daily_spend(redis: Redis, agent_did: str, now: datetime) -> int:
    key = f"daily_spend:{agent_did}:{now.date().isoformat()}"
    value = await redis.get(key)
    return int(value) if value is not None else 0


async def _record_daily_spend(
    redis: Redis, agent_did: str, now: datetime, amount_paise: int
) -> None:
    key = f"daily_spend:{agent_did}:{now.date().isoformat()}"
    await redis.incrby(key, amount_paise)
    await redis.expire(key, DAILY_CAP_TTL_S)


async def _evaluate(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    req: GateRequest,
    *,
    demo_mode: bool,
) -> GateResult:
    # R01 + R02
    auth_result = await verify_agent_request(
        registry,
        redis,
        agent_did=req.agent_did,
        method=req.method,
        body=req.body,
        timestamp=req.timestamp,
        nonce=req.nonce,
        signature=req.signature,
        now=req.now,
        skip_nonce_check=req.human_present,
    )
    if auth_result.decision != "ALLOW":
        return auth_result

    # R03: envelope exists
    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == req.envelope_id)
    )
    env_row = env_result.scalar_one_or_none()
    if env_row is None:
        return _block(
            "ENVELOPE_INVALID",
            f"envelope_id {req.envelope_id!r} does not exist.",
            "Submit a cart against a valid, issued envelope.",
            "R03",
        )

    env = envelope_from_row(env_row)

    # R04: verify_cart_within_envelope — the most important function in the repo
    envelope_result = verify_cart_within_envelope(req.cart, env, req.now)
    if envelope_result.decision != "ALLOW":
        return envelope_result

    # R05 / R06 / R07: per-item quote freshness, price match, stock
    merchant_result = await session.execute(
        select(Merchant).where(Merchant.id == env_row.merchant_id)
    )
    merchant = merchant_result.scalar_one_or_none()
    if merchant is None:
        return _block(
            "ENVELOPE_INVALID",
            f"envelope {req.envelope_id!r} references a merchant that no longer exists.",
            "Contact the merchant to re-issue the envelope.",
            "R03",
        )

    for quote in req.quotes:
        product_result = await session.execute(
            select(Product).where(Product.id == quote.product_id)
        )
        product = product_result.scalar_one_or_none()
        if product is None:
            return _block(
                "QUOTE_EXPIRED",
                f"quote references product_id {quote.product_id!r}, which no longer exists.",
                "Request a fresh quote.",
                "R05",
            )
        quote_result = verify_quote(
            quote,
            merchant.public_key,
            live_unit_price_paise=product.unit_price_paise,
            live_stock=product.stock,
            now=req.now,
        )
        if quote_result.decision != "ALLOW":
            return quote_result

    # R08 / R09: reversibility
    score, breakdown = reversibility_score_detailed(
        list(req.reversibility_items), req.cart.total_paise, env
    )
    cart_band = band(score)
    if score < env.min_reversibility and not req.human_present:
        return _escalate(
            "STEP_UP_REQUIRED",
            f"reversibility score {score:.3f} is below the envelope's minimum "
            f"{env.min_reversibility:.3f} (band={cart_band}).",
            "A human must step up to authorise this purchase.",
            "R08",
        )
    if cart_band == "amber":
        return GateResult(
            decision="HOLD",
            reason_code="COOLING_OFF_OPEN",
            detail=f"reversibility score {score:.3f} is in the amber band; "
            f"dispatch is held for the cooling-off window. breakdown={breakdown}",
            remedy="Dispatch proceeds automatically once the cooling-off window elapses "
            "unless the buyer cancels.",
            rule_id="R09",
        )

    # R10: velocity
    window_s = VELOCITY_WINDOW_DEMO_MODE_S if demo_mode else VELOCITY_WINDOW_S
    max_txns = VELOCITY_MAX_TRANSACTIONS_DEMO_MODE if demo_mode else VELOCITY_MAX_TRANSACTIONS
    current_velocity = await _velocity_count(redis, req.agent_did, window_s, req.now)
    if current_velocity >= max_txns:
        return _block(
            "VELOCITY_EXCEEDED",
            f"agent {req.agent_did!r} has made {current_velocity} allowed requests in the "
            f"last {window_s}s (limit {max_txns}).",
            "Wait for the rolling window to clear, or request a higher velocity allowance.",
            "R10",
        )

    # R11: trust tier + daily cap
    agent_record = await registry.resolve(req.agent_did)
    assert agent_record is not None  # guaranteed by R01/R02 already passing
    if req.cart.total_paise > agent_record.max_txn_paise:
        return _escalate(
            "TIER_CEILING",
            f"cart total {req.cart.total_paise} exceeds agent trust tier "
            f"{agent_record.trust_tier!r}'s max_txn_paise {agent_record.max_txn_paise}.",
            "Escalate for human approval, or use an agent identity with a higher trust tier.",
            "R11",
        )
    spent_today = await _daily_spend(redis, req.agent_did, req.now)
    if spent_today + req.cart.total_paise > agent_record.daily_cap_paise:
        return _escalate(
            "TIER_CEILING",
            f"spent_today {spent_today} + cart {req.cart.total_paise} exceeds daily_cap_paise "
            f"{agent_record.daily_cap_paise}.",
            "Escalate for human approval, or wait until the daily cap resets.",
            "R11",
        )

    # R12: idempotency
    idempotency_key = f"idempotency:{req.idempotency_key}"
    is_new = await redis.set(idempotency_key, "1", nx=True, ex=IDEMPOTENCY_KEY_TTL_S)
    if not is_new:
        return _block(
            "DUPLICATE_ATTEMPT",
            f"idempotency_key {req.idempotency_key!r} has already been used.",
            "This request was already processed; check the original result rather than retrying.",
            "R12",
        )

    # ALLOW: record velocity and daily spend now that every rule has passed.
    await _record_velocity(redis, req.agent_did, window_s, req.now)
    await _record_daily_spend(redis, req.agent_did, req.now, req.cart.total_paise)

    return allow(
        detail=f"cart allowed: score={score:.3f} band={cart_band} breakdown={breakdown}",
        rule_id=None,
    )


async def run_gate(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    req: GateRequest,
    *,
    demo_mode: bool = False,
) -> GateResult:
    """The gate's single entrypoint. Persists a `GateDecision` row and
    appends a ledger event for every outcome, including ALLOW. Fail-closed:
    an unhandled exception anywhere in `_evaluate` is caught here and
    reported as BLOCK/INTERNAL_ERROR — it never propagates into a decision
    that could be mistaken for ALLOW.
    """
    start = time.perf_counter()
    try:
        result = await _evaluate(session, redis, registry, req, demo_mode=demo_mode)
    except Exception:
        logger.exception("gate: unhandled exception evaluating session %s", req.session_id)
        result = GateResult(
            decision="BLOCK",
            reason_code="INTERNAL_ERROR",
            detail="An internal error occurred while evaluating this request.",
            remedy="Retry; if this persists, contact support.",
            rule_id=None,
        )
    latency_ms = (time.perf_counter() - start) * 1000

    now = datetime.now(UTC)
    session.add(
        GateDecision(
            session_id=req.session_id,
            cart_id=req.cart_id,
            decision=result.decision,
            reason_code=result.reason_code,
            rule_id=result.rule_id,
            detail=result.detail,
            remedy=result.remedy,
            evaluated_at=now,
            latency_ms=latency_ms,
        )
    )
    await session.commit()

    await append_event(
        session,
        req.session_id,
        req.agent_did,
        "GATE_DECISION",
        {
            "decision": result.decision,
            "reason_code": result.reason_code,
            "rule_id": result.rule_id,
            "detail": result.detail,
            "remedy": result.remedy,
            "latency_ms": latency_ms,
            "cart_id": req.cart_id,
        },
    )

    return result
