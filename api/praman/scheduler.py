"""APScheduler sweeps — the two time-driven transitions nothing else
triggers: an amber order dispatching once its cooling-off window elapses
unclaimed, and a merchant-approval escalation timing out after
`MERCHANT_APPROVAL_TIMEOUT_S` (default 15 min, the design spec §6).

`sweep_cooling_off_dispatch` / `sweep_approvals` take an explicit
`AsyncSession` (same testability discipline as `gate.py`/`checkout.py`:
tests pass their own `db_session` fixture instead of the real engine).
`_run_sweeps`, wired into `main.py`'s lifespan via `create_scheduler`, is
the only caller that opens a real `SessionLocal` — one session per sweep
run, so a failure in one sweep can't corrupt the other's transaction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.config import get_settings
from praman.core.ledger import append_event
from praman.db import SessionLocal
from praman.models import Order
from praman.timeutil import as_aware_utc
from praman.whatsapp.approvals import sweep_expired_approvals
from praman.whatsapp.client import MultiChannelClient, WhatsAppClient, get_whatsapp_client
from praman.whatsapp.telegram_client import get_telegram_client

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_S = 5


async def sweep_cooling_off_dispatch(session: AsyncSession, now: datetime) -> int:
    """Dispatches any amber order whose cooling-off window has elapsed
    without a buyer cancel. Returns how many were dispatched."""
    dispatched_count = 0
    result = await session.execute(
        select(Order).where(
            Order.status == "captured",
            Order.cooling_off_until.is_not(None),
            Order.dispatched_at.is_(None),
            Order.cancelled_at.is_(None),
        )
    )
    for order in result.scalars().all():
        if order.cooling_off_until is None or now < as_aware_utc(order.cooling_off_until):
            continue
        order.dispatched_at = now
        session.add(order)
        await session.commit()
        await append_event(
            session, f"order:{order.id}", None, "ORDER_DISPATCHED", {"order_id": order.id}
        )
        dispatched_count += 1
    return dispatched_count


async def sweep_approvals(session: AsyncSession, whatsapp: WhatsAppClient, now: datetime) -> int:
    return await sweep_expired_approvals(session, whatsapp, now)


async def _run_sweeps() -> None:
    now = datetime.now(UTC)
    settings = get_settings()
    # Same routing-by-recipient-address fix as `deps.py::get_whatsapp_dep`
    # — a merchant-approval timeout denial for a Telegram-onboarded
    # merchant must go out over Telegram, not be silently attempted
    # through a Twilio client that has no idea what a `telegram:` address
    # is.
    whatsapp: WhatsAppClient = MultiChannelClient(
        get_whatsapp_client(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_whatsapp_from,
            use_fake=settings.twilio_use_fake,
        ),
        get_telegram_client(settings.telegram_bot_token, use_fake=settings.telegram_use_fake),
    )
    try:
        async with SessionLocal() as session:
            dispatched = await sweep_cooling_off_dispatch(session, now)
        async with SessionLocal() as session:
            denied = await sweep_approvals(session, whatsapp, now)
        if dispatched or denied:
            logger.info(
                "scheduler: dispatched %d cooling-off order(s), denied %d expired approval(s)",
                dispatched,
                denied,
            )
    except Exception:
        logger.exception("scheduler: sweep failed")


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_run_sweeps, "interval", seconds=_SWEEP_INTERVAL_S, id="praman_sweeps")
    return scheduler
