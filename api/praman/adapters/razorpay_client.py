"""Razorpay adapter.

`RazorpayClient` is a Protocol so the rest of the codebase never imports the
`razorpay` SDK directly. `RealRazorpayClient` talks to Razorpay's TEST MODE
API. `FakeRazorpayClient` is a deterministic in-memory stand-in used when the
real API is unavailable (rate limits, S2S not enabled on the test account,
offline dev) or in unit tests. Money is always `int` paise.

Non-negotiable: this module is I/O only. It contains no policy, no gate
logic, no LLM calls — see CLAUDE.md §0.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import razorpay

OrderStatus = Literal["created", "attempted", "paid"]
PaymentStatus = Literal["created", "authorized", "captured", "refunded", "failed"]


@dataclass(frozen=True, slots=True)
class RazorpayOrder:
    order_id: str
    amount_paise: int
    currency: str
    receipt: str | None
    status: OrderStatus
    notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RazorpayPayment:
    payment_id: str
    order_id: str
    amount_paise: int
    status: PaymentStatus
    method: str | None = None


class RazorpayClient(Protocol):
    """One implementation is real; one is fake. Nothing above this line
    should ever change when the real UAP-shaped payment rail replaces this
    Razorpay-specific adapter — same shape as `core.registry.AgentRegistry`.
    """

    def create_order(
        self, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder: ...

    def capture_payment(self, payment_id: str, amount_paise: int) -> RazorpayPayment: ...

    def fetch_payment(self, payment_id: str) -> RazorpayPayment: ...

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool: ...


class RealRazorpayClient:
    """Wraps the official `razorpay` SDK against api.razorpay.com in TEST MODE.

    `key_id` MUST start with `rzp_test_` — this is enforced by config
    validation (see `config.py`), not here, so a misconfigured live key still
    fails loudly rather than silently degrading.
    """

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str) -> None:
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._webhook_secret = webhook_secret

    def create_order(
        self, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder:
        resp: dict[str, Any] = self._client.order.create(
            {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "notes": notes,
                "payment_capture": 1,
            }
        )
        return RazorpayOrder(
            order_id=resp["id"],
            amount_paise=resp["amount"],
            currency=resp["currency"],
            receipt=resp.get("receipt"),
            status=resp["status"],
            notes=resp.get("notes", {}),
        )

    def capture_payment(self, payment_id: str, amount_paise: int) -> RazorpayPayment:
        resp: dict[str, Any] = self._client.payment.capture(
            payment_id, amount_paise, {"currency": "INR"}
        )
        return _payment_from_resp(resp)

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        resp: dict[str, Any] = self._client.payment.fetch(payment_id)
        return _payment_from_resp(resp)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        try:
            self._client.utility.verify_webhook_signature(
                body.decode(), signature, self._webhook_secret
            )
            return True
        except razorpay.errors.SignatureVerificationError:
            return False


def _payment_from_resp(resp: dict[str, Any]) -> RazorpayPayment:
    return RazorpayPayment(
        payment_id=resp["id"],
        order_id=resp["order_id"],
        amount_paise=resp["amount"],
        status=resp["status"],
        method=resp.get("method"),
    )


class FakeRazorpayClient:
    """Deterministic in-memory stand-in. Same signature scheme as Razorpay
    (HMAC-SHA256 over the raw body with the webhook secret) so code that
    verifies webhooks works identically against either implementation.
    """

    def __init__(self, webhook_secret: str = "fake_webhook_secret") -> None:
        self._webhook_secret = webhook_secret
        self._orders: dict[str, RazorpayOrder] = {}
        self._payments: dict[str, RazorpayPayment] = {}

    def create_order(
        self, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder:
        order = RazorpayOrder(
            order_id=f"order_fake_{uuid.uuid4().hex[:14]}",
            amount_paise=amount_paise,
            currency=currency,
            receipt=receipt,
            status="created",
            notes=notes,
        )
        self._orders[order.order_id] = order
        return order

    def capture_payment(self, payment_id: str, amount_paise: int) -> RazorpayPayment:
        existing = self._payments.get(payment_id)
        order_id = existing.order_id if existing else next(iter(self._orders), "order_unknown")
        payment = RazorpayPayment(
            payment_id=payment_id,
            order_id=order_id,
            amount_paise=amount_paise,
            status="captured",
            method="card",
        )
        self._payments[payment_id] = payment
        if order_id in self._orders:
            self._orders[order_id] = _replace_status(self._orders[order_id], "paid")
        return payment

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        if payment_id not in self._payments:
            raise KeyError(f"unknown payment_id: {payment_id}")
        return self._payments[payment_id]

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def sign(self, body: bytes) -> str:
        """Test helper: produce a valid signature for `body`, mirroring what
        Razorpay's webhook sender would compute."""
        return hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    def simulate_payment(self, order_id: str) -> RazorpayPayment:
        """Test helper: fabricate a captured payment against `order_id`
        without a checkout round-trip."""
        payment_id = f"pay_fake_{uuid.uuid4().hex[:14]}"
        order = self._orders[order_id]
        return self.capture_payment(payment_id, order.amount_paise)


def _replace_status(order: RazorpayOrder, status: OrderStatus) -> RazorpayOrder:
    return RazorpayOrder(
        order_id=order.order_id,
        amount_paise=order.amount_paise,
        currency=order.currency,
        receipt=order.receipt,
        status=status,
        notes=order.notes,
    )


def make_idempotency_key(cart_id: str, agent_did: str) -> str:
    return hashlib.sha256(f"{cart_id}|{agent_did}".encode()).hexdigest()


def now_ms() -> int:
    return int(time.time() * 1000)
