"use client";

import { useEffect, useState } from "react";
import { api, type MerchantOut, type ProductOut, type QuoteOut } from "@/lib/api";
import { signRequest } from "@/lib/sign";
import { useLedgerStream } from "@/lib/sse";
import { ReversibilityGauge } from "@/components/ReversibilityGauge";
import { LedgerStream } from "@/components/LedgerStream";
import { BandSeal } from "@/components/BandSeal";

interface ToolCall {
  label: string;
  status: "pending" | "ok" | "error";
  detail?: string;
}

type CartResult = {
  cart_id: string;
  band: "green" | "amber" | "red";
  reversibility_score: number;
  reversibility_breakdown: Record<string, number>;
};

type CheckoutResult = {
  decision: string;
  reason_code: string;
  detail: string;
  order_id: string | null;
  order_status: string | null;
};

export default function LivePage() {
  const [merchants, setMerchants] = useState<MerchantOut[]>([]);
  const [merchantId, setMerchantId] = useState<string>("");
  const [products, setProducts] = useState<ProductOut[]>([]);
  const [productId, setProductId] = useState<string>("");
  const [ceiling, setCeiling] = useState(500_000);
  const [minReversibility, setMinReversibility] = useState(0);

  const [running, setRunning] = useState(false);
  const [calls, setCalls] = useState<ToolCall[]>([]);
  const [cart, setCart] = useState<CartResult | null>(null);
  const [checkout, setCheckout] = useState<CheckoutResult | null>(null);
  const [userRef, setUserRef] = useState<string>("");
  const [postAction, setPostAction] = useState<string | null>(null);

  useEffect(() => {
    api
      .listMerchants()
      .then((list) => {
        const live = list.filter((m) => m.onboarding_state === "LIVE");
        setMerchants(live);
        if (live[0]) setMerchantId(live[0].id);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!merchantId) return;
    api
      .catalogSearch(merchantId)
      .then((list) => {
        setProducts(list);
        setProductId((prev) => (list.some((p) => p.id === prev) ? prev : (list[0]?.id ?? "")));
      })
      .catch(() => undefined);
  }, [merchantId]);

  const { events, connected } = useLedgerStream(cart ? `cart:${cart.cart_id}` : null);

  function pushCall(label: string) {
    setCalls((prev) => [...prev, { label, status: "pending" }]);
  }
  function settleCall(ok: boolean, detail?: string) {
    setCalls((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last) {
        last.status = ok ? "ok" : "error";
        last.detail = detail;
      }
      return next;
    });
  }

  async function runSession() {
    const product = products.find((p) => p.id === productId);
    if (!product) return;
    setRunning(true);
    setCalls([]);
    setCart(null);
    setCheckout(null);
    setPostAction(null);
    const ref = `web-buyer-${Date.now().toString(36)}`;
    setUserRef(ref);

    try {
      pushCall("agents_register");
      const agent = await api.registerAgent({
        operator: "praman-live-demo",
        max_txn_paise: ceiling,
        daily_cap_paise: ceiling * 10,
      });
      settleCall(true, agent.agent_did);
      if (!agent.private_key) throw new Error("server did not return a demo private key");

      pushCall("envelope_submit");
      const envelope = await api.submitEnvelope({
        merchant_id: merchantId,
        agent_did: agent.agent_did,
        user_ref: ref,
        user_whatsapp: "whatsapp:+919000000000",
        ceiling_paise: ceiling,
        max_single_txn_paise: ceiling,
        allowed_categories: [product.category],
        min_reversibility: minReversibility,
        valid_hours: 24,
      });
      settleCall(true, envelope.envelope_id);

      pushCall("quote_request");
      const quoteBody = JSON.stringify({ product_id: product.id, agent_did: agent.agent_did, qty: 1 });
      const quoteSig = signRequest(agent.private_key, "POST", quoteBody);
      const quote: QuoteOut = await api.requestQuote(quoteBody, quoteSig);
      settleCall(true, `${(quote.total_paise / 100).toFixed(2)} INR`);

      pushCall("cart_confirm");
      const cartBody = JSON.stringify({
        envelope_id: envelope.envelope_id,
        agent_did: agent.agent_did,
        quotes: [quote],
      });
      const cartSig = signRequest(agent.private_key, "POST", cartBody);
      const cartResult = await api.confirmCart(cartBody, cartSig);
      settleCall(true, `band=${cartResult.band} score=${cartResult.reversibility_score.toFixed(3)}`);
      setCart({
        cart_id: cartResult.cart_id,
        band: cartResult.band,
        reversibility_score: cartResult.reversibility_score,
        reversibility_breakdown: cartResult.reversibility_breakdown,
      });

      pushCall("checkout_execute");
      const checkoutBody = JSON.stringify({
        cart_id: cartResult.cart_id,
        agent_did: agent.agent_did,
        quotes: [quote],
      });
      const checkoutSig = signRequest(agent.private_key, "POST", checkoutBody);
      const result = await api.executeCheckout(checkoutBody, checkoutSig);
      settleCall(result.decision !== "BLOCK", result.decision);
      setCheckout(result);
    } catch (err) {
      settleCall(false, err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  async function approveAsMerchant() {
    if (!checkout?.order_id) return;
    const decision = await api.approvalsDecide(checkout.order_id, "approve");
    const status = await api.orderStatus(checkout.order_id);
    setCheckout((prev) => (prev ? { ...prev, order_status: status.status } : prev));
    setPostAction(
      status.status === "captured"
        ? "Merchant approved — order captured."
        : `Approved, but the order didn't end up captured (status: ${decision.order_status}).`,
    );
  }

  async function undoAsBuyer() {
    if (!checkout?.order_id) return;
    // A HOLD order in DEMO_MODE dispatches automatically 60s after
    // capture — a real race the UI must reflect honestly rather than
    // claiming success regardless (a bug found live: this used to always
    // show "cancelled" even when the order had already dispatched and
    // the cancel silently no-op'd).
    const result = await api.orderUndo(checkout.order_id, userRef);
    const status = await api.orderStatus(checkout.order_id);
    setCheckout((prev) => (prev ? { ...prev, order_status: status.status } : prev));
    setPostAction(
      result.cancelled
        ? "Buyer cancelled — refunded."
        : "Too late — the cooling-off window already elapsed and the order dispatched.",
    );
  }

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 py-10">
      <h1 className="font-display text-3xl mb-2">Live agent session</h1>
      <p className="text-ink-muted max-w-2xl mb-6">
        Pick a shop and a product, set an envelope, and run a real signed agent session against
        the live API — every step below is a genuine HTTP call, not a simulation.
      </p>

      <div className="border border-rule bg-paper-raised p-4 mb-8 grid sm:grid-cols-2 lg:grid-cols-5 gap-4 items-end">
        <Field label="Shop">
          <select
            value={merchantId}
            onChange={(e) => setMerchantId(e.target.value)}
            className="w-full border border-rule bg-paper px-2 py-1.5 text-sm font-mono"
          >
            {merchants.length === 0 && <option value="">No live shops yet</option>}
            {merchants.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Product">
          <select
            value={productId}
            onChange={(e) => setProductId(e.target.value)}
            className="w-full border border-rule bg-paper px-2 py-1.5 text-sm font-mono"
          >
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} — ₹{(p.unit_price_paise / 100).toFixed(0)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Envelope ceiling (₹)">
          <input
            type="number"
            min={1}
            value={ceiling / 100}
            onChange={(e) => setCeiling(Math.max(1, Number(e.target.value)) * 100)}
            className="w-full border border-rule bg-paper px-2 py-1.5 text-sm font-mono"
          />
        </Field>
        <Field label="Min. reversibility">
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={minReversibility}
            onChange={(e) => setMinReversibility(Number(e.target.value))}
            className="w-full border border-rule bg-paper px-2 py-1.5 text-sm font-mono"
          />
        </Field>
        <button
          type="button"
          onClick={runSession}
          disabled={running || !merchantId || !productId}
          className="border border-ink bg-ink text-paper px-4 py-2 font-mono text-sm uppercase tracking-wide hover:bg-ink/85 disabled:opacity-40 transition-colors"
        >
          {running ? "Running…" : "Run agent session"}
        </button>
      </div>

      <div className="grid lg:grid-cols-3 gap-8">
        <section aria-labelledby="calls-heading">
          <h2 id="calls-heading" className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">
            Tool calls
          </h2>
          <ol className="border border-rule divide-y divide-rule">
            {calls.length === 0 && (
              <li className="px-3 py-6 text-center text-sm text-ink-muted">
                Nothing run yet.
              </li>
            )}
            {calls.map((c, i) => (
              <li key={i} className="px-3 py-2 text-sm flex items-center justify-between gap-2">
                <span className="font-mono truncate">{c.label}</span>
                <span
                  className={`font-mono text-[10px] uppercase shrink-0 ${
                    c.status === "ok"
                      ? "text-band-green"
                      : c.status === "error"
                        ? "text-band-red"
                        : "text-ink-muted"
                  }`}
                  title={c.detail}
                >
                  {c.status}
                </span>
              </li>
            ))}
          </ol>

          {checkout && (
            <div className="mt-4 border border-rule p-3">
              <p className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-1">
                Gate decision
              </p>
              <p className="text-sm mb-1">
                <span className="font-mono">{checkout.decision}</span> — {checkout.detail}
              </p>
              {checkout.order_id && (
                <p className="text-xs text-ink-muted font-mono">
                  order {checkout.order_id} · {checkout.order_status}
                </p>
              )}
              {checkout.decision === "ESCALATE" &&
                checkout.order_status === "pending_approval" &&
                !postAction && (
                  <button
                    type="button"
                    onClick={approveAsMerchant}
                    className="mt-2 border border-band-amber text-band-amber px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-band-amber/10 transition-colors"
                  >
                    Approve as merchant
                  </button>
                )}
              {checkout.decision === "HOLD" && checkout.order_status === "captured" && !postAction && (
                <button
                  type="button"
                  onClick={undoAsBuyer}
                  className="mt-2 border border-band-amber text-band-amber px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-band-amber/10 transition-colors"
                >
                  Undo as buyer
                </button>
              )}
              {postAction && <p className="mt-2 text-xs text-band-green font-mono">{postAction}</p>}
            </div>
          )}
        </section>

        <section aria-labelledby="gauge-heading">
          <h2 id="gauge-heading" className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">
            Reversibility
          </h2>
          {cart ? (
            <div className="border border-rule p-4">
              <ReversibilityGauge
                score={cart.reversibility_score}
                breakdown={cart.reversibility_breakdown}
                band={cart.band}
              />
              <div className="mt-4">
                <BandSeal band={cart.band} />
              </div>
            </div>
          ) : (
            <div className="border border-rule p-8 text-center text-sm text-ink-muted">
              Run a session to see the gauge.
            </div>
          )}
        </section>

        <section aria-labelledby="ledger-heading">
          <h2 id="ledger-heading" className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">
            Ledger
          </h2>
          <LedgerStream events={events} connected={connected} allowBreak />
        </section>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 min-w-0">
      <span className="font-mono text-[10px] uppercase tracking-wider text-ink-muted">{label}</span>
      {children}
    </label>
  );
}
