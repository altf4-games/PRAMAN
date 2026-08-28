"use client";

import type { LedgerBusEvent } from "@/lib/sse";

/** Renders the live trace of a genuine LLM-driven agent run — reasoning
 * text, the tool it decided to call, and that tool's real result — as
 * published by `agent_runner/runner.py` to the SSE bus under
 * `agent:{run_id}`. Deliberately a separate component from
 * `LedgerStream`: these events aren't part of the hash-chained ledger
 * (they're the model's own decisions, not gate/money-path facts), so
 * showing them with a chain-proof strip would misrepresent what they are.
 */
export function AgentTrace({
  events,
  connected,
}: {
  events: LedgerBusEvent[];
  connected: boolean;
}) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <span
          className={`inline-block h-1.5 w-1.5 rounded-full ${connected ? "bg-agent" : "bg-ink-muted"}`}
          aria-hidden
        />
        <span className="font-mono text-xs uppercase tracking-wider text-ink-muted">
          Agent {connected ? "live" : "idle"}
        </span>
      </div>
      <ol className="border border-rule divide-y divide-rule max-h-[28rem] overflow-y-auto">
        {events.length === 0 && (
          <li className="px-3 py-6 text-center text-ink-muted text-sm">
            No agent activity yet — give it a goal and let it decide.
          </li>
        )}
        {events.map((e) => (
          <li key={e.payload.event_id} className="px-3 py-2 text-xs">
            {renderRow(e)}
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Shortens any long string value (DIDs, hashes, quote ids) recursively
 * before stringifying, rather than relying on CSS `truncate` — which
 * doesn't actually truncate here, since these are inline `<span>`s in a
 * freely-wrapping `<p>`, not a fixed-width block. Without this, a raw
 * `did:key:z6Mk...` or 32-char id blows the row onto several wrapped
 * lines instead of reading as one compact summary. */
function shorten(value: unknown): unknown {
  if (typeof value === "string") {
    return value.length > 24 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
  }
  if (Array.isArray(value)) return value.map(shorten);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, shorten(v)]),
    );
  }
  return value;
}

function renderRow(e: LedgerBusEvent) {
  const p = e.payload as Record<string, unknown>;
  switch (e.event_type) {
    case "AGENT_GOAL":
      return (
        <p>
          <span className="text-agent font-mono uppercase text-[10px]">goal</span>{" "}
          <span>{String(p.goal)}</span>
        </p>
      );
    case "AGENT_THOUGHT":
      return <p className="italic text-ink-muted">&ldquo;{String(p.text)}&rdquo;</p>;
    case "AGENT_TOOL_CALL":
      return (
        <p className="break-all">
          <span className="font-mono text-agent">→ {String(p.tool)}</span>{" "}
          <span className="font-mono text-ink-muted">{JSON.stringify(shorten(p.args))}</span>
        </p>
      );
    case "AGENT_TOOL_RESULT": {
      const result = p.result as Record<string, unknown> | undefined;
      const isError = result && "error" in result;
      return (
        <p className={`break-all ${isError ? "text-band-red" : "text-band-green"}`}>
          <span className="font-mono uppercase text-[10px]">{isError ? "error" : "ok"}</span>{" "}
          <span className="font-mono text-ink-muted">{JSON.stringify(shorten(result))}</span>
        </p>
      );
    }
    case "AGENT_ERROR":
      return (
        <p className="text-band-red">
          <span className="font-mono uppercase text-[10px]">error</span>{" "}
          {String(p.error)}
        </p>
      );
    case "AGENT_DONE":
      return (
        <p className="font-medium">
          <span className="font-mono uppercase text-[10px] text-agent">done</span>{" "}
          {String(p.summary)}
        </p>
      );
    default:
      return <span className="font-mono text-ink-muted">{e.event_type}</span>;
  }
}
