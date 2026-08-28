from __future__ import annotations

import pytest
from praman.adapters.razorpay_client import (
    FakeRazorpayClient,
    RazorpayPayment,
    RealRazorpayClient,
    S2SUnavailableError,
    create_and_capture_order,
    get_razorpay_client,
    make_idempotency_key,
)


def test_make_idempotency_key_is_deterministic() -> None:
    a = make_idempotency_key("cart-1", "did:key:zAgent")
    b = make_idempotency_key("cart-1", "did:key:zAgent")
    assert a == b


def test_make_idempotency_key_differs_for_different_inputs() -> None:
    a = make_idempotency_key("cart-1", "did:key:zAgent")
    b = make_idempotency_key("cart-2", "did:key:zAgent")
    assert a != b


def test_get_razorpay_client_returns_fake_when_requested() -> None:
    client = get_razorpay_client("rzp_test_x", "secret", "webhook_secret", use_fake=True)
    assert isinstance(client, FakeRazorpayClient)


def test_get_razorpay_client_returns_real_by_default() -> None:
    client = get_razorpay_client("rzp_test_x", "secret", "webhook_secret")
    assert isinstance(client, RealRazorpayClient)


def test_fake_client_signature_roundtrip() -> None:
    client = FakeRazorpayClient(webhook_secret="secret")
    body = b'{"event":"payment.captured"}'
    signature = client.sign(body)
    assert client.verify_webhook_signature(body, signature)
    assert not client.verify_webhook_signature(b"tampered", signature)


def test_fake_client_drive_to_captured_matches_simulate_payment() -> None:
    client = FakeRazorpayClient()
    order = client.create_order(15_000, "INR", "receipt-1", {})
    payment = client.drive_to_captured(order.order_id, order.amount_paise)
    assert payment.status == "captured"
    assert payment.order_id == order.order_id


def test_create_and_capture_order_with_fake_client() -> None:
    client = FakeRazorpayClient()
    order, payment, path = create_and_capture_order(client, 15_000, "INR", "receipt-1", {})
    assert path == "fake"
    assert payment.status == "captured"
    assert payment.order_id == order.order_id


def test_create_and_capture_order_falls_back_when_s2s_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RealRazorpayClient("rzp_test_dummy", "dummy_secret", "dummy_webhook_secret")

    def _create_order(amount_paise: int, currency: str, receipt: str, notes: dict[str, str]):
        from praman.adapters.razorpay_client import RazorpayOrder

        return RazorpayOrder(
            order_id="order_real_123",
            amount_paise=amount_paise,
            currency=currency,
            receipt=receipt,
            status="created",
            notes=notes,
        )

    def _drive_to_captured(order_id: str, amount_paise: int) -> RazorpayPayment:
        raise S2SUnavailableError("404 not enabled on this account")

    monkeypatch.setattr(client, "create_order", _create_order)
    monkeypatch.setattr(client, "drive_to_captured", _drive_to_captured)

    order, payment, path = create_and_capture_order(client, 15_000, "INR", "receipt-1", {})

    # S2S capture unavailable: the order is still real (visible in the
    # Razorpay dashboard), but no payment is fabricated for it — capturing
    # a genuine payment now needs a real Checkout.js round-trip, which
    # only a browser can drive (see the docstring on this function).
    assert path == "pending_real_checkout"
    assert order.order_id == "order_real_123"
    assert payment is None


def test_create_and_capture_order_uses_real_path_when_s2s_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RealRazorpayClient("rzp_test_dummy", "dummy_secret", "dummy_webhook_secret")

    def _create_order(amount_paise: int, currency: str, receipt: str, notes: dict[str, str]):
        from praman.adapters.razorpay_client import RazorpayOrder

        return RazorpayOrder(
            order_id="order_real_456",
            amount_paise=amount_paise,
            currency=currency,
            receipt=receipt,
            status="created",
            notes=notes,
        )

    def _drive_to_captured(order_id: str, amount_paise: int) -> RazorpayPayment:
        return RazorpayPayment(
            payment_id="pay_real_1", order_id=order_id, amount_paise=amount_paise, status="captured"
        )

    monkeypatch.setattr(client, "create_order", _create_order)
    monkeypatch.setattr(client, "drive_to_captured", _drive_to_captured)

    order, payment, path = create_and_capture_order(client, 15_000, "INR", "receipt-1", {})

    assert path == "real"
    assert payment.payment_id == "pay_real_1"
    assert order.order_id == "order_real_456"


def test_refund_payment_passes_amount_as_a_dict_not_a_bare_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for a real bug: `payment.refund`'s second positional
    argument is `data`, a dict the SDK JSON-serializes as the request body.
    Passing the raw `amount_paise` int directly produced a bare JSON
    scalar instead of `{"amount": ...}`, which Razorpay responded to with
    an empty body -- crashing the SDK's own `response.json()` and getting
    silently swallowed by `cancel_order`'s try/except, so a real undo
    cancelled the order but never actually refunded the payment. Found
    live: an actual buyer undo succeeded at the app level while the real
    Razorpay refund silently failed underneath it."""
    client = RealRazorpayClient("rzp_test_dummy", "dummy_secret", "dummy_webhook_secret")

    captured_args: dict[str, object] = {}

    def _fake_refund(payment_id: str, data: object = None, **kwargs: object) -> dict[str, object]:
        captured_args["payment_id"] = payment_id
        captured_args["data"] = data
        return {
            "id": "rfnd_test123",
            "payment_id": payment_id,
            "amount": 18000,
            "status": "processed",
        }

    monkeypatch.setattr(client._client.payment, "refund", _fake_refund)

    refund = client.refund_payment("pay_test123", 18000)

    assert captured_args["data"] == {"amount": 18000}
    assert refund.refund_id == "rfnd_test123"
    assert refund.amount_paise == 18000
