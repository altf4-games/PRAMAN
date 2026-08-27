"""Harness entrypoint — `python -m harness.run`. Builds a fresh sqlite
(in-memory) + fakeredis environment, seeds one merchant with both catalogs,
runs all 200 sessions through both arms, then the three metrics that need
live DB state (band accuracy, cooling-off cancellation, dispute-pack
completeness), and writes `harness/results.json` + `RESULTS.md`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fakeredis.aioredis
from praman.api.routes_dispute import dispute_pack as assemble_dispute_pack
from praman.core.checkout import cancel_order
from praman.core.envelope import Envelope
from praman.core.registry import LocalRegistry
from praman.core.reversibility import ReversibilityItem, band, reversibility_score_detailed
from praman.db import Base
from praman.models import Product
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harness.report import compute_metrics, write_report
from harness.sessions import build_sessions
from harness.setup import HarnessAdapters, seed_merchant_and_catalog
from harness.simulator import run_session_both_arms

logger = logging.getLogger(__name__)

NOW = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
LABELS_PATH = Path(__file__).resolve().parent / "labels.json"
OUT_DIR = Path(__file__).resolve().parent


async def _band_accuracy(session: AsyncSession, products: dict[str, Product]) -> dict[str, Any]:
    labels = json.loads(LABELS_PATH.read_text())["carts"]
    matches = 0
    confusion: Counter[tuple[str, str]] = Counter()
    for cart in labels:
        items = []
        total_paise = 0
        for line in cart["items"]:
            product = products[line["sku"]]
            items.append(
                ReversibilityItem(
                    category_class=product.category_class,
                    is_personalised=product.is_personalised,
                    return_window_days=product.return_window_days or 0,
                    fulfilment_hours=product.fulfilment_hours or 0,
                    restocking_cost_pct=product.restocking_cost_pct,
                )
            )
            total_paise += product.unit_price_paise * line["qty"]
        env = Envelope(
            agent_did="harness",
            revoked_at=None,
            valid_from=NOW,
            valid_until=NOW,
            allowed_categories=(),
            max_single_txn_paise=cart["assumed_envelope_ceiling_paise"],
            ceiling_paise=cart["assumed_envelope_ceiling_paise"],
            spent_paise=0,
        )
        score, _ = reversibility_score_detailed(items, total_paise, env)
        actual_band = band(score)
        label_band = cart["label_band"]
        confusion[(label_band, actual_band)] += 1
        if actual_band == label_band:
            matches += 1
    return {
        "total": len(labels),
        "matches": matches,
        "accuracy": matches / len(labels) if labels else None,
        "confusion": {f"{k[0]}|{k[1]}": v for k, v in confusion.items()},
    }


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapters = HarnessAdapters()

    async with session_factory() as session:
        merchant, merchant_priv, products = await seed_merchant_and_catalog(session)

        registry = LocalRegistry(session)
        specs = build_sessions()

        print(f"Running {len(specs)} sessions...")
        all_results = []
        for i, spec in enumerate(specs):
            results = await run_session_both_arms(
                session,
                redis,
                registry,
                adapters.razorpay,
                adapters.whatsapp,
                adapters.llm,
                merchant,
                merchant_priv,
                products,
                spec,
                now=NOW,
            )
            all_results.extend(results)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(specs)} sessions done")

        print("Computing band accuracy against harness/labels.json...")
        band_acc = await _band_accuracy(session, products)

        print("Simulating cooling-off cancellations...")
        held_order_ids = [
            (r.order_id, r.cart_id)
            for r in all_results
            if r.arm == "B" and r.decision == "HOLD" and r.order_id
        ]
        from praman.models import CartMandate, Order
        from sqlalchemy import select

        cancelled_count = 0
        for i, (order_id, cart_id) in enumerate(held_order_ids):
            if i % 3 != 0:  # a fixed, seeded fraction -- see RESULTS.md's Limitations
                continue
            order_row = (
                await session.execute(select(Order).where(Order.id == order_id))
            ).scalar_one_or_none()
            cart_row = (
                await session.execute(select(CartMandate).where(CartMandate.cart_id == cart_id))
            ).scalar_one_or_none()
            if order_row is None or cart_row is None:
                continue
            cancelled = await cancel_order(
                session,
                adapters.razorpay,
                order_row,
                amount_paise=cart_row.total_paise,
                now=NOW,
                reason="harness_simulated_buyer_cancel",
            )
            if cancelled:
                cancelled_count += 1

        cooling_off_stats = {
            "held": len(held_order_ids),
            "cancelled": cancelled_count,
            "cancellation_rate": cancelled_count / len(held_order_ids) if held_order_ids else None,
        }

        print("Checking dispute-pack completeness...")
        capturable_cart_ids = {
            r.cart_id
            for r in all_results
            if r.arm == "B" and r.decision in ("ALLOW", "HOLD", "ESCALATE") and r.cart_id
        }
        complete = 0
        for cart_id in capturable_cart_ids:
            try:
                pack = await assemble_dispute_pack(session, cart_id)
            except Exception:
                logger.warning(
                    "harness: dispute pack assembly failed for cart %s", cart_id, exc_info=True
                )
                continue
            ok = (
                bool(pack.envelope)
                and bool(pack.cart_mandate)
                and len(pack.gate_trail) > 0
                and pack.ledger["chain_verified"] is True
            )
            if ok:
                complete += 1
        dispute_pack_stats = {
            "total": len(capturable_cart_ids),
            "complete": complete,
            "completeness": complete / len(capturable_cart_ids) if capturable_cart_ids else None,
        }

    metrics = compute_metrics(
        all_results,
        band_accuracy=band_acc,
        cooling_off_stats=cooling_off_stats,
        dispute_pack_stats=dispute_pack_stats,
    )
    write_report(metrics, OUT_DIR)
    print(f"\nWrote {OUT_DIR / 'RESULTS.md'} and {OUT_DIR / 'results.json'}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
