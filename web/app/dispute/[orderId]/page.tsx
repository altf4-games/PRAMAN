import { notFound } from "next/navigation";
import { API_BASE_URL, ApiError, api } from "@/lib/api";
import { ReversibilityGauge } from "@/components/ReversibilityGauge";
import { BandSeal } from "@/components/BandSeal";
import { GateTrail } from "@/components/GateTrail";
import { DownloadJsonButton } from "@/components/DownloadJsonButton";

export const dynamic = "force-dynamic";

function money(paise: number): string {
  return `₹${(paise / 100).toFixed(2)}`;
}

export default async function DisputePage({
  params,
}: PageProps<"/dispute/[orderId]">) {
  const { orderId } = await params;

  let order;
  try {
    order = await api.orderStatus(orderId);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) notFound();
    throw err;
  }

  const pack = await api.disputePack(order.cart_id);

  const envelope = pack.envelope as {
    envelope_id?: string;
    agent_did?: string;
    ceiling_paise?: number;
    max_single_txn_paise?: number;
    allowed_categories?: string[];
    valid_from?: string;
    valid_until?: string;
    signature?: string;
  };
  const cartMandate = pack.cart_mandate as {
    items?: Array<{ sku: string; qty: number; unit_price_paise: number }>;
    total_paise?: number;
    agent_sig?: string;
  };
  // Older, still-deployed backends don't return these fields yet -- default
  // to empty/undefined rather than crashing on a stale API response.
  const quoteProvenance = pack.quote_provenance ?? [];

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6 py-10">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <p className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-1">
            Dispute pack
          </p>
          <h1 className="font-display text-3xl">Order {order.id}</h1>
        </div>
        <div className="flex gap-2">
          <DownloadJsonButton data={pack} filename={`dispute-pack-${order.id}.json`} />
          <a
            href={`${API_BASE_URL}/api/dispute-pack/${order.cart_id}/pdf`}
            className="border border-ink px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-paper-raised transition-colors"
          >
            Export PDF
          </a>
        </div>
      </div>

      <section className="border border-rule p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted">
            Reversibility at time of decision
          </h2>
          <BandSeal band={pack.band} />
        </div>
        <ReversibilityGauge score={cartScore(pack)} breakdown={pack.reversibility_breakdown} band={pack.band} hideSeal />
      </section>

      <div className="grid sm:grid-cols-2 gap-6 mb-6">
        <section className="border border-rule p-4">
          <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-2">
            Intent envelope
          </h2>
          <dl className="text-sm space-y-1">
            <Row k="agent" v={envelope.agent_did ?? "—"} mono />
            <Row k="ceiling" v={envelope.ceiling_paise ? money(envelope.ceiling_paise) : "—"} />
            <Row
              k="max single txn"
              v={envelope.max_single_txn_paise ? money(envelope.max_single_txn_paise) : "—"}
            />
            <Row k="categories" v={(envelope.allowed_categories ?? []).join(", ") || "—"} />
            <Row k="valid" v={`${envelope.valid_from ?? "—"} → ${envelope.valid_until ?? "—"}`} />
          </dl>
        </section>

        <section className="border border-rule p-4">
          <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-2">
            Cart mandate
          </h2>
          <dl className="text-sm space-y-1 mb-2">
            {(cartMandate.items ?? []).map((item, i) => (
              <Row key={i} k={item.sku} v={`${item.qty} × ${money(item.unit_price_paise)}`} />
            ))}
          </dl>
          <p className="font-mono text-sm border-t border-rule pt-2">
            total {cartMandate.total_paise ? money(cartMandate.total_paise) : "—"}
          </p>
        </section>
      </div>

      {quoteProvenance.length > 0 && (
        <section className="border border-rule p-4 mb-6">
          <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-2">
            Quote provenance
          </h2>
          <dl className="text-sm space-y-1">
            {quoteProvenance.map((q, i) => (
              <Row
                key={i}
                k={String(q.sku ?? "?")}
                v={`${q.qty} × ${money(Number(q.unit_price_paise ?? 0))} · issued ${q.issued_at}`}
              />
            ))}
          </dl>
        </section>
      )}

      {pack.order && (
        <section className="border border-rule p-4 mb-6">
          <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-2">
            Cooling-off / dispatch timeline
          </h2>
          <dl className="text-sm space-y-1">
            <Row k="status" v={String((pack.order as Record<string, unknown>).status ?? "—")} />
            <Row
              k="cooling_off_until"
              v={String((pack.order as Record<string, unknown>).cooling_off_until ?? "—")}
            />
            <Row
              k="dispatched_at"
              v={String((pack.order as Record<string, unknown>).dispatched_at ?? "—")}
            />
            <Row
              k="cancelled_at"
              v={String((pack.order as Record<string, unknown>).cancelled_at ?? "—")}
            />
            <Row
              k="refunded_at"
              v={String((pack.order as Record<string, unknown>).refunded_at ?? "—")}
            />
            <Row
              k="step-up channel"
              v={String((pack.order as Record<string, unknown>).stepup_channel ?? "—")}
            />
            <Row
              k="step-up confirmed"
              v={String((pack.order as Record<string, unknown>).stepup_confirmed_at ?? "—")}
            />
          </dl>
        </section>
      )}

      <section className="mb-6">
        <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-2">
          Gate trail
        </h2>
        <GateTrail entries={pack.gate_trail as never} />
      </section>

      <section>
        <div className="flex items-center gap-2 mb-2">
          <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted">
            Chain proof
          </h2>
          <span
            className={`font-mono text-[10px] uppercase px-1.5 py-0.5 border ${
              pack.ledger.chain_verified
                ? "border-band-green text-band-green"
                : "border-band-red text-band-red"
            }`}
          >
            {pack.ledger.chain_verified ? "verified" : "broken"}
          </span>
        </div>
        <ol className="border border-rule divide-y divide-rule max-h-96 overflow-y-auto">
          {pack.ledger.events.map((e) => (
            <li key={e.event_id} className="px-3 py-2 text-xs grid grid-cols-[1fr_auto] gap-3">
              <span className="font-mono truncate">{e.event_type}</span>
              <span className="font-mono text-ink-muted tabular-nums">
                {e.chain_hash.slice(0, 8)}…{e.chain_hash.slice(-6)}
              </span>
            </li>
          ))}
        </ol>
      </section>

      <section className="mt-6 pt-4 border-t border-rule">
        <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-2">
          Pack signature
        </h2>
        <dl className="text-sm space-y-1">
          <Row k="pack_hash" v={pack.pack_hash ?? "—"} mono />
          <Row k="merchant" v={pack.merchant_did ?? "—"} mono />
          <Row
            k="signature"
            v={pack.merchant_signature ? `${pack.merchant_signature.slice(0, 24)}…` : "unsigned"}
            mono
          />
        </dl>
      </section>
    </div>
  );
}

function cartScore(pack: { reversibility_breakdown: Record<string, number> }): number {
  // The pack stores the breakdown, not the raw scalar score, since that's
  // what CartMandate persists — reconstruct it here the same way the gate
  // does, purely for display (never re-used for a decision).
  const b = pack.reversibility_breakdown;
  return (
    0.35 * (b.f_unwind ?? 0) +
    0.25 * (b.f_class ?? 0) +
    0.15 * (b.f_speed ?? 0) +
    0.1 * (b.f_restock ?? 0) +
    0.15 * (b.f_value ?? 0)
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
