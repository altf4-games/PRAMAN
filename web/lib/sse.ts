/**
 * `useLedgerStream` — subscribes to the API's SSE bus (`GET
 * /api/events/stream`, api/praman/events.py) and accumulates events for a
 * given session_id, or every session if omitted. Backing this with `fetch`
 * + a manual line-by-line SSE parser rather than the browser's native
 * `EventSource` is deliberate: the backend names each frame's `event:`
 * field after that event's own `event_type` (there are dozens — see
 * `whatsapp/onboarding.py`, `core/checkout.py`, `core/gate.py` for the
 * full list, tested at `tests/test_sse_endpoint.py`), and `EventSource`'s
 * `onmessage` only ever fires for the *default*, unnamed event type — it
 * would silently miss every one of ours.
 */

"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE_URL } from "@/lib/api";

export interface LedgerBusEvent {
  event_type: string;
  payload: {
    event_id: string;
    ts: string;
    // Real ledger events carry both; non-ledger events published to this
    // same bus (e.g. agent_runner's AGENT_THOUGHT/AGENT_TOOL_CALL trace)
    // carry neither — components rendering `chain_hash` must handle it
    // being absent (see LedgerStream.tsx's truncateHash).
    agent_did?: string | null;
    chain_hash?: string;
    [key: string]: unknown;
  };
}

/** Parses one blank-line-delimited SSE frame's `event:`/`data:` lines.
 * Ignores `id:`/`retry:`/comment lines — this bus never sends them. */
function parseFrame(frame: string): LedgerBusEvent | null {
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    const clean = line.endsWith("\r") ? line.slice(0, -1) : line;
    if (clean.startsWith("data:")) dataLines.push(clean.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;
  try {
    return JSON.parse(dataLines.join("\n")) as LedgerBusEvent;
  } catch {
    return null;
  }
}

export function useLedgerStream(sessionId: string | null, opts?: { max?: number }) {
  const [events, setEvents] = useState<LedgerBusEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const max = opts?.max ?? 500;
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setEvents([]);
    const controller = new AbortController();
    abortRef.current = controller;

    async function run() {
      const params = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
      try {
        const res = await fetch(`${API_BASE_URL}/api/events/stream${params}`, {
          signal: controller.signal,
          headers: { accept: "text/event-stream" },
        });
        if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);
        setConnected(true);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          // sse_starlette terminates lines with CRLF, not bare LF — a
          // frame boundary is a blank line, i.e. "\r\n\r\n" on the wire.
          // Normalizing to LF here means the "\n\n" search below (and
          // parseFrame's line splitting) doesn't need to special-case it.
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

          let sepIndex: number;
          while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0, sepIndex);
            buffer = buffer.slice(sepIndex + 2);
            const parsed = parseFrame(frame);
            if (parsed) {
              setEvents((prev) => {
                const next = [...prev, parsed];
                return next.length > max ? next.slice(next.length - max) : next;
              });
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") setConnected(false);
      } finally {
        setConnected(false);
      }
    }

    run();
    return () => controller.abort();
  }, [sessionId, max]);

  return { events, connected };
}
