import { Badge } from "@/components/ui/badge";

export interface GateTrailEntry {
  decision: string;
  reason_code: string;
  rule_id: string | null;
  detail: string;
  remedy: string;
  evaluated_at?: string;
  latency_ms?: number;
}

const DECISION_TONE: Record<string, string> = {
  ALLOW: "text-band-green border-band-green",
  HOLD: "text-band-amber border-band-amber",
  ESCALATE: "text-band-amber border-band-amber",
  SUBSTITUTE: "text-agent border-agent",
  BLOCK: "text-band-red border-band-red",
};

/** The ordered R01-R12 policy-gate trail — every rule the request was
 * checked against, first non-ALLOW wins (the design spec §5). Used on `/live`
 * (a live decision) and `/dispute/[orderId]` (the full historical trail
 * from `GateDecision` rows). */
export function GateTrail({ entries }: { entries: GateTrailEntry[] }) {
  if (entries.length === 0) {
    return (
      <p className="text-sm text-ink-muted font-mono">No gate decisions yet for this session.</p>
    );
  }

  return (
    <ol className="border border-rule divide-y divide-rule">
      {entries.map((entry, i) => (
        <li key={i} className="px-3 py-2.5">
          <div className="flex items-center justify-between gap-2 mb-1">
            <div className="flex items-center gap-2 min-w-0">
              {entry.rule_id && (
                <span className="font-mono text-[10px] text-ink-muted border border-rule px-1 shrink-0">
                  {entry.rule_id}
                </span>
              )}
              <Badge
                variant="outline"
                className={`font-mono text-[10px] uppercase ${DECISION_TONE[entry.decision] ?? ""}`}
              >
                {entry.decision}
              </Badge>
              <span className="font-mono text-xs text-ink-muted truncate">{entry.reason_code}</span>
            </div>
            {entry.latency_ms !== undefined && (
              <span className="font-mono text-[10px] text-ink-muted tabular-nums shrink-0">
                {entry.latency_ms.toFixed(1)}ms
              </span>
            )}
          </div>
          <p className="text-sm text-ink">{entry.detail}</p>
          {entry.remedy && (
            <p className="text-xs text-ink-muted mt-0.5">
              <span className="font-mono uppercase text-[10px] mr-1">remedy:</span>
              {entry.remedy}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}
