import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { BandSeal } from "@/components/BandSeal";

export const dynamic = "force-dynamic";

async function getMetrics() {
  try {
    return await api.metrics();
  } catch {
    return null;
  }
}

export default async function HomePage() {
  const metrics = await getMetrics();

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6 py-16 sm:py-24">
      <p className="font-mono text-xs uppercase tracking-[0.2em] text-ink-muted mb-6">
        Agent commerce · Evidence by construction
      </p>

      <h1 className="font-display text-4xl sm:text-5xl leading-[1.1] mb-6 max-w-3xl">
        Razorpay made the agent able to pay.
        <br />
        This makes the merchant able to prove{" "}
        <span className="italic">what it was allowed to do.</span>
      </h1>

      <p className="text-lg text-ink-muted max-w-2xl mb-10">
        A kirana owner sends three photos of his price list over WhatsApp. Minutes later his
        shop is a signed, agent-readable storefront — gated by a reversibility-scaled policy
        engine, with every decision hash-chained into a dispute pack that exports in one click.
      </p>

      <div className="flex flex-wrap gap-3 mb-16">
        <Link
          href="/onboard"
          className="inline-flex items-center gap-1.5 border border-ink bg-ink text-paper px-4 py-2.5 font-mono text-sm uppercase tracking-wide hover:bg-ink/85 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
        >
          Onboard a shop <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
        <Link
          href="/live"
          className="inline-flex items-center gap-1.5 border border-ink px-4 py-2.5 font-mono text-sm uppercase tracking-wide hover:bg-paper-raised transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
        >
          Watch a live session <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
        <Link
          href="/metrics"
          className="inline-flex items-center gap-1.5 border border-rule px-4 py-2.5 font-mono text-sm uppercase tracking-wide text-ink-muted hover:text-ink hover:border-ink transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
        >
          See the numbers <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
      </div>

      <div className="border border-rule bg-paper-raised p-5 sm:p-6 mb-16">
        <p className="font-mono text-xs uppercase tracking-wider text-ink-muted mb-4">
          This deployment, right now
        </p>
        {metrics ? (
          <dl className="grid grid-cols-2 sm:grid-cols-4 gap-6">
            <Metric label="Sessions gated" value={metrics.sessions_gated} />
            <Metric label="Orders captured" value={metrics.orders_by_status["captured"] ?? 0} />
            <Metric label="Disputes resolvable" value={metrics.disputes_resolvable} />
            <Metric
              label="Escalated to a human"
              value={metrics.orders_by_status["pending_approval"] ?? 0}
            />
          </dl>
        ) : (
          <p className="text-sm text-ink-muted">
            Live counter unavailable — the API may still be waking up.
          </p>
        )}
      </div>

      <section aria-labelledby="ladder-heading" className="mb-8">
        <h2 id="ladder-heading" className="font-display text-2xl mb-4">
          The Reversibility Ladder
        </h2>
        <p className="text-ink-muted mb-6 max-w-2xl">
          Autonomy scales inversely with how hard a purchase is to undo. Every cart is scored
          0 to 1 across five weighted, explainable factors — never tuned after the fact.
        </p>
        <div className="grid sm:grid-cols-3 gap-3">
          <LadderCard
            band="green"
            title="Full autonomy"
            body="Inside the envelope, zero friction. The agent completes the purchase on its own."
          />
          <LadderCard
            band="amber"
            title="Cooling-off"
            body="Payment authorized, dispatch held for a window. The buyer gets a one-tap WhatsApp undo."
          />
          <LadderCard
            band="red"
            title="Step-up required"
            body="A human must approve. The merchant gets a WhatsApp Approve / Decline before anything ships."
          />
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs text-ink-muted mb-1">{label}</dt>
      <dd className="font-mono text-2xl tabular-nums">{value}</dd>
    </div>
  );
}

function LadderCard({
  band,
  title,
  body,
}: {
  band: "green" | "amber" | "red";
  title: string;
  body: string;
}) {
  return (
    <div className="border border-rule p-4">
      <div className="mb-2">
        <BandSeal band={band} />
      </div>
      <h3 className="font-display text-lg mb-1">{title}</h3>
      <p className="text-sm text-ink-muted">{body}</p>
    </div>
  );
}
