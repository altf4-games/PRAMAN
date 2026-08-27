"""`/api/approvals` — the REST mirror of the merchant WhatsApp inbox
(CLAUDE.md §7: "Actions here and on WhatsApp stay in sync"). Listing reads
straight from `Order`/`CartMandate`; deciding calls the exact same
`_approve`/`_decline` logic `whatsapp/approvals.py`'s reply handler uses
(via `decide_by_order_id`), so a click here and a WhatsApp reply can never
produce different outcomes for the same order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from praman.api.deps import DbSession, LLMDep, RazorpayDep, RedisDep, RegistryDep, WhatsAppDep
from praman.config import get_settings
from praman.core.stepup import approval_deadline
from praman.models import CartMandate, LedgerEvent
from praman.schemas import ApprovalDecideIn, ApprovalDecideOut, ApprovalOut
from praman.timeutil import as_aware_utc
from praman.whatsapp.approvals import decide_by_order_id, list_pending_approvals

router = APIRouter(prefix="/api", tags=["approvals"])


@router.get("/approvals")
async def approvals_inbox(session: DbSession, merchant_id: str) -> list[ApprovalOut]:
    orders = await list_pending_approvals(session, merchant_id)
    out: list[ApprovalOut] = []
    for order in orders:
        cart_result = await session.execute(
            select(CartMandate).where(CartMandate.cart_id == order.cart_id)
        )
        cart = cart_result.scalar_one_or_none()
        if cart is None:
            continue
        item_summary = ", ".join(f"{i['qty']}x {i['sku']}" for i in cart.items)
        # The reason code lives on the ORDER_PENDING_APPROVAL ledger event
        # (checkout.py's _handle_escalate), not on pending_gate_request
        # (which is the agent's original *request*, not the gate's verdict).
        reason_result = await session.execute(
            select(LedgerEvent)
            .where(
                LedgerEvent.session_id == f"cart:{order.cart_id}",
                LedgerEvent.event_type == "ORDER_PENDING_APPROVAL",
            )
            .order_by(LedgerEvent.ts.desc())
            .limit(1)
        )
        reason_event = reason_result.scalar_one_or_none()
        reason_code = (
            str(reason_event.payload_json.get("reason_code", "STEP_UP_REQUIRED"))
            if reason_event is not None
            else "STEP_UP_REQUIRED"
        )
        created_at = as_aware_utc(order.created_at)
        out.append(
            ApprovalOut(
                order_id=order.id,
                cart_id=order.cart_id,
                merchant_id=merchant_id,
                item_summary=item_summary,
                total_paise=cart.total_paise,
                reason_code=reason_code,
                reversibility_score=cart.reversibility_score,
                reversibility_breakdown=cart.reversibility_breakdown,
                band=cart.band,
                created_at=created_at.isoformat(),
                deadline=approval_deadline(created_at).isoformat(),
            )
        )
    return out


@router.post("/approvals/{order_id}/decide")
async def approvals_decide(
    session: DbSession,
    order_id: str,
    body: ApprovalDecideIn,
    redis: RedisDep,
    registry: RegistryDep,
    razorpay: RazorpayDep,
    whatsapp: WhatsAppDep,
    llm: LLMDep,
) -> ApprovalDecideOut:
    if body.decision not in ("approve", "decline"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'decline'")
    try:
        order = await decide_by_order_id(
            session,
            redis,
            registry,
            razorpay,
            whatsapp,
            llm,
            order_id,
            body.decision,
            demo_mode=get_settings().demo_mode,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApprovalDecideOut(decision=body.decision, order_status=order.status)
