"""Renders a `DisputePackOut` as a print-ready HTML document, then to PDF
via WeasyPrint (`routes_dispute.py`'s `/pdf` route). Deliberately the same
document, not a separate design: same tokens, same field order, same
sections as `web/app/dispute/[orderId]/page.tsx`'s bahi-khata layout
(CLAUDE.md §7) -- a merchant or a reviewer should recognise the PDF as the
same artifact as the `/dispute/[orderId]` page, just paginated.
"""

from __future__ import annotations

import html
from typing import Any

from praman.schemas import DisputePackOut

_BAND_COLOR = {"green": "#1F7A4C", "amber": "#B8791A", "red": "#A32C2C"}


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _money(paise: object) -> str:
    if not isinstance(paise, (int, float)):
        return "—"
    return f"₹{paise / 100:,.2f}"


def _row(label: str, value: object) -> str:
    return (
        f'<div class="row"><span class="k">{_esc(label)}</span>'
        f'<span class="v">{_esc(value)}</span></div>'
    )


def render_dispute_pack_html(pack: DisputePackOut) -> str:
    envelope: dict[str, Any] = pack.envelope
    cart_mandate: dict[str, Any] = pack.cart_mandate
    order: dict[str, Any] | None = pack.order
    band_color = _BAND_COLOR.get(pack.band, "#5A6472")

    items_rows = "".join(
        _row(
            item.get("sku", "?"), f"{item.get('qty', '?')} × {_money(item.get('unit_price_paise'))}"
        )
        for item in cart_mandate.get("items", [])
    )

    quote_rows = (
        "".join(
            f"<tr><td>{_esc(q.get('sku'))}</td><td>{_esc(q.get('qty'))}</td>"
            f"<td>{_money(q.get('unit_price_paise'))}</td>"
            f"<td>{_esc(q.get('issued_at'))}</td><td>{_esc(q.get('expires_at'))}</td>"
            f"<td class='mono small'>{_esc(str(q.get('signature', ''))[:16])}…</td></tr>"
            for q in pack.quote_provenance
        )
        or "<tr><td colspan='6' class='muted'>No persisted quote row matched this cart.</td></tr>"
    )

    gate_rows = "".join(
        f"<tr><td class='mono'>{_esc(g.get('rule_id') or '—')}</td>"
        f"<td>{_esc(g.get('decision'))}</td><td>{_esc(g.get('reason_code'))}</td>"
        f"<td>{_esc(g.get('detail'))}</td><td>{_esc(g.get('evaluated_at'))}</td></tr>"
        for g in pack.gate_trail
    )

    ledger_events_raw = pack.ledger.get("events", [])
    ledger_events: list[dict[str, Any]] = (
        list(ledger_events_raw) if isinstance(ledger_events_raw, list) else []
    )
    ledger_rows = "".join(
        f"<tr><td>{_esc(e.get('event_type'))}</td><td>{_esc(e.get('ts'))}</td>"
        f"<td class='mono small'>{_esc(str(e.get('chain_hash', ''))[:12])}…"
        f"{_esc(str(e.get('chain_hash', ''))[-8:])}</td></tr>"
        for e in ledger_events
    )
    chain_verified = (
        bool(pack.ledger.get("chain_verified")) if isinstance(pack.ledger, dict) else False
    )

    order_section = ""
    if order is not None:
        order_section = (
            '<section><h2>Cooling-off / step-up / dispatch</h2><div class="grid2">'
            + _row("status", order.get("status"))
            + _row("cooling_off_until", order.get("cooling_off_until"))
            + _row("dispatched_at", order.get("dispatched_at"))
            + _row("cancelled_at", order.get("cancelled_at"))
            + _row("refunded_at", order.get("refunded_at"))
            + _row("stepup_channel", order.get("stepup_channel"))
            + _row("stepup_confirmed_at", order.get("stepup_confirmed_at"))
            + "</div></section>"
        )

    breakdown_rows = "".join(_row(k, f"{v:.3f}") for k, v in pack.reversibility_breakdown.items())

    signature_line = (
        f"Signed by merchant {_esc(pack.merchant_did)} — "
        f"<span class='mono small'>{_esc(pack.merchant_signature)}</span>"
        if pack.merchant_signature
        else "<span class='muted'>Not signed (merchant key unavailable at pack-assembly time).</span>"
    )

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{
    font-family: 'Inter Tight', 'Helvetica Neue', Arial, sans-serif;
    color: #141A22;
    background: #EDEEEA;
    font-size: 10.5pt;
  }}
  h1 {{ font-family: 'Newsreader', Georgia, serif; font-size: 20pt; margin: 0 0 2mm 0; }}
  h2 {{
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 9pt; text-transform: uppercase; letter-spacing: 0.05em;
    color: #5A6472; border-bottom: 1px solid #C9CCC4; padding-bottom: 1mm;
    margin: 6mm 0 2mm 0;
  }}
  .muted {{ color: #5A6472; }}
  .mono {{ font-family: 'JetBrains Mono', 'Courier New', monospace; }}
  .small {{ font-size: 8pt; }}
  .band {{
    display: inline-block; font-family: 'JetBrains Mono', monospace;
    font-size: 9pt; text-transform: uppercase; padding: 1mm 3mm;
    border: 1px solid {band_color}; color: {band_color};
  }}
  .row {{ display: flex; justify-content: space-between; gap: 4mm; padding: 0.5mm 0; }}
  .row .k {{ font-family: 'JetBrains Mono', monospace; font-size: 8pt; color: #5A6472; text-transform: uppercase; }}
  .row .v {{ font-family: 'JetBrains Mono', monospace; font-size: 9pt; text-align: right; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0 8mm; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 8.5pt; }}
  th {{
    text-align: left; font-family: 'JetBrains Mono', monospace; font-size: 7.5pt;
    text-transform: uppercase; color: #5A6472; border-bottom: 1px solid #C9CCC4;
    padding: 1mm 2mm;
  }}
  td {{ padding: 1mm 2mm; border-bottom: 1px solid #C9CCC4; }}
  .footer {{ margin-top: 8mm; padding-top: 2mm; border-top: 1px solid #C9CCC4; font-size: 8pt; color: #5A6472; }}
</style>
</head>
<body>
  <div style="display:flex; justify-content:space-between; align-items:flex-start;">
    <div>
      <p class="mono small muted" style="text-transform:uppercase;">Dispute pack</p>
      <h1>Cart {_esc(pack.cart_id)}</h1>
    </div>
    <span class="band">{_esc(pack.band)}</span>
  </div>

  <section><h2>Reversibility breakdown</h2><div class="grid2">{breakdown_rows}</div></section>

  <section><h2>Intent envelope</h2><div class="grid2">
    {_row("agent_did", envelope.get("agent_did"))}
    {_row("ceiling_paise", _money(envelope.get("ceiling_paise")))}
    {_row("max_single_txn_paise", _money(envelope.get("max_single_txn_paise")))}
    {_row("min_reversibility", envelope.get("min_reversibility"))}
    {_row("allowed_categories", ", ".join(envelope.get("allowed_categories") or []))}
    {_row("valid_from", envelope.get("valid_from"))}
    {_row("valid_until", envelope.get("valid_until"))}
  </div></section>

  <section><h2>Cart mandate</h2>{items_rows}
    {_row("total_paise", _money(cart_mandate.get("total_paise")))}
    {_row("agent_sig", str(cart_mandate.get("agent_sig", ""))[:32] + "…")}
  </section>

  {order_section}

  <section><h2>Quote provenance</h2>
    <table><thead><tr><th>sku</th><th>qty</th><th>price</th><th>issued</th><th>expires</th><th>sig</th></tr></thead>
    <tbody>{quote_rows}</tbody></table>
  </section>

  <section><h2>Gate trail</h2>
    <table><thead><tr><th>rule</th><th>decision</th><th>reason</th><th>detail</th><th>at</th></tr></thead>
    <tbody>{gate_rows}</tbody></table>
  </section>

  <section><h2>Chain proof — {"verified" if chain_verified else "BROKEN"}</h2>
    <table><thead><tr><th>event</th><th>ts</th><th>chain_hash</th></tr></thead>
    <tbody>{ledger_rows}</tbody></table>
  </section>

  <div class="footer">
    <p>pack_hash (sha256 of the canonical JCS form of this pack's facts): <span class="mono small">{_esc(pack.pack_hash)}</span></p>
    <p>{signature_line}</p>
  </div>
</body>
</html>
"""
