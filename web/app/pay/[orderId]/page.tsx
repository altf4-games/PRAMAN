"use client";

import { use, useCallback, useEffect, useState } from "react";
import { api, ApiError, type OrderOut } from "@/lib/api";
import { openRazorpayCheckout } from "@/lib/razorpayCheckout";

/** A standalone "complete this specific order's real payment" page —
 * exists because `/live`'s own "Pay with Razorpay" button only ever
 * works for an order created in that same browser session. An order
 * created anywhere else (a terminal MCP session, another agent entirely)
 * has nowhere to be paid for otherwise. Takes only an order_id in the
 * URL; everything else it needs (amount, real Razorpay order id, the
 * publishable key) comes from `GET /api/orders/{id}`. */
export default function PayPage({ params }: PageProps<"/pay/[orderId]">) {
  const { orderId } = use(params);

  const [order, setOrder] = useState<OrderOut | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [paying, setPaying] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const o = await api.orderStatus(orderId);
      setOrder(o);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof ApiError && err.status === 404
          ? "No order with this id."
          : err instanceof Error
            ? err.message
            : String(err),
      );
    }
  }, [orderId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function pay() {
    if (!order?.razorpay_order_id || !order.amount_paise || !order.razorpay_key_id) return;
    setPayError(null);
    setPaying(true);
    try {
      const response = await openRazorpayCheckout({
        razorpay_order_id: order.razorpay_order_id,
        amount_paise: order.amount_paise,
        cart_id: order.cart_id,
        razorpay_key_id: order.razorpay_key_id,
      });
      await api.checkoutConfirm(orderId, response);
      await refresh();
      setResult("Real Razorpay payment captured.");
    } catch (err) {
      setPayError(err instanceof Error ? err.message : String(err));
    } finally {
      setPaying(false);
    }
  }

  const payable =
    order && (order.status === "awaiting_payment" || order.status === "awaiting_payment_amber");

  return (
    <div className="mx-auto max-w-xl px-4 sm:px-6 py-10">
      <p className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-1">
        Complete payment
      </p>
      <h1 className="font-display text-3xl mb-6">Order {orderId}</h1>

      {loadError && (
        <div className="border border-band-red text-band-red p-4 text-sm font-mono">
          {loadError}
        </div>
      )}

      {order && (
        <div className="border border-rule p-4">
          <dl className="text-sm space-y-1 mb-4">
            <Row k="status" v={order.status} />
            <Row k="cart_id" v={order.cart_id} mono />
            {order.amount_paise != null && (
              <Row k="amount" v={`₹${(order.amount_paise / 100).toFixed(2)}`} />
            )}
          </dl>

          {payable ? (
            <>
              <p className="text-xs text-ink-muted mb-3">
                Real Razorpay order — use the domestic Mastercard test card{" "}
                <span className="font-mono">5267 3181 8797 5449</span>, any future expiry/CVV,
                any OTP. (<span className="font-mono">4111 1111 1111 1111</span> is flagged
                international and this account has those disabled.)
              </p>
              <button
                type="button"
                onClick={pay}
                disabled={paying}
                className="border border-ink bg-ink text-paper px-4 py-2 font-mono text-sm uppercase tracking-wide hover:bg-ink/85 disabled:opacity-40 transition-colors"
              >
                {paying ? "Opening Razorpay…" : "Pay with Razorpay (real)"}
              </button>
              {payError && <p className="mt-2 text-xs text-band-red font-mono">{payError}</p>}
            </>
          ) : (
            <p className="text-sm text-ink-muted">
              {order.status === "captured"
                ? "Already paid — nothing to do here."
                : `This order isn't awaiting payment (status: ${order.status}).`}
            </p>
          )}

          {result && <p className="mt-3 text-xs text-band-green font-mono">{result}</p>}

          <a
            href={`/dispute/${orderId}`}
            className="mt-4 inline-block border border-rule px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-paper-raised transition-colors"
          >
            View dispute pack →
          </a>
        </div>
      )}
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="font-mono text-[11px] uppercase text-ink-muted shrink-0">{k}</dt>
      <dd className={`text-right truncate ${mono ? "font-mono text-xs" : ""}`}>{v}</dd>
    </div>
  );
}
