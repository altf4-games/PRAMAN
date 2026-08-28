#!/usr/bin/env python3
"""A genuine third-party MCP client buying something from a live PRAMAN
storefront — no scripted frontend, no Gemini, no special access. Connects
to the deployed `/mcp` endpoint exactly the way any real MCP-compatible
agent (Claude, or anything else speaking the protocol) would, per the
storefront's own `/.well-known/agent-commerce.json` discovery document.

Demo use: run this live, on camera, narrated. It's the honest version of
"an AI agent bought this" — not a bespoke integration built for the video,
the same real signed protocol any external agent has to use.

    python scripts/mcp_agent_demo.py --goal "1kg toor dal" \\
        --merchant-id 51f06df7cbbf4a74b305bb0768ffd37d \\
        --budget-paise 50000

One honest caveat, worth saying out loud if asked: `register_agent` isn't
an MCP tool (an agent has no identity yet to sign with, so that bootstrap
step is a plain, unsigned REST call — see `mcp/server.py`'s own
docstring). Everything after that — envelope, quote, cart, checkout — goes
through the real MCP tool calls, each independently signed here with a
fresh Ed25519 keypair this script holds only in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, "api")
from praman.crypto.canonical import canonicalize
from praman.crypto.keys import sign

_RULE = "─" * 60


def _step(label: str) -> None:
    print(f"\n{_RULE}\n{label}\n{_RULE}")


def _sig_headers(
    private_key_hex: str, method: str, body: dict[str, object]
) -> tuple[str, str, str]:
    """Sign over the RFC 8785 canonical form — the contract any real
    external MCP caller must follow (see `mcp/server.py`'s docstring)."""
    body_hash = hashlib.sha256(canonicalize(body)).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    message = f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()
    return timestamp, nonce, sign(private_key_hex, message)


class _EmptyContent(Exception):
    """A transient quirk observed in the `mcp` SDK's streamable-HTTP
    client: consecutive tool calls in one session occasionally come back
    with empty `content` (not a tool error — `isError` is false, there's
    just nothing there). Reproducible only intermittently and not tied to
    any particular tool or call order — a fresh single-call session never
    showed it; rerunning the exact same two-call sequence sometimes did
    and sometimes didn't. Retried rather than root-caused further, since
    it's in third-party transport code this project doesn't own."""


def _unwrap(result: object) -> dict[str, object]:
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "(no detail)"  # type: ignore[union-attr]
        raise RuntimeError(f"MCP tool call failed: {text}")
    if not result.content:  # type: ignore[union-attr]
        raise _EmptyContent("tool call returned no content")
    return json.loads(result.content[0].text)  # type: ignore[union-attr,no-any-return]


async def _call_tool(
    session: ClientSession, name: str, args: dict[str, object]
) -> dict[str, object]:
    for attempt in range(3):
        try:
            return _unwrap(await session.call_tool(name, args))
        except _EmptyContent:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)
    raise AssertionError("unreachable")  # pragma: no cover


async def buy(
    *,
    api_url: str,
    merchant_id: str,
    goal: str,
    budget_paise: int,
    qty: int,
    min_reversibility: float,
    buyer: str,
) -> None:
    print(f"Goal: {goal!r}  |  budget: ₹{budget_paise / 100:.2f}  |  merchant: {merchant_id}")

    _step("Registering this agent's identity (plain REST — no signature exists yet)")
    async with httpx.AsyncClient(base_url=api_url, timeout=20) as http:
        resp = await http.post(
            "/api/agents/register",
            json={
                "operator": "Claude (MCP client demo)",
                "max_txn_paise": budget_paise,
                "daily_cap_paise": budget_paise * 10,
            },
        )
        resp.raise_for_status()
        reg = resp.json()
    agent_did = reg["agent_did"]
    private_key = reg["private_key"]
    print(f"agent_did = {agent_did}")

    checkout: dict[str, object]
    async with streamablehttp_client(f"{api_url}/mcp") as (read, write, _):  # noqa: SIM117
        async with ClientSession(read, write) as session:
            await session.initialize()
            # An extra round-trip (list_tools) before the first real tool
            # call reliably avoided the empty-content quirk in testing --
            # see `_EmptyContent`'s docstring. Also doubles as a genuine
            # "here are this storefront's real tools" moment for a demo.
            tools = await session.list_tools()
            print(f"discovered {len(tools.tools)} MCP tools")

            _step("catalog_search (MCP tool, readOnlyHint) — " + repr(goal))
            products = await _call_tool(
                session, "catalog_search", {"merchant_id": merchant_id, "q": goal}
            )
            product_list = products if isinstance(products, list) else []
            if not product_list:
                raise RuntimeError(f"no products matched {goal!r} at this merchant")
            product = product_list[0]
            print(f"matched: {product['name']} — ₹{product['unit_price_paise'] / 100:.2f}")
            await asyncio.sleep(0.4)

            _step("envelope_submit (MCP tool)")
            env = await _call_tool(
                session,
                "envelope_submit",
                {
                    "merchant_id": merchant_id,
                    "agent_did": agent_did,
                    "user_ref": "mcp-agent-demo",
                    "user_whatsapp": buyer,
                    "ceiling_paise": budget_paise,
                    "max_single_txn_paise": budget_paise,
                    "allowed_categories": [product["category"]],
                    "min_reversibility": min_reversibility,
                    "valid_hours": 1,
                },
            )
            print(f"envelope_id = {env['envelope_id']}")
            await asyncio.sleep(0.4)

            _step("quote_request (MCP tool, signed)")
            qbody = {"product_id": product["id"], "agent_did": agent_did, "qty": qty}
            ts, nonce, sig = _sig_headers(private_key, "POST", qbody)
            quote = await _call_tool(
                session,
                "quote_request",
                {**qbody, "timestamp": ts, "nonce": nonce, "signature": sig},
            )
            print(f"quote_id = {quote['quote_id']}  total = ₹{quote['total_paise'] / 100:.2f}")
            await asyncio.sleep(0.4)

            _step("cart_confirm (MCP tool, signed)")
            cbody = {"envelope_id": env["envelope_id"], "agent_did": agent_did, "quotes": [quote]}
            ts, nonce, sig = _sig_headers(private_key, "POST", cbody)
            cart = await _call_tool(
                session,
                "cart_confirm",
                {**cbody, "timestamp": ts, "nonce": nonce, "signature": sig},
            )
            print(
                f"cart_id = {cart['cart_id']}  band = {str(cart['band']).upper()}  "
                f"reversibility = {cart['reversibility_score']:.3f}"
            )
            await asyncio.sleep(0.4)

            _step("checkout_execute (MCP tool, signed, destructiveHint — the money path)")
            xbody = {"cart_id": cart["cart_id"], "agent_did": agent_did, "quotes": [quote]}
            ts, nonce, sig = _sig_headers(private_key, "POST", xbody)
            checkout = await _call_tool(
                session,
                "checkout_execute",
                {**xbody, "timestamp": ts, "nonce": nonce, "signature": sig},
            )

    _step("RESULT")
    print(f"decision      = {checkout['decision']}")
    print(f"reason_code   = {checkout['reason_code']}")
    print(f"detail        = {checkout['detail']}")
    if checkout.get("order_id"):
        order_id = checkout["order_id"]
        order_status = checkout.get("order_status")
        frontend_url = api_url.replace("praman-production.up.railway.app", "praman-jet.vercel.app")
        print(f"order_id      = {order_id}  ({order_status})")
        if order_status in ("awaiting_payment", "awaiting_payment_amber"):
            # No page in the app shows a "pay" button for an order it
            # didn't itself create -- /live's own button only works for a
            # cart built in that same browser session. /pay/[orderId]
            # exists specifically so an order created here, from a
            # terminal, has somewhere to complete a real payment.
            print(f"pay this order = {frontend_url}/pay/{order_id}")
        print(f"dispute pack  = {frontend_url}/dispute/{order_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://praman-production.up.railway.app")
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--goal", required=True, help='e.g. "1kg toor dal"')
    parser.add_argument("--budget-paise", type=int, default=50_000)
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument(
        "--min-reversibility",
        type=float,
        default=0.0,
        help="R08 only escalates below this -- 0 means a red-band item still ALLOWs",
    )
    parser.add_argument(
        "--buyer",
        default="whatsapp:+919000000000",
        help="the buyer's channel:address -- e.g. telegram:8980338920 to receive the real "
        "cooling-off/approval notification on your own account instead of a placeholder",
    )
    args = parser.parse_args()

    asyncio.run(
        buy(
            api_url=args.api_url,
            merchant_id=args.merchant_id,
            goal=args.goal,
            budget_paise=args.budget_paise,
            qty=args.qty,
            min_reversibility=args.min_reversibility,
            buyer=args.buyer,
        )
    )


if __name__ == "__main__":
    main()
