/** Shared Razorpay Checkout.js loader — used by `/live`'s "Pay with
 * Razorpay" button and `/pay/[orderId]`, the standalone page that
 * completes payment for an order created outside the browser entirely
 * (a terminal MCP session, another agent). Both need the exact same real
 * widget: since server-to-server test-card capture isn't enabled on this
 * account (confirmed against the live API), a real Razorpay Order exists
 * the moment the gate ALLOWs/HOLDs, but a real Payment only exists once
 * this widget's card form actually runs.
 */

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => { open: () => void };
  }
}

export function loadRazorpayCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://checkout.razorpay.com/v1/checkout.js";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("failed to load Razorpay Checkout.js"));
    document.body.appendChild(script);
  });
}

export interface RazorpayCheckoutSuccess {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
}

/** Opens the real widget for one order and resolves with the browser's
 * signed success callback, or rejects (including on user dismissal). The
 * caller is responsible for verifying it server-side
 * (`api.checkoutConfirm`) — this function never trusts the callback on
 * its own, it just plumbs it through. */
export async function openRazorpayCheckout(order: {
  razorpay_order_id: string;
  amount_paise: number;
  cart_id: string;
  razorpay_key_id: string;
}): Promise<RazorpayCheckoutSuccess> {
  await loadRazorpayCheckout();
  if (!window.Razorpay) throw new Error("Razorpay Checkout.js did not load");

  return new Promise<RazorpayCheckoutSuccess>((resolve, reject) => {
    const rzp = new window.Razorpay!({
      key: order.razorpay_key_id,
      order_id: order.razorpay_order_id,
      amount: order.amount_paise,
      currency: "INR",
      name: "PRAMAN",
      description: `cart ${order.cart_id}`,
      prefill: { contact: "9999999999", email: "checkout@praman.dev" },
      theme: { color: "#141A22" },
      handler: (response: RazorpayCheckoutSuccess) => resolve(response),
      modal: { ondismiss: () => reject(new Error("checkout dismissed")) },
    });
    rzp.open();
  });
}
