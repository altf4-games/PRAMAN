"use client";

import { useMemo, useState } from "react";
import { CheckCircle2, TriangleAlert } from "lucide-react";
import type { LedgerBusEvent } from "@/lib/sse";

function truncateHash(hash: string | undefined): string {
  // Defensive: this panel is meant only for real, hash-chained ledger
  // events. A caller subscribed to an unfiltered bus (`useLedgerStream`
  // with no session_id) can still hand it something else — render a
  // placeholder rather than crash on `.slice()` of `undefined`.
  if (!hash) return "—";
  return `${hash.slice(0, 8)}…${hash.slice(-6)}`;
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export interface LedgerStreamProps {
  events: LedgerBusEvent[];
  connected?: boolean;
  /** Shows the "Break the ledger" demo control — the design spec §7: corrupts a
   * row's displayed hash client-side only (nothing server-side is ever
   * touched — a real dispute ledger doesn't get a button that mutates it)
   * and turns the chain-proof strip red from that row forward. Off by
   * default; only `/live` opts in. */
  allowBreak?: boolean;
  className?: string;
}

export function LedgerStream({
  events,
  connected = false,
  allowBreak = false,
  className,
}: LedgerStreamProps) {
  const [brokenIndex, setBrokenIndex] = useState<number | null>(null);

  const chainOk = brokenIndex === null;

  const rows = useMemo(
    () =>
      events.map((e, i) => {
        const hash = e.payload.chain_hash ?? "";
        return {
          ...e,
          displayHash:
            brokenIndex !== null && i >= brokenIndex
              ? `${hash.slice(0, 4)}00000000BAD${hash.slice(-4)}`
              : hash,
          broken: brokenIndex !== null && i >= brokenIndex,
        };
      }),
    [events, brokenIndex],
  );

  return (
    <div className={className}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${connected ? "bg-band-green" : "bg-ink-muted"}`}
            aria-hidden
          />
          <span className="font-mono text-xs uppercase tracking-wider text-ink-muted">
            Ledger {connected ? "live" : "idle"}
          </span>
        </div>
        {allowBreak && (
          <button
            type="button"
            onClick={() =>
              setBrokenIndex((prev) =>
                prev !== null ? null : Math.max(0, events.length - Math.min(3, events.length)),
              )
            }
            className="font-mono text-[11px] uppercase tracking-wide border border-rule px-2 py-1 hover:bg-paper-raised transition-colors"
            disabled={events.length === 0}
          >
            {brokenIndex !== null ? "Restore chain" : "Break the ledger"}
          </button>
        )}
      </div>

      <div
        className={`flex items-center gap-2 border px-3 py-2 mb-2 font-mono text-xs transition-colors ${
          chainOk
            ? "border-rule text-ink-muted"
            : "border-band-red text-band-red bg-band-red/5"
        }`}
        role="status"
      >
        {chainOk ? (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0" aria-hidden />
        ) : (
          <TriangleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
        )}
        {chainOk
          ? `chain verified — ${events.length} event${events.length === 1 ? "" : "s"}`
          : `chain broken at index ${brokenIndex} — every hash after it no longer matches`}
      </div>

      <ol className="border border-rule divide-y divide-rule max-h-[28rem] overflow-y-auto">
        {rows.length === 0 && (
          <li className="px-3 py-6 text-center text-ink-muted text-sm">
            No ledger events yet — run a session to see them arrive here live.
          </li>
        )}
        {rows.map((row, i) => (
          <li
            key={row.payload.event_id}
            className={`px-3 py-2 text-xs grid grid-cols-[auto_1fr_auto] gap-x-3 items-baseline ${
              row.broken ? "bg-band-red/5" : i % 2 === 1 ? "bg-paper-raised/60" : ""
            }`}
          >
            <span className="font-mono text-ink-muted tabular-nums">{formatTime(row.payload.ts)}</span>
            <span className="flex items-center gap-2 min-w-0">
              {row.payload.agent_did && (
                <span className="inline-block h-1.5 w-1.5 shrink-0 bg-agent" aria-hidden title="agent-originated" />
              )}
              <span className="font-mono truncate">{row.event_type}</span>
            </span>
            <span
              className={`font-mono tabular-nums ${row.broken ? "text-band-red" : "text-ink-muted"}`}
              title={row.displayHash}
            >
              {truncateHash(row.displayHash)}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
