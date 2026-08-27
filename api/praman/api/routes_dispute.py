"""`GET /api/dispute-pack/{cart_id}` — a Phase 8 preview, pulled forward
because `/dispute/[orderId]` needs real content to render rather than a
stub. Assembles envelope, cart mandate, gate trail, order (if any), and
the ordered ledger trail with its chain-verification result — the same
`session_id` convention (`cart:{cart_id}`) every Phase 6 route already
appends events under, so this needs no new bookkeeping.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from praman.api.deps import DbSession
from praman.core.ledger import dispute_pack_events
from praman.models import CartMandate, GateDecision, IntentEnvelope, Order
from praman.schemas import DisputePackOut
from praman.timeutil import as_aware_utc

router = APIRouter(prefix="/api", tags=["dispute"])


@router.get("/dispute-pack/{cart_id}")
async def dispute_pack(session: DbSession, cart_id: str) -> DisputePackOut:
    cart_result = await session.execute(select(CartMandate).where(CartMandate.cart_id == cart_id))
    cart = cart_result.scalar_one_or_none()
    if cart is None:
        raise HTTPException(status_code=404, detail="cart not found")

    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == cart.envelope_id)
    )
    env = env_result.scalar_one_or_none()

    order_result = await session.execute(select(Order).where(Order.cart_id == cart_id))
    order = order_result.scalar_one_or_none()

    gate_result = await session.execute(
        select(GateDecision)
        .where(GateDecision.cart_id == cart_id)
        .order_by(GateDecision.evaluated_at)
    )
    gate_trail: list[dict[str, object]] = [
        {
            "decision": g.decision,
            "reason_code": g.reason_code,
            "rule_id": g.rule_id,
            "detail": g.detail,
            "remedy": g.remedy,
            "evaluated_at": as_aware_utc(g.evaluated_at).isoformat(),
            "latency_ms": g.latency_ms,
        }
        for g in gate_result.scalars().all()
    ]

    ledger = await dispute_pack_events(session, f"cart:{cart_id}")

    return DisputePackOut(
        cart_id=cart.cart_id,
        envelope={
            "envelope_id": env.envelope_id,
            "agent_did": env.agent_did,
            "ceiling_paise": env.ceiling_paise,
            "spent_paise": env.spent_paise,
            "max_single_txn_paise": env.max_single_txn_paise,
            "allowed_categories": env.allowed_categories,
            "min_reversibility": env.min_reversibility,
            "valid_from": as_aware_utc(env.valid_from).isoformat(),
            "valid_until": as_aware_utc(env.valid_until).isoformat(),
            "signature": env.signature,
        }
        if env is not None
        else {},
        cart_mandate={
            "cart_id": cart.cart_id,
            "agent_did": cart.agent_did,
            "items": cart.items,
            "subtotal_paise": cart.subtotal_paise,
            "total_paise": cart.total_paise,
            "agent_sig": cart.agent_sig,
            "created_at": as_aware_utc(cart.created_at).isoformat(),
        },
        order={
            "id": order.id,
            "status": order.status,
            "razorpay_order_id": order.razorpay_order_id,
            "cooling_off_until": as_aware_utc(order.cooling_off_until).isoformat()
            if order.cooling_off_until
            else None,
            "dispatched_at": as_aware_utc(order.dispatched_at).isoformat()
            if order.dispatched_at
            else None,
            "cancelled_at": as_aware_utc(order.cancelled_at).isoformat()
            if order.cancelled_at
            else None,
        }
        if order is not None
        else None,
        gate_trail=gate_trail,
        reversibility_breakdown=cart.reversibility_breakdown,
        band=cart.band,
        ledger=ledger,
    )
