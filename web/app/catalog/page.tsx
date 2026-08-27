"use client";

import { useEffect, useState } from "react";
import { api, type MerchantOut, type ReviewProductOut } from "@/lib/api";

/**
 * The confidence review queue. Read-only: there's no REST endpoint yet for
 * actually confirming/correcting a product (that logic only exists inside
 * the WhatsApp state machine's plain-text matching,
 * `whatsapp/onboarding.py::_handle_confirming_items`) — per the design spec's own
 * cut order, this page carries less of the demo than `/live` and
 * `/approvals`, so it stays a view rather than a second, REST-only
 * confirmation flow duplicating WhatsApp's.
 */
export default function CatalogPage() {
  const [merchants, setMerchants] = useState<MerchantOut[]>([]);
  const [merchantId, setMerchantId] = useState("");
  const [queue, setQueue] = useState<ReviewProductOut[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api
      .listMerchants()
      .then((list) => {
        setMerchants(list);
        if (list[0]) setMerchantId(list[0].id);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!merchantId) return;
    setLoading(true);
    api
      .catalogReviewQueue(merchantId)
      .then(setQueue)
      .catch(() => setQueue([]))
      .finally(() => setLoading(false));
  }, [merchantId]);

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6 py-10">
      <h1 className="font-display text-3xl mb-2">Catalog review queue</h1>
      <p className="text-ink-muted max-w-2xl mb-6">
        Every product a low-confidence extraction flagged for a merchant to confirm — never
        exposed to a shopping agent until it&apos;s cleared. Confirming happens over WhatsApp
        (the design spec&apos;s onboarding flow); this page mirrors the queue read-only.
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

      <div className="border border-rule divide-y divide-rule">
        {loading && <p className="p-4 text-sm text-ink-muted">Loading…</p>}
        {!loading && queue.length === 0 && (
          <p className="p-6 text-center text-sm text-ink-muted">
            Nothing awaiting review for this shop right now.
          </p>
        )}
        {queue.map((p) => (
          <div key={p.id} className="p-4 flex flex-col sm:flex-row sm:items-center gap-4">
            <div className="flex-1 min-w-0">
              <p className="font-medium">{p.name}</p>
              <p className="font-mono text-xs text-ink-muted">
                {p.sku} · ₹{(p.unit_price_paise / 100).toFixed(2)} · {p.category} · source: {p.source}
              </p>
              <dl className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2 max-w-md">
                {Object.entries(p.field_confidence).map(([field, value]) => (
                  <div key={field}>
                    <dt className="text-[10px] font-mono uppercase text-ink-muted truncate">
                      {field}
                    </dt>
                    <dd className="h-1.5 bg-rule mt-0.5">
                      <div
                        className={`h-full ${value < 0.75 ? "bg-band-amber" : "bg-band-green"}`}
                        style={{ width: `${Math.round(value * 100)}%` }}
                      />
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
            <div className="flex gap-2 shrink-0">
              <button
                type="button"
                disabled
                title="Confirm over WhatsApp — no REST action for this yet"
                className="border border-rule text-ink-muted px-3 py-1.5 font-mono text-xs uppercase tracking-wide opacity-50 cursor-not-allowed"
              >
                Confirm
              </button>
              <button
                type="button"
                disabled
                title="Edit over WhatsApp — no REST action for this yet"
                className="border border-rule text-ink-muted px-3 py-1.5 font-mono text-xs uppercase tracking-wide opacity-50 cursor-not-allowed"
              >
                Edit
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
