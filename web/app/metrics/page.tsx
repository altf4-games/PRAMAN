import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

/**
 * Live counts from this deployment — NOT the harness's Arm A vs Arm B
 * comparison (`harness/RESULTS.md`, a separate, already-run 200-session
 * benchmark against a seeded catalog). Honesty matters more here than
 * looking impressive: the design spec §8 is explicit that a tuned-looking
 * number discredits the whole submission, so this page only ever shows
 * what actually happened on this deployment, never the benchmark's numbers
 * relabelled as live ones.
 */
export default async function MetricsPage() {
  let metrics;
  try {
    metrics = await api.metrics();
  } catch {
    metrics = null;
  }

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6 py-10">
      <h1 className="font-display text-3xl mb-2">Numbers</h1>
      <p className="text-ink-muted max-w-2xl mb-8">
        Live counts from this specific deployment&apos;s database — real gate decisions and orders,
        not a benchmark. The controlled comparison (200 seeded sessions, Arm A naive vs Arm B
        PRAMAN, including false-positive cost, reversibility-band accuracy, and
        injection-invariance) is a separate, already-completed run in{" "}
        <a
          href="https://github.com/altf4-games/PRAMAN/blob/main/harness/RESULTS.md"
          className="underline hover:text-ink"
        >
          harness/RESULTS.md
        </a>
        , kept distinct from this page rather than blended into it — a live deployment counter
        and a controlled benchmark answer different questions, and conflating them would make
        both less trustworthy.
      </p>

      {!metrics ? (
        <p className="border border-band-red text-band-red px-4 py-3 font-mono text-sm">
          Metrics unavailable — the API may still be waking up.
        </p>
      ) : (
        <div className="grid sm:grid-cols-2 gap-6">
          <Panel title="Sessions gated">
            <p className="font-mono text-4xl tabular-nums">{metrics.sessions_gated}</p>
          </Panel>
          <Panel title="Disputes resolvable">
            <p className="font-mono text-4xl tabular-nums">{metrics.disputes_resolvable}</p>
            <p className="text-xs text-ink-muted mt-1">
              orders with a complete dispute pack available at /dispute/[orderId]
            </p>
          </Panel>
          <Panel title="Orders by status">
            <Bars data={metrics.orders_by_status} />
          </Panel>
          <Panel title="Orders by reversibility band">
            <Bars
              data={metrics.orders_by_band}
              colorFor={(k) =>
                k === "green" ? "var(--band-green)" : k === "amber" ? "var(--band-amber)" : "var(--band-red)"
              }
            />
          </Panel>
        </div>
      )}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border border-rule p-4">
      <h2 className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-3">{title}</h2>
      {children}
    </div>
  );
}

function Bars({
  data,
  colorFor,
}: {
  data: Record<string, number>;
  colorFor?: (key: string) => string;
}) {
  const entries = Object.entries(data);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (entries.length === 0) {
    return <p className="text-sm text-ink-muted">No data yet.</p>;
  }
  return (
    <div className="flex flex-col gap-2">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center gap-2">
          <span className="font-mono text-xs w-40 shrink-0 truncate" title={key}>
            {key}
          </span>
          <div className="h-3 bg-paper-raised flex-1 border border-rule">
            <div
              className="h-full"
              style={{
                width: `${(value / max) * 100}%`,
                backgroundColor: colorFor ? colorFor(key) : "var(--ink)",
              }}
            />
          </div>
          <span className="font-mono text-xs tabular-nums w-8 text-right shrink-0">{value}</span>
        </div>
      ))}
    </div>
  );
}
