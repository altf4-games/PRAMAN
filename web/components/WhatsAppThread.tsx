export interface WhatsAppMessageItem {
  direction: "inbound" | "outbound";
  body: string;
  ts: string;
  delivered?: boolean;
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit" });
  } catch {
    return ts;
  }
}

/** Mirrors the real WhatsApp thread driving vendor onboarding
 * (`whatsapp/onboarding.py`) — `/onboard`'s theatre. Inbound = the vendor's
 * own phone; outbound = the bot. Deliberately plain chat bubbles, not a
 * WhatsApp UI clone — this is a record of what was actually said, styled
 * like the rest of the product (ink on paper), not a trademark pastiche. */
export function WhatsAppThread({ messages }: { messages: WhatsAppMessageItem[] }) {
  if (messages.length === 0) {
    return (
      <p className="text-sm text-ink-muted font-mono px-1">
        No messages yet — send a photo of your price list to begin.
      </p>
    );
  }

  return (
    <ol className="flex flex-col gap-2 px-1">
      {messages.map((m, i) => (
        <li
          key={i}
          className={`max-w-[85%] px-3 py-2 border ${
            m.direction === "outbound"
              ? "self-start border-rule bg-paper-raised"
              : "self-end border-ink/20 bg-agent/5"
          }`}
        >
          <p className="text-sm whitespace-pre-wrap">{m.body}</p>
          <div className="mt-1 flex items-center gap-1.5 justify-end">
            {m.direction === "outbound" && m.delivered === false && (
              <span className="font-mono text-[10px] text-band-red uppercase">not delivered</span>
            )}
            <span className="font-mono text-[10px] text-ink-muted">{formatTime(m.ts)}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}
