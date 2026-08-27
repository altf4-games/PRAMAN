"""Razorpay webhook — `POST /webhooks/razorpay`. Verifies the webhook
signature (HMAC-SHA256 over the raw body with `RAZORPAY_WEBHOOK_SECRET`)
before touching the payload at all, then reconciles the event into the
ledger. Most of this build's money path captures/refunds synchronously
(`core/checkout.py`, `core/checkout.py::cancel_order`); the one exception
is a real order awaiting a genuine Checkout.js payment
(`core/checkout.py::confirm_real_payment`), which drives state from the
browser's own signed callback rather than this webhook — so its job here
stays reconciliation and audit, not driving state either way.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from praman.api.deps import DbSession, RazorpayDep
from praman.core.ledger import append_event

router = APIRouter(tags=["razorpay"])


@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request, session: DbSession, razorpay: RazorpayDep
) -> dict[str, str]:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not razorpay.verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="invalid Razorpay webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="malformed webhook payload") from exc

    event = payload.get("event", "unknown")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = entity.get("order_id", "unknown")

    await append_event(
        session,
        f"razorpay_order:{order_id}",
        None,
        "RAZORPAY_WEBHOOK_RECEIVED",
        {"event": event, "razorpay_order_id": order_id, "payload": payload},
    )

    return {"status": "ok"}
