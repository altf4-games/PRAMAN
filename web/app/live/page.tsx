"use client";

import { useEffect, useState } from "react";
import { api, type MerchantOut, type ProductOut, type QuoteOut } from "@/lib/api";
import { signRequest } from "@/lib/sign";
import { useLedgerStream } from "@/lib/sse";
import { ReversibilityGauge } from "@/components/ReversibilityGauge";
import { LedgerStream } from "@/components/LedgerStream";
import { AgentTrace } from "@/components/AgentTrace";
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

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => { open: () => void };
  }
}

/** Loads Razorpay's hosted Checkout.js once and reuses it — the real
 * capture path for this test account, since server-to-server test-card
 * capture isn't enabled on it (confirmed against the live API): a real
 * Razorpay Order exists the moment the gate ALLOWs/HOLDs, but a real
 * Payment only exists once this widget's card form actually runs. */
function loadRazorpayCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://checkout.razorpay.com/v1/checkout.js";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("failed to load Razorpay Checkout.js"));
    document.body.appendChild(script);
  });
}

export default function LivePage() {
  const [merchants, setMerchants] = useState<MerchantOut[]>([]);
  const [merchantId, setMerchantId] = useState<string>("");
  const [products, setProducts] = useState<ProductOut[]>([]);
  const [productId, setProductId] = useState<string>("");
  const [ceiling, setCeiling] = useState(500_000);
  const [minReversibility, setMinReversibility] = useState(0);

  const [mode, setMode] = useState<"scripted" | "agent">("agent");
  const [goal, setGoal] = useState("Buy 1kg toor dal");

  const [running, setRunning] = useState(false);
  const [calls, setCalls] = useState<ToolCall[]>([]);
  const [cart, setCart] = useState<CartResult | null>(null);
  const [checkout, setCheckout] = useState<CheckoutResult | null>(null);
  const [userRef, setUserRef] = useState<string>("");
  const [postAction, setPostAction] = useState<string | null>(null);
  const [payingLive, setPayingLive] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);

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

  const { events: rawEvents, connected } = useLedgerStream(
    cart ? `cart:${cart.cart_id}` : null,
  );
  // Before a cart exists, useLedgerStream(null) subscribes to the *entire*
  // bus (see its own docstring) -- harmless when only real, chain-hashed
  // ledger events existed, but this page also now publishes agent-trace
  // events (AGENT_THOUGHT etc.) onto the same bus, which carry no
  // chain_hash. A real crash this caused: LedgerStream's truncateHash
  // reading `.slice()` off an undefined chain_hash the moment an agent
  // run started. Filtered here rather than widening LedgerStream to
  // tolerate non-ledger payloads it was never meant to render.
  const events = rawEvents.filter((e) => typeof e.payload.chain_hash === "string");
  const { events: agentEvents, connected: agentConnected } = useLedgerStream(
    agentRunId ? `agent:${agentRunId}` : null,
  );

  // The agent's own tool results are the only place a cart's reversibility
  // band/score/breakdown and the checkout decision are known in Agent mode
  // — `POST /api/agent/run` only returns a final summary, not that detail,
  // so this page reconstructs live state from the same SSE trace it's
  // already rendering, rather than adding a second round-trip.
  useEffect(() => {
    if (mode !== "agent") return;
    for (const e of agentEvents) {
      if (e.event_type !== "AGENT_TOOL_RESULT") continue;
      const p = e.payload as unknown as { tool: string; result: Record<string, unknown> };
      if (p.tool === "cart_confirm" && typeof p.result.cart_id === "string") {
        setCart({
          cart_id: p.result.cart_id,
          band: p.result.band as CartResult["band"],
          reversibility_score: p.result.reversibility_score as number,
          reversibility_breakdown: p.result.reversibility_breakdown as Record<string, number>,
        });
      }
      if (p.tool === "checkout_execute" && typeof p.result.decision === "string") {
        setCheckout({
          decision: p.result.decision as string,
          reason_code: (p.result.reason_code as string) ?? "",
          detail: (p.result.detail as string) ?? "",
          order_id: (p.result.order_id as string) ?? null,
          order_status: (p.result.order_status as string) ?? null,
        });
      }
    }
  }, [agentEvents, mode]);

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

  /** The genuine agent path: a live Gemini model, given only the buyer's
   * goal in natural language, decides for itself which tools to call and
   * in what order (`POST /api/agent/run`, `agent_runner/runner.py`) — no
   * scripted call sequence, no button-per-step. Every decision and tool
   * call streams live over SSE via `agentEvents` above. */
  async function runAgentDecides() {
    if (!merchantId) return;
    setRunning(true);
    setCart(null);
    setCheckout(null);
    setPostAction(null);
    setAgentError(null);
    const ref = `web-buyer-${Date.now().toString(36)}`;
    setUserRef(ref);
    const runId =
      typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    setAgentRunId(runId);

    const merchant = merchants.find((m) => m.id === merchantId);
    const categories = [...new Set(products.map((p) => p.category))];

    // Give the SSE subscription (the effect watching `agentRunId`) a beat
    // to connect before the run starts publishing — otherwise the first
    // few events could fire before this tab is listening.
    await new Promise((r) => setTimeout(r, 300));

    try {
      await api.agentRun({
        run_id: runId,
        goal,
        merchant_id: merchantId,
        merchant_name: merchant?.name ?? "the shop",
        user_ref: ref,
        user_whatsapp: "whatsapp:+919000000000",
        ceiling_paise: ceiling,
        max_single_txn_paise: ceiling,
        allowed_categories: categories,
        min_reversibility: minReversibility,
      });
    } catch (err) {
      setAgentError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
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

  /** Opens Razorpay's real Checkout.js widget for an order the gate
   * already ALLOW'd/HOLD'd but that's sitting in `awaiting_payment(_amber)`
   * — a genuine Razorpay Order the backend created, waiting on a genuine
   * captured Payment only a browser can produce. On success this POSTs
   * the browser's signed callback to `/checkout/{order_id}/confirm`,
   * which verifies it server-side before dispatching/holding the order —
   * the widget's own "success" callback is never trusted on its own. */
  async function payWithRazorpay() {
    if (!checkout?.order_id) return;
    setPayError(null);
    setPayingLive(true);
    try {
      const order = await api.orderStatus(checkout.order_id);
      if (!order.razorpay_order_id || !order.amount_paise || !order.razorpay_key_id) {
        throw new Error("order is missing real-checkout details");
      }
      await loadRazorpayCheckout();
      if (!window.Razorpay) throw new Error("Razorpay Checkout.js did not load");

      await new Promise<void>((resolve, reject) => {
        const rzp = new window.Razorpay!({
          key: order.razorpay_key_id,
          order_id: order.razorpay_order_id,
          amount: order.amount_paise,
          currency: "INR",
          name: "PRAMAN",
          description: `cart ${order.cart_id}`,
          prefill: { contact: "9999999999", email: "checkout@praman.dev" },
          theme: { color: "#141A22" },
          handler: async (response: {
            razorpay_order_id: string;
            razorpay_payment_id: string;
            razorpay_signature: string;
          }) => {
            try {
              await api.checkoutConfirm(checkout.order_id!, response);
              resolve();
            } catch (err) {
              reject(err instanceof Error ? err : new Error(String(err)));
            }
          },
          modal: { ondismiss: () => reject(new Error("checkout dismissed")) },
        });
        rzp.open();
      });

      const status = await api.orderStatus(checkout.order_id);
      setCheckout((prev) => (prev ? { ...prev, order_status: status.status } : prev));
      setPostAction(
        status.status === "captured"
          ? "Real Razorpay payment captured."
          : `Payment confirmed but order ended up in status: ${status.status}.`,
      );
    } catch (err) {
      setPayError(err instanceof Error ? err.message : String(err));
    } finally {
      setPayingLive(false);
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
      <p className="text-ink-muted max-w-2xl mb-4">
        {mode === "agent"
          ? "A live model decides what to do from a plain-language goal — it picks the tools, the order, and the arguments itself. Nothing below is scripted."
          : "Pick a shop and a product, set an envelope, and run a real signed agent session against the live API — every step below is a genuine HTTP call, not a simulation."}
      </p>

      <div className="flex gap-2 mb-4" role="tablist" aria-label="Agent mode">
        {(["agent", "scripted"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            onClick={() => setMode(m)}
            className={`font-mono text-xs uppercase tracking-wide px-3 py-1.5 border transition-colors ${
              mode === m
                ? "border-ink bg-ink text-paper"
                : "border-rule text-ink-muted hover:bg-paper-raised"
            }`}
          >
            {m === "agent" ? "Agent decides" : "Scripted"}
          </button>
        ))}
      </div>

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
        {mode === "agent" ? (
          <Field label="Goal (plain language)">
            <input
              type="text"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="Buy 1kg toor dal"
              className="w-full border border-rule bg-paper px-2 py-1.5 text-sm"
            />
          </Field>
        ) : (
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
        )}
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
          onClick={mode === "agent" ? runAgentDecides : runSession}
          disabled={running || !merchantId || (mode === "scripted" && !productId)}
          className="border border-ink bg-ink text-paper px-4 py-2 font-mono text-sm uppercase tracking-wide hover:bg-ink/85 disabled:opacity-40 transition-colors"
        >
          {running
            ? "Running…"
            : mode === "agent"
              ? "Let the agent decide"
              : "Run agent session"}
        </button>
        {mode === "agent" && agentError && (
          <p className="sm:col-span-2 lg:col-span-5 text-xs text-band-red font-mono">
            {agentError}
          </p>
        )}
      </div>

      <div className="grid lg:grid-cols-3 gap-8">
        <section aria-labelledby="calls-heading">
          <h2 id="calls-heading" className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">
            {mode === "agent" ? "Agent reasoning" : "Tool calls"}
          </h2>
          {mode === "agent" ? (
            <AgentTrace events={agentEvents} connected={agentConnected} />
          ) : (
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
          )}

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
              {(checkout.order_status === "awaiting_payment" ||
                checkout.order_status === "awaiting_payment_amber") &&
                !postAction && (
                  <div className="mt-2">
                    <p className="text-xs text-ink-muted mb-1">
                      Real Razorpay order created; S2S test-card capture isn&apos;t enabled on
                      this account, so a real payment needs a real Checkout.js round-trip — use
                      Razorpay&apos;s <strong>domestic</strong> Mastercard test card{" "}
                      <span className="font-mono">5267 3181 8797 5449</span>, any future
                      expiry/CVV, any OTP. (
                      <span className="font-mono">4111 1111 1111 1111</span> is flagged as an
                      international card and this account has those disabled — it&apos;ll fail
                      with &quot;international cards not allowed&quot;.)
                    </p>
                    <button
                      type="button"
                      onClick={payWithRazorpay}
                      disabled={payingLive}
                      className="border border-ink px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-paper-raised disabled:opacity-40 transition-colors"
                    >
                      {payingLive ? "Opening Razorpay…" : "Pay with Razorpay (real)"}
                    </button>
                    {payError && (
                      <p className="mt-1 text-xs text-band-red font-mono">{payError}</p>
                    )}
                  </div>
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
