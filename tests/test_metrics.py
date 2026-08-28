"""`GET /api/metrics`, specifically `escalations_ever` — a real clarity bug
found live: the homepage used to show "Escalated to a human" as a count
of orders *currently* `pending_approval`, which reads as zero the moment
every past escalation has been resolved (approved, declined, or timed
out), giving a first-time viewer the false impression escalation had
never fired at all. `escalations_ever` is a cumulative count of every
ESCALATE gate decision instead, independent of what's resolved since.
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from praman.models import GateDecision, Order
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_api_routes import (  # noqa: F401 -- reused fixtures
    client,
    razorpay,
    redis,
    whatsapp,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_escalations_ever_counts_resolved_escalations_too(
    client: AsyncClient,  # noqa: F811 -- pytest fixture injection by name
    db_session: AsyncSession,
) -> None:
    # An ESCALATE decision whose order has since been resolved (captured,
    # not sitting in pending_approval) -- exactly the case that used to
    # read as "0 escalations ever" on the homepage.
    db_session.add(
        GateDecision(
            session_id="cart:resolved-escalation",
            cart_id="resolved-escalation",
            decision="ESCALATE",
            reason_code="STEP_UP_REQUIRED",
            rule_id="R08",
            detail="reversibility below threshold",
            remedy="merchant approval required",
            evaluated_at=NOW,
            latency_ms=1.0,
        )
    )
    db_session.add(
        Order(
            cart_id="resolved-escalation",
            razorpay_order_id="order_resolved",
            razorpay_payment_id="pay_resolved",
            status="captured",  # resolved -- not pending_approval anymore
            idempotency_key="idem-resolved-escalation",
            created_at=NOW,
        )
    )
    await db_session.commit()

    resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["escalations_ever"] == 1
    assert body["orders_by_status"].get("pending_approval", 0) == 0
