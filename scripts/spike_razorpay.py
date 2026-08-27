#!/usr/bin/env python3
"""Phase 0 spike (CLAUDE.md §4). Timeboxed to 2 hours.

Proves out the Razorpay TEST MODE integration end to end:
  1. Create a test-mode order via `RealRazorpayClient`.
  2. Drive it to `captured` — via Razorpay's server-to-server (S2S) JSON
     test-card API if the test account has S2S enabled, else fall back to
     `FakeRazorpayClient.simulate_payment` and say so plainly.
  3. Verify a webhook signature using the *real* verification code path
     (`RealRazorpayClient.verify_webhook_signature`), against a payload
     signed the same way Razorpay signs webhook deliveries.

Per CLAUDE.md: "If the real path isn't working in 2 hours, ship on Fake,
declare it in the README, move on." This script reports which path it took
for each step so that declaration is easy to write honestly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from praman.adapters.razorpay_client import (
    FakeRazorpayClient,
    RealRazorpayClient,
)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
TEST_CARD = {
    "number": "4111111111111111",
    "expiry_month": "12",
    "expiry_year": "2030",
    "cvv": "123",
    "name": "PRAMAN Spike",
}


def step(n: int, msg: str) -> None:
    print(f"\n[{n}] {msg}")


def try_s2s_capture(key_id: str, key_secret: str, order_id: str, amount_paise: int) -> str | None:
    """Attempt Razorpay's legacy S2S JSON test-card API to fabricate a real
    captured payment without a checkout round-trip. Returns payment_id on
    success, None if S2S isn't enabled on this test account (common default)
    or the call otherwise fails — caller falls back to the Fake client.
    """
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "email": "spike@praman.dev",
        "contact": "9999999999",
        "order_id": order_id,
        "method": "card",
        "card": TEST_CARD,
    }
    try:
        resp = requests.post(
            f"{RAZORPAY_API_BASE}/payments/create/json",
            json=payload,
            auth=(key_id, key_secret),
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"    S2S request failed: {exc}")
        return None

    if resp.status_code != 200:
        print(f"    S2S returned {resp.status_code}: {resp.text[:300]}")
        return None

    body = resp.json()
    payment_id = body.get("razorpay_payment_id") or body.get("id")
    if not payment_id:
        print(f"    S2S response had no payment id: {body}")
        return None
    return str(payment_id)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    key_id = os.environ["RAZORPAY_KEY_ID"]
    key_secret = os.environ["RAZORPAY_KEY_SECRET"]
    webhook_secret = os.environ["RAZORPAY_WEBHOOK_SECRET"]

    assert key_id.startswith("rzp_test_"), "refusing to run spike against a non-test key"

    real = RealRazorpayClient(key_id, key_secret, webhook_secret)

    # --- 1. create order (real) ---
    step(1, "create_order via RealRazorpayClient")
    order = real.create_order(
        amount_paise=15_000,
        currency="INR",
        receipt="spike-phase0-001",
        notes={"purpose": "phase0-spike"},
    )
    print(
        f"    real order created: {order.order_id} status={order.status} amount={order.amount_paise}"
    )

    # --- 2. drive to captured ---
    step(2, "drive order to captured")
    payment_id = try_s2s_capture(key_id, key_secret, order.order_id, order.amount_paise)
    path_used = "real"
    if payment_id is None:
        print("    S2S path unavailable on this test account (expected — S2S is opt-in).")
        print("    Falling back to FakeRazorpayClient.simulate_payment per CLAUDE.md §4 rule.")
        fake = FakeRazorpayClient(webhook_secret=webhook_secret)
        fake_order = fake.create_order(order.amount_paise, "INR", order.receipt or "", order.notes)
        payment = fake.simulate_payment(fake_order.order_id)
        path_used = "fake"
    else:
        payment = real.fetch_payment(payment_id)
        if payment.status != "captured":
            payment = real.capture_payment(payment_id, order.amount_paise)
    print(f"    [{path_used}] payment {payment.payment_id} status={payment.status}")
    assert payment.status == "captured"

    # --- 3. verify webhook signature (real verification code, real client) ---
    step(3, "verify_webhook_signature via RealRazorpayClient")
    webhook_body = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment.payment_id,
                    "order_id": order.order_id,
                    "amount": order.amount_paise,
                    "status": "captured",
                }
            }
        },
    }
    body_bytes = json.dumps(webhook_body, separators=(",", ":")).encode()
    signature = hmac.new(webhook_secret.encode(), body_bytes, hashlib.sha256).hexdigest()

    ok = real.verify_webhook_signature(body_bytes, signature)
    print(f"    signature valid: {ok}")
    assert ok, "webhook signature verification failed on a correctly-signed body"

    tampered = body_bytes.replace(b"captured", b"failed")
    ok_tampered = real.verify_webhook_signature(tampered, signature)
    print(f"    tampered body correctly rejected: {not ok_tampered}")
    assert not ok_tampered, "verification incorrectly accepted a tampered body"

    print("\n--- SPIKE RESULT ---")
    print("order.create   : real")
    print(f"drive-to-capture: {path_used}")
    print("webhook verify : real (code path), synthetic (delivery)")
    print("PASS")


if __name__ == "__main__":
    main()
