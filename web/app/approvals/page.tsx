"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type ApprovalOut, type MerchantOut } from "@/lib/api";
import { ReversibilityGauge } from "@/components/ReversibilityGauge";
import { BandSeal } from "@/components/BandSeal";

function useCountdown(deadline: string): string {
  const [label, setLabel] = useState("");
  useEffect(() => {
    function tick() {
      const ms = new Date(deadline).getTime() - Date.now();
      if (ms <= 0) {
        setLabel("expired");
        return;
      }
      const minutes = Math.floor(ms / 60000);
      const seconds = Math.floor((ms % 60000) / 1000);
      setLabel(`${minutes}:${seconds.toString().padStart(2, "0")}`);
    }
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [deadline]);
  return label;
}

/**
 * Mirrors the merchant's WhatsApp inbox (the design spec §7) — every action here
 * calls the exact same `_approve`/`_decline` path a WhatsApp reply does
 * (`whatsapp/approvals.py::decide_by_order_id`), so the two stay in sync.
 */
export default function ApprovalsPage() {
  const [merchants, setMerchants] = useState<MerchantOut[]>([]);
  const [merchantId, setMerchantId] = useState("");
  const [inbox, setInbox] = useState<ApprovalOut[]>([]);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listMerchants()
      .then((list) => {
        setMerchants(list);
        if (list[0]) setMerchantId(list[0].id);
      })
      .catch(() => undefined);
  }, []);

  const refresh = useCallback(() => {
    if (!merchantId) return;
    api
      .approvalsInbox(merchantId)
      .then(setInbox)
      .catch(() => setInbox([]));
  }, [merchantId]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  async function decide(orderId: string, decision: "approve" | "decline") {
    setDeciding(orderId);
    setError(null);
    try {
      await api.approvalsDecide(orderId, decision);
      refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeciding(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6 py-10">
      <h1 className="font-display text-3xl mb-2">Merchant approvals</h1>
      <p className="text-ink-muted max-w-2xl mb-6">
        Every order the gate escalated (R08 reversibility or R11 trust-tier ceiling), waiting
        on a human. Deciding here or replying on the merchant&rsquo;s own chat (WhatsApp or
        Telegram) produce the identical outcome.
      </p>

      <div className="mb-6">
        <label className="font-mono text-xs uppercase tracking-wider text-ink-muted mr-2">
          Shop
        </label>
        <select
          value={merchantId}
          onChange={(e) => setMerchantId(e.target.value)}
          className="border border-rule bg-paper px-2 py-1.5 text-sm font-mono"
        >
          {merchants.length === 0 && <option value="">No shops yet</option>}
          {merchants.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <p className="mb-4 border border-band-red text-band-red px-3 py-2 text-sm font-mono">
          {error}
        </p>
      )}

      <div className="flex flex-col gap-4">
        {inbox.length === 0 && (
          <p className="border border-rule p-6 text-center text-sm text-ink-muted">
            Nothing pending approval right now.
          </p>
        )}
        {inbox.map((a) => (
          <ApprovalCard
            key={a.order_id}
            approval={a}
            busy={deciding === a.order_id}
            onDecide={(d) => decide(a.order_id, d)}
          />
        ))}
      </div>
    </div>
  );
}

function ApprovalCard({
  approval,
  busy,
  onDecide,
}: {
  approval: ApprovalOut;
  busy: boolean;
  onDecide: (decision: "approve" | "decline") => void;
}) {
  const countdown = useCountdown(approval.deadline);
  return (
    <div className="border border-rule p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
        <div>
          <p className="font-medium">{approval.item_summary}</p>
          <p className="font-mono text-xs text-ink-muted">
            ₹{(approval.total_paise / 100).toFixed(2)} · {approval.reason_code}
          </p>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <BandSeal band={approval.band} />
          <span className="font-mono text-xs text-ink-muted tabular-nums">{countdown}</span>
        </div>
      </div>

      <ReversibilityGauge
        score={approval.reversibility_score}
        breakdown={approval.reversibility_breakdown}
        band={approval.band}
        hideSeal
      />

      <div className="mt-4 flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide("approve")}
          className="border border-band-green text-band-green px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-band-green/10 disabled:opacity-40 transition-colors"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide("decline")}
          className="border border-band-red text-band-red px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-band-red/10 disabled:opacity-40 transition-colors"
        >
          Decline
        </button>
      </div>
    </div>
  );
}
