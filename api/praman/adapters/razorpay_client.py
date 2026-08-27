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
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import razorpay

OrderStatus = Literal["created", "attempted", "paid"]
PaymentStatus = Literal["created", "authorized", "captured", "refunded", "failed"]

_S2S_API_BASE = "https://api.razorpay.com/v1"
_S2S_TEST_CARD = {
    "number": "4111111111111111",
    "expiry_month": "12",
    "expiry_year": "2030",
    "cvv": "123",
    "name": "PRAMAN Checkout",
}


class S2SUnavailableError(Exception):
    """Razorpay's server-to-server test-card capture isn't enabled on this
    account (confirmed in the Phase 0 spike: it's opt-in and off by
    default). Callers catch this and fall back to `FakeRazorpayClient`,
    logging the fallback explicitly rather than pretending it's real."""


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


@dataclass(frozen=True, slots=True)
class RazorpayRefund:
    refund_id: str
    payment_id: str
    amount_paise: int
    status: str


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

    def drive_to_captured(self, order_id: str, amount_paise: int) -> RazorpayPayment: ...

    def refund_payment(self, payment_id: str, amount_paise: int) -> RazorpayRefund: ...


class RealRazorpayClient:
    """Wraps the official `razorpay` SDK against api.razorpay.com in TEST MODE.

    `key_id` MUST start with `rzp_test_` — this is enforced by config
    validation (see `config.py`), not here, so a misconfigured live key still
    fails loudly rather than silently degrading.
    """

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str) -> None:
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._webhook_secret = webhook_secret
        self._key_id = key_id
        self._key_secret = key_secret

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

    def drive_to_captured(self, order_id: str, amount_paise: int) -> RazorpayPayment:
        """Attempts the S2S JSON test-card capture proven out in
        `scripts/spike_razorpay.py`. Raises `S2SUnavailableError` if the
        account doesn't have S2S enabled (the confirmed default) rather
        than silently degrading — the caller decides the fallback."""
        import requests

        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": "INR",
            "email": "checkout@praman.dev",
            "contact": "9999999999",
            "order_id": order_id,
            "method": "card",
            "card": _S2S_TEST_CARD,
        }
        try:
            resp = requests.post(
                f"{_S2S_API_BASE}/payments/create/json",
                json=payload,
                auth=(self._key_id, self._key_secret),
                timeout=15,
            )
        except requests.RequestException as exc:
            raise S2SUnavailableError(str(exc)) from exc

        if resp.status_code != 200:
            raise S2SUnavailableError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        body = resp.json()
        payment_id = body.get("razorpay_payment_id") or body.get("id")
        if not payment_id:
            raise S2SUnavailableError(f"no payment id in response: {body}")
        return self.fetch_payment(str(payment_id))

    def refund_payment(self, payment_id: str, amount_paise: int) -> RazorpayRefund:
        resp: dict[str, Any] = self._client.payment.refund(payment_id, amount_paise)
        return RazorpayRefund(
            refund_id=resp["id"],
            payment_id=resp["payment_id"],
            amount_paise=resp["amount"],
            status=resp.get("status", "processed"),
        )


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
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
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

    def drive_to_captured(self, order_id: str, amount_paise: int) -> RazorpayPayment:
        """Mirrors `RealRazorpayClient.drive_to_captured`'s signature so
        callers don't need to branch on which implementation they hold."""
        return self.simulate_payment(order_id)

    def refund_payment(self, payment_id: str, amount_paise: int) -> RazorpayRefund:
        if payment_id not in self._payments:
            raise KeyError(f"unknown payment_id: {payment_id}")
        refund = RazorpayRefund(
            refund_id=f"rfnd_fake_{uuid.uuid4().hex[:14]}",
            payment_id=payment_id,
            amount_paise=amount_paise,
            status="processed",
        )
        self._payments[payment_id] = replace(self._payments[payment_id], status="refunded")
        return refund


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


def get_razorpay_client(
    key_id: str, key_secret: str, webhook_secret: str, *, use_fake: bool = False
) -> RazorpayClient:
    if use_fake:
        return FakeRazorpayClient(webhook_secret=webhook_secret or "fake_webhook_secret")
    return RealRazorpayClient(key_id, key_secret, webhook_secret)


def create_and_capture_order(
    client: RazorpayClient, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
) -> tuple[RazorpayOrder, RazorpayPayment, str]:
    """The order-then-payment sequence checkout needs, with the same
    real-then-fake fallback Phase 0's spike established. Returns
    (order, payment, path_used) — 'real' or 'fake' — so the caller can
    ledger which path actually ran, per CLAUDE.md's honesty rule (never
    let a demo silently claim to be more real than it is).

    A real bug lived here until it was caught by the harness's cooling-off
    cancellation simulation: the Fake fallback used to construct a *new*,
    throwaway `FakeRazorpayClient()` instead of capturing through `client`
    itself when `client` was already a `FakeRazorpayClient` (DEMO_MODE,
    every test, and the harness all use one as the primary client, not
    just as a fallback). A later `cancel_order` → `refund_payment` call
    against the *original* client then couldn't find the payment — it was
    created in an instance nobody kept a reference to — and silently
    failed, while `cancel_order` still marked the order `refunded_at`
    regardless. Fixed by capturing through `client` directly whenever it's
    already Fake, so the payment a later refund looks for is the one that
    actually exists.
    """
    order = client.create_order(amount_paise, currency, receipt, notes)

    if isinstance(client, RealRazorpayClient):
        try:
            payment = client.drive_to_captured(order.order_id, amount_paise)
            return order, payment, "real"
        except S2SUnavailableError:
            pass  # fall through to the Fake path below, same as Phase 0
        # S2S unavailable: the payment this produces was never real to
        # begin with, so a later refund against the real Razorpay API
        # cannot succeed for it regardless of which Fake instance
        # captures it — disclosed in README's "what's real vs mocked".
        fake = FakeRazorpayClient()
        fake_order = fake.create_order(amount_paise, currency, receipt, notes)
        payment = fake.simulate_payment(fake_order.order_id)
        # Report under the REAL order's id — that's the order of record
        # (the one whose notes carry the cart_mandate_hash and that
        # webhooks/the ledger reference), even though capture itself used
        # the Fake path.
        payment = replace(payment, order_id=order.order_id)
        return order, payment, "fake"

    # `client` is already a FakeRazorpayClient — capture through it
    # directly so its own `_payments` dict actually holds this payment for
    # a later `refund_payment` call to find.
    payment = client.simulate_payment(order.order_id)
    return order, payment, "fake"
