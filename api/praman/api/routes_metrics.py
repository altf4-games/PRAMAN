"""`GET /api/metrics` — powers the homepage's live counter and the
`/metrics` page. This is not the Phase 9 harness's Arm A/B comparison
(`RESULTS.md`) — it's a live, always-current count of what this specific
deployment has actually gated and settled, a much smaller and honest claim
than a benchmark result.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from praman.api.deps import DbSession
from praman.models import CartMandate, GateDecision, Order
from praman.schemas import MetricsOut

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
async def metrics(session: DbSession) -> MetricsOut:
    sessions_result = await session.execute(
        select(func.count(func.distinct(GateDecision.session_id)))
    )
    sessions_gated = sessions_result.scalar_one()

    status_result = await session.execute(select(Order.status, func.count()).group_by(Order.status))
    orders_by_status = {status: count for status, count in status_result.all()}

    band_result = await session.execute(
        select(CartMandate.band, func.count())
        .select_from(Order)
        .join(CartMandate, Order.cart_id == CartMandate.cart_id)
        .group_by(CartMandate.band)
    )
    orders_by_band = {band: count for band, count in band_result.all()}

    disputes_result = await session.execute(select(func.count(Order.id)))
    disputes_resolvable = disputes_result.scalar_one()

    # Cumulative, not a live snapshot -- see MetricsOut.escalations_ever's
    # own docstring for why `orders_by_status["pending_approval"]` alone
    # is a misleading stat to lead with.
    escalations_result = await session.execute(
        select(func.count(func.distinct(GateDecision.session_id))).where(
            GateDecision.decision == "ESCALATE"
        )
    )
    escalations_ever = escalations_result.scalar_one()

    return MetricsOut(
        sessions_gated=sessions_gated,
        orders_by_status=orders_by_status,
        orders_by_band=orders_by_band,
        disputes_resolvable=disputes_resolvable,
        escalations_ever=escalations_ever,
    )
