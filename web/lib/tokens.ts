/**
 * Design tokens — the design spec §7, mirrored from app/globals.css. This file
 * exists because one place in the product (ReversibilityGauge's SVG fills/
 * strokes) needs literal colour strings rather than a Tailwind class name;
 * everywhere else should reach for the Tailwind utilities (bg-paper,
 * text-band-green, etc.) generated from globals.css's @theme block, not
 * this object. Keep the two in sync by hand — there are only nine colours.
 *
 * Ink on paper. The reversibility band (green/amber/red) is the ONLY
 * saturated colour anywhere in this product — never decorative, never used
 * for anything else. Do not introduce a fourth accent.
 */

export const colors = {
  paper: "#EDEEEA",
  paperRaised: "#F6F7F3",
  ink: "#141A22",
  inkMuted: "#5A6472",
  rule: "#C9CCC4",
  agent: "#2D4A7C",
  bandGreen: "#1F7A4C",
  bandAmber: "#B8791A",
  bandRed: "#A32C2C",
} as const;

export type Band = "green" | "amber" | "red";

export const bandColor: Record<Band, string> = {
  green: colors.bandGreen,
  amber: colors.bandAmber,
  red: colors.bandRed,
};

export const bandLabel: Record<Band, string> = {
  green: "GREEN · FULL AUTONOMY",
  amber: "AMBER · COOLING-OFF",
  red: "RED · STEP-UP REQUIRED",
};

// the design spec §5 — named constants, never inline literals, mirrored from the
// API's own praman/config.py so the frontend's copy of the thresholds can
// never silently drift from what the gate actually enforces.
export const BAND_GREEN_THRESHOLD = 0.75;
export const BAND_AMBER_THRESHOLD = 0.4;

export function bandFromScore(score: number): Band {
  if (score >= BAND_GREEN_THRESHOLD) return "green";
  if (score >= BAND_AMBER_THRESHOLD) return "amber";
  return "red";
}

export const REVERSIBILITY_FACTORS = [
  { key: "f_return", label: "Return window", weight: 0.35 },
  { key: "f_class", label: "Category class", weight: 0.25 },
  { key: "f_speed", label: "Fulfilment speed", weight: 0.15 },
  { key: "f_restock", label: "Restocking cost", weight: 0.1 },
  { key: "f_value", label: "Value vs ceiling", weight: 0.15 },
] as const;
