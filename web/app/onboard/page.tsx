"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type MerchantOut, type ReviewProductOut } from "@/lib/api";
import { useLedgerStream } from "@/lib/sse";
import { WhatsAppThread, type WhatsAppMessageItem } from "@/components/WhatsAppThread";

const TELEGRAM_BOT_USERNAME = process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME || "";
const TWILIO_NUMBER = process.env.NEXT_PUBLIC_TWILIO_WHATSAPP_NUMBER || "";

const STATE_STEPS = [
  "NEW",
  "AWAITING_MEDIA",
  "EXTRACTING",
  "CONFIRMING_ITEMS",
  "SETTING_POLICY",
  "LIVE",
] as const;

export default function OnboardPage() {
  const [merchants, setMerchants] = useState<MerchantOut[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [reviewQueue, setReviewQueue] = useState<ReviewProductOut[]>([]);
  const [clearCount, setClearCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const list = await api.listMerchants();
        if (cancelled) return;
        setMerchants(list);
        setSelectedId((prev) => prev ?? list[0]?.id ?? null);
      } catch {
        // API may be cold-starting; the next tick retries.
      }
    }
    poll();
    const interval = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const selected = merchants.find((m) => m.id === selectedId) ?? null;
  const { events } = useLedgerStream(selected ? `onboarding:${selected.id}` : null);

  const messages: WhatsAppMessageItem[] = useMemo(
    () =>
      events
        .filter((e) => e.event_type === "WHATSAPP_INBOUND" || e.event_type === "WHATSAPP_OUTBOUND")
        .map((e) => ({
          direction: e.event_type === "WHATSAPP_INBOUND" ? "inbound" : "outbound",
          body: String(e.payload.body ?? ""),
          ts: e.payload.ts,
          delivered: e.payload.delivered as boolean | undefined,
        })),
    [events],
  );

  useEffect(() => {
    const extracted = events.find((e) => e.event_type === "CATALOG_EXTRACTED");
    if (extracted) {
      setClearCount(Number(extracted.payload.clear ?? 0));
    }
  }, [events]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    async function fetchQueue() {
      try {
        const queue = await api.catalogReviewQueue(selected!.id);
        if (!cancelled) setReviewQueue(queue);
      } catch {
        // ignore transient errors — next poll retries
      }
    }
    fetchQueue();
    const interval = setInterval(fetchQueue, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [selected]);

  const currentStepIndex = selected ? STATE_STEPS.indexOf(selected.onboarding_state as (typeof STATE_STEPS)[number]) : -1;

  return (
    <div className="mx-auto max-w-6xl px-4 sm:px-6 py-10">
      <h1 className="font-display text-3xl mb-2">Onboard a shop</h1>
      <p className="text-ink-muted max-w-2xl mb-6">
        Text a photo of a price list to the sandbox number below from a real phone, then pick
        the shop that appears here to watch the whole thing happen live.
      </p>

      <div className="border border-rule bg-paper-raised px-4 py-3 mb-3 flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs uppercase tracking-wider text-ink-muted">Telegram</span>
        {TELEGRAM_BOT_USERNAME ? (
          <a
            href={`https://t.me/${TELEGRAM_BOT_USERNAME}`}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-sm underline"
          >
            @{TELEGRAM_BOT_USERNAME}
          </a>
        ) : (
          <span className="font-mono text-sm text-ink-muted">
            (configure NEXT_PUBLIC_TELEGRAM_BOT_USERNAME)
          </span>
        )}
        <span className="text-xs text-ink-muted">
          — primary channel: Twilio&apos;s trial account blocks real photo delivery entirely
          (see README), Telegram doesn&apos;t
        </span>
      </div>

      {TWILIO_NUMBER && (
        <div className="border border-rule px-4 py-3 mb-8 flex flex-wrap items-center gap-2 opacity-70">
          <span className="font-mono text-xs uppercase tracking-wider text-ink-muted">
            WhatsApp sandbox (legacy)
          </span>
          <span className="font-mono text-sm">{TWILIO_NUMBER}</span>
          <span className="text-xs text-ink-muted">
            — still wired up, but photo delivery is blocked on this Twilio account tier
          </span>
        </div>
      )}

      <div className="mb-6 flex items-center gap-3">
        <label htmlFor="merchant-picker" className="font-mono text-xs uppercase tracking-wider text-ink-muted">
          Watching
        </label>
        <select
          id="merchant-picker"
          value={selectedId ?? ""}
          onChange={(e) => setSelectedId(e.target.value)}
          className="border border-rule bg-paper px-2 py-1.5 text-sm font-mono min-w-0 max-w-full"
        >
          {merchants.length === 0 && <option value="">No shops yet</option>}
          {merchants.map((m) => (
            <option key={m.id} value={m.id}>
              {m.whatsapp_number} — {m.onboarding_state}
            </option>
          ))}
        </select>
      </div>

      {selected && (
        <ol className="flex flex-wrap items-center gap-1 mb-8 font-mono text-[11px] uppercase tracking-wide">
          {STATE_STEPS.map((step, i) => (
            <li key={step} className="flex items-center gap-1">
              <span
                className={`px-2 py-1 border ${
                  i <= currentStepIndex
                    ? "border-ink text-ink bg-paper-raised"
                    : "border-rule text-ink-muted"
                }`}
              >
                {step}
              </span>
              {i < STATE_STEPS.length - 1 && <span className="text-ink-muted">→</span>}
            </li>
          ))}
        </ol>
      )}

      <div className="grid lg:grid-cols-2 gap-8">
        <section aria-labelledby="thread-heading">
          <h2 id="thread-heading" className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">
            Message thread
          </h2>
          <div className="border border-rule bg-paper p-3 min-h-[24rem] max-h-[36rem] overflow-y-auto">
            <WhatsAppThread messages={messages} />
          </div>
        </section>

        <section aria-labelledby="catalog-heading">
          <h2 id="catalog-heading" className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">
            Catalog materialising {clearCount > 0 && `— ${clearCount} clear`}
          </h2>
          <div className="border border-rule bg-paper min-h-[24rem] max-h-[36rem] overflow-y-auto divide-y divide-rule">
            {reviewQueue.length === 0 ? (
              <p className="p-4 text-sm text-ink-muted">
                Nothing awaiting review yet — items will appear here as they&apos;re extracted.
              </p>
            ) : (
              reviewQueue.map((p) => (
                <div key={p.id} className="p-3 flex items-center gap-3 opacity-70">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium truncate">{p.name}</p>
                    <p className="font-mono text-xs text-ink-muted">
                      ₹{(p.unit_price_paise / 100).toFixed(2)} · {p.category}
                    </p>
                    <ConfidenceBars confidence={p.field_confidence} />
                  </div>
                  <span className="font-mono text-[10px] uppercase text-band-amber border border-band-amber px-1.5 py-0.5 shrink-0">
                    awaiting merchant
                  </span>
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function ConfidenceBars({ confidence }: { confidence: Record<string, number> }) {
  const entries = Object.entries(confidence).slice(0, 4);
  if (entries.length === 0) return null;
  return (
    <div className="mt-1.5 flex gap-1" aria-hidden>
      {entries.map(([key, value]) => (
        <div key={key} className="h-1 flex-1 bg-rule" title={`${key}: ${value.toFixed(2)}`}>
          <div
            className="h-full bg-band-amber"
            style={{ width: `${Math.round(value * 100)}%` }}
          />
        </div>
      ))}
    </div>
  );
}
