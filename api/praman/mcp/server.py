"""MCP surface — thin wrappers over the REST routes (the design spec §6: "Build
REST first, wrap second"). Every tool here does nothing but forward its
arguments to the matching REST endpoint over HTTP and return the JSON
response; none of them touch the database, the gate, or any adapter
directly. Annotated per the spec's table: `readOnlyHint` for the catalog/
policy/order-status reads, `idempotentHint` for `quote_request`, and
`destructiveHint` on `checkout_execute` — the sole money-path tool.

Agents bring their own Ed25519 signature (timestamp/nonce/signature) the
same way a direct REST caller would; this layer doesn't sign on an agent's
behalf and never sees a private key. Per `schemas.py`'s module docstring,
those three travel as `X-Praman-*` headers, not body fields — the tools
that need them (`quote_request`, `cart_confirm`, `checkout_execute`,
`substitution_accept`) take them as separate arguments and forward them as
headers, exactly as a direct REST caller would have to.

**On the exact bytes a caller must sign:** the signature covers
`sha256(body)`, so the body bytes this layer actually transmits on the
wire must be byte-identical to what the caller hashed — not merely the
same JSON *value*. A real bug lived here until an external MCP client
(this build's own author, connecting as a genuine third-party agent) hit
it: `_post` used to hand its `body` dict to httpx's `json=` parameter,
which re-serializes it with httpx's own encoder — different whitespace
and key ordering than whatever the caller's own `json.dumps` (or any
other JSON library) produced, so the hash the caller signed almost never
matched the hash this layer's re-serialization produced, and every signed
tool call failed `AGENT_SIG_INVALID`. It went unnoticed because this
build's own callers (the frontend, `agent_runner/tools.py`) never route
signed calls *through* this MCP layer — they call the REST routes
directly with their own exact pre-signed bytes, which is exactly the path
this bug didn't touch.

Fixed by having `_signed_post` transmit the RFC 8785 (JCS) canonical
serialization of `body` (`crypto/canonical.py::canonicalize` — the same
deterministic-bytes primitive the ledger and every other signed structure
in this codebase already depends on) rather than letting httpx re-encode
it. A caller in *any* language gets the same guarantee this build's own
code does: canonicalize your body the same RFC 8785 way, sign
`sha256(canonical_bytes)`, and the bytes this layer transmits are
guaranteed identical to what you hashed — deterministic across
implementations, not just within this one.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from praman.config import get_settings
from praman.crypto.canonical import canonicalize

mcp: FastMCP = FastMCP("praman")


def _base_url() -> str:
    return get_settings().public_base_url.rstrip("/")


def _new_client(base_url: str) -> httpx.AsyncClient:
    # A thin, monkeypatchable seam -- mirrors
    # `agent_runner/tools.py::_new_client`. Tests substitute this to route
    # through an `ASGITransport` against the in-process app rather than
    # real HTTP, without changing any tool's public shape.
    return httpx.AsyncClient(base_url=base_url, timeout=15)


def _sig_headers(timestamp: str, nonce: str, signature: str) -> dict[str, str]:
    return {
        "X-Praman-Timestamp": timestamp,
        "X-Praman-Nonce": nonce,
        "X-Praman-Signature": signature,
    }


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with _new_client(_base_url()) as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    async with _new_client(_base_url()) as client:
        resp = await client.post(path, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _signed_post(path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
    """For the four signed tools only — see this module's docstring for
    why this can't just be `_post`. Transmits the exact RFC 8785 canonical
    bytes of `body`; a caller who signed over anything else (including
    this build's own default `json.dumps`) will still see
    `AGENT_SIG_INVALID`, since canonicalization is what makes the bytes
    deterministic in the first place."""
    async with _new_client(_base_url()) as client:
        resp = await client.post(
            path,
            content=canonicalize(body),
            headers={**headers, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def catalog_search(
    merchant_id: str, category: str | None = None, q: str | None = None
) -> Any:
    """Search a merchant's live (confidence-confirmed) catalog."""
    params: dict[str, Any] = {"merchant_id": merchant_id}
    if category is not None:
        params["category"] = category
    if q is not None:
        params["q"] = q
    return await _get("/api/catalog/search", params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def catalog_get(product_id: str) -> Any:
    """Fetch one product by id."""
    return await _get(f"/api/catalog/{product_id}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def policy_get(merchant_id: str) -> Any:
    """Fetch a merchant's agent-facing policy (spend ceiling, cooling-off preference)."""
    return await _get(f"/api/policy/{merchant_id}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def order_status(order_id: str) -> Any:
    """Look up an order's current status."""
    return await _get(f"/api/orders/{order_id}")


@mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
async def quote_request(
    product_id: str, agent_did: str, qty: int, timestamp: str, nonce: str, signature: str
) -> Any:
    """Request a signed, TTL'd price/stock quote for one product."""
    return await _signed_post(
        "/api/quotes",
        {"product_id": product_id, "agent_did": agent_did, "qty": qty},
        headers=_sig_headers(timestamp, nonce, signature),
    )


@mcp.tool
async def envelope_submit(
    merchant_id: str,
    agent_did: str,
    user_ref: str,
    user_whatsapp: str,
    ceiling_paise: int,
    max_single_txn_paise: int,
    allowed_categories: list[str],
    min_reversibility: float = 0.0,
    valid_hours: int = 24,
) -> Any:
    """Issue a merchant-countersigned Intent Envelope for an agent to operate within."""
    return await _post(
        "/api/envelopes",
        {
            "merchant_id": merchant_id,
            "agent_did": agent_did,
            "user_ref": user_ref,
            "user_whatsapp": user_whatsapp,
            "ceiling_paise": ceiling_paise,
            "max_single_txn_paise": max_single_txn_paise,
            "allowed_categories": allowed_categories,
            "min_reversibility": min_reversibility,
            "valid_hours": valid_hours,
        },
    )


@mcp.tool
async def cart_confirm(
    envelope_id: str,
    agent_did: str,
    quotes: list[dict[str, Any]],
    timestamp: str,
    nonce: str,
    signature: str,
) -> Any:
    """Build and persist a cart mandate from a set of signed quotes."""
    return await _signed_post(
        "/api/cart/confirm",
        {"envelope_id": envelope_id, "agent_did": agent_did, "quotes": quotes},
        headers=_sig_headers(timestamp, nonce, signature),
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
async def checkout_execute(
    cart_id: str,
    agent_did: str,
    quotes: list[dict[str, Any]],
    timestamp: str,
    nonce: str,
    signature: str,
) -> Any:
    """Run the full R01-R12 policy gate and, if allowed or held, capture payment.
    The sole money-path tool in this surface."""
    return await _signed_post(
        "/api/checkout/execute",
        {"cart_id": cart_id, "agent_did": agent_did, "quotes": quotes},
        headers=_sig_headers(timestamp, nonce, signature),
    )


@mcp.tool
async def substitution_accept(
    cart_id: str,
    agent_did: str,
    accepted_product_id: str,
    timestamp: str,
    nonce: str,
    signature: str,
) -> Any:
    """Accept an offered substitute for an out-of-stock item."""
    return await _signed_post(
        "/api/checkout/substitute",
        {"cart_id": cart_id, "agent_did": agent_did, "accepted_product_id": accepted_product_id},
        headers=_sig_headers(timestamp, nonce, signature),
    )


@mcp.tool
async def order_undo(order_id: str, user_ref: str) -> Any:
    """Cancel a still-held (cooling-off) order and refund it."""
    return await _post(f"/api/orders/{order_id}/undo", {"user_ref": user_ref})
