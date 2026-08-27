"use client";

import { useEffect, useState } from "react";
import {
  bandColor,
  bandFromScore,
  bandLabel,
  REVERSIBILITY_FACTORS,
  type Band,
} from "@/lib/tokens";

export interface ReversibilityGaugeProps {
  /** 0..1, as returned by `reversibility_score_detailed`. */
  score: number;
  /** The five named factors — f_return, f_class, f_speed, f_restock, f_value.
   * Values are the raw per-factor score (0..1), not yet weighted; this
   * component applies the weight to size each segment. */
  breakdown: Record<string, number>;
  /** Overrides the band derived from `score` — pass this when the caller
   * (e.g. a live gate decision) has an authoritative band that should win
   * over a client-side recomputation using possibly-stale thresholds. */
  band?: Band;
  /** Hides the seal even on a non-green band — used when embedding a small
   * read-only gauge (e.g. a ledger row) where the stamp would be noise. */
  hideSeal?: boolean;
  className?: string;
}

/**
 * The signature element (CLAUDE.md §7): a horizontal meter, 0→1, with the
 * five reversibility factors as stacked, labelled segments. Crossing below
 * the green threshold stamps a bordered mono seal onto the gauge reading
 * "{BAND} · {WHAT HAPPENS}" — the single most memorable frame in the demo
 * video, so it's the first component built and the one held to the
 * highest bar.
 *
 * Ink on paper: every colour here is either ink/paper/rule (chrome) or the
 * one live band colour (green/amber/red) — never anything else.
 */
export function ReversibilityGauge({
  score,
  breakdown,
  band,
  hideSeal = false,
  className,
}: ReversibilityGaugeProps) {
  const resolvedBand = band ?? bandFromScore(score);
  const color = bandColor[resolvedBand];
  const showSeal = !hideSeal && resolvedBand !== "green";

  // Stamp only after the fill has had a beat to land — keeps the "seal
  // slams down" moment legible instead of everything firing at once.
  const [stampVisible, setStampVisible] = useState(false);
  useEffect(() => {
    setStampVisible(false);
    if (!showSeal) return;
    const t = setTimeout(() => setStampVisible(true), 450);
    return () => clearTimeout(t);
  }, [showSeal, resolvedBand, score]);

  // A fixed, deterministic-looking "hand stamped" tilt per band, not
  // random — random rotation would make the same decision look different
  // on every render, undermining the point of a stamp being a fixed record.
  const stampRotate = resolvedBand === "red" ? "-4deg" : "3deg";

  return (
    <div className={className} data-testid="reversibility-gauge">
      <div className="flex items-baseline justify-between mb-2">
        <span className="font-mono text-xs uppercase tracking-wider text-ink-muted">
          Reversibility
        </span>
        <span className="font-mono text-sm tabular-nums" style={{ color }}>
          {score.toFixed(3)}
        </span>
      </div>

      <div className="relative">
        {/* the meter track */}
        <div className="relative h-8 w-full border border-rule bg-paper-raised overflow-hidden flex">
          {REVERSIBILITY_FACTORS.map((factor, i) => {
            const raw = breakdown[factor.key] ?? 0;
            const widthPct = Math.max(0, Math.min(1, raw * factor.weight)) * 100;
            return (
              <div
                key={factor.key}
                className="group relative h-full gauge-segment-animate border-r border-paper last:border-r-0"
                style={{
                  width: `${widthPct}%`,
                  backgroundColor: color,
                  opacity: 0.35 + i * 0.13,
                  animationDelay: `${i * 60}ms`,
                }}
                title={`${factor.label}: ${raw.toFixed(2)} × weight ${factor.weight}`}
              />
            );
          })}
        </div>

        {/* threshold ticks at 0.40 and 0.75 */}
        <div
          className="absolute top-0 h-8 w-px bg-ink-muted/50"
          style={{ left: "40%" }}
          aria-hidden
        />
        <div
          className="absolute top-0 h-8 w-px bg-ink-muted/50"
          style={{ left: "75%" }}
          aria-hidden
        />

        {stampVisible && (
          <div
            className="gauge-stamp-animate absolute inset-0 flex items-center justify-center pointer-events-none"
            style={{ ["--stamp-rotate" as string]: stampRotate }}
          >
            <span
              className="font-mono text-[11px] sm:text-xs font-semibold uppercase tracking-[0.14em] border-2 px-3 py-1 bg-paper/95 shadow-sm"
              style={{ color, borderColor: color, transform: `rotate(${stampRotate})` }}
              role="status"
            >
              {bandLabel[resolvedBand]}
            </span>
          </div>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {REVERSIBILITY_FACTORS.map((factor) => (
          <div key={factor.key} className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <span
              className="inline-block h-2 w-2 border border-rule"
              style={{ backgroundColor: color, opacity: 0.6 }}
              aria-hidden
            />
            <span className="font-mono">{factor.label}</span>
            <span className="font-mono tabular-nums">
              {((breakdown[factor.key] ?? 0)).toFixed(2)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
