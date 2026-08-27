import { bandColor, bandLabel, type Band } from "@/lib/tokens";

/** A small standalone band pill — for tables, lists, and inline mentions
 * where the full `ReversibilityGauge` would be too much. Same rule as the
 * gauge: the band colour is the only saturated colour used here. */
export function BandSeal({ band, className }: { band: Band; className?: string }) {
  const color = bandColor[band];
  return (
    <span
      className={`inline-flex items-center border font-mono text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 ${className ?? ""}`}
      style={{ color, borderColor: color }}
    >
      {bandLabel[band]}
    </span>
  );
}
