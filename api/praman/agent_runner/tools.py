"""Tool schemas and a stateful executor for the LLM shopping agent
(`agent_runner/runner.py`). Two boundaries matter here, both deliberate:

1. **The model never sees or handles cryptographic material.** It only
   ever proposes `(tool_name, business_arguments)` -- this executor is the
   "agent SDK" a real external agent operator would also have to write:
   it holds the demo Ed25519 keypair, computes each signed request's
   timestamp/nonce/signature, and only then makes the same real HTTP call
   an unattended agent would make against this build's REST surface (the
   same routes `mcp/server.py` wraps 1:1). `mcp/server.py`'s own docstring
   establishes this same separation -- "this layer doesn't sign on an
   agent's behalf" -- and it holds here too: the model is never handed a
   private key.
2. **Opaque, signed blobs (quotes) are cached here by id, not reproduced
   by the model.** The model references a `quote_id` it has already seen
   in a tool result; this executor resolves it back to the exact signed
   object the real API returned. This doesn't hide any shopping decision
   from the model -- it still decides what to search for, how much to
   buy, and when to check out -- it just keeps a well-known LLM weak spot
   (reproducing a large opaque JSON blob byte-for-byte) out of the one
   path where a mismatch means a broken signature, not just a wrong
   answer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from google.genai import types

from praman.crypto.keys import sign


def _signing_message(method: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    # Identical to `core/registry.py::_signing_message` -- duplicated
    # rather than imported so this module doesn't reach into another
    # module's private helper; the two-line formula is the actual
    # contract (documented in `core/registry.py`'s module docstring), not
    # an implementation detail worth coupling to.
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()


def _new_client(base_url: str) -> httpx.AsyncClient:
    # A thin, monkeypatchable seam -- tests substitute this to route through
    # an `ASGITransport` against the in-process app rather than real HTTP,
    # without changing `AgentToolExecutor`'s public shape.
    return httpx.AsyncClient(base_url=base_url, timeout=20)


def _sig_headers(private_key_hex: str, method: str, body: bytes) -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    signature = sign(private_key_hex, _signing_message(method, body, timestamp, nonce))
    return {
        "X-Praman-Timestamp": timestamp,
        "X-Praman-Nonce": nonce,
        "X-Praman-Signature": signature,
        "Content-Type": "application/json",
    }


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="register_agent",
        description=(
            "Register this shopping agent's own identity. Call this first, before anything else."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "operator": types.Schema(
                    type=types.Type.STRING,
                    description="Who operates this agent, e.g. a person's name or app name.",
                )
            },
            required=["operator"],
        ),
    ),
    types.FunctionDeclaration(
        name="envelope_submit",
        description=(
            "Request a merchant-countersigned Intent Envelope -- a one-time consent with a "
            "spending ceiling -- before shopping. Call this once, after register_agent."
        ),
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),
    types.FunctionDeclaration(
        name="catalog_search",
        description="Search the merchant's live catalog for products matching a query.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "q": types.Schema(
                    type=types.Type.STRING, description="search text, e.g. a product name"
                )
            },
        ),
    ),
    types.FunctionDeclaration(
        name="quote_request",
        description=(
            "Request a signed, time-limited price/stock quote for one product, by its "
            "product_id, before buying it. Returns a quote_id to reference later."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "product_id": types.Schema(type=types.Type.STRING),
                "qty": types.Schema(type=types.Type.INTEGER),
            },
            required=["product_id", "qty"],
        ),
    ),
    types.FunctionDeclaration(
        name="cart_confirm",
        description=(
            "Build a cart mandate from one or more quotes you already requested, referenced by "
            "the quote_id values quote_request returned."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "quote_ids": types.Schema(
                    type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
                )
            },
            required=["quote_ids"],
        ),
    ),
    types.FunctionDeclaration(
        name="checkout_execute",
        description=(
            "Run checkout for a confirmed cart_id. This is the sole money-carrying step and "
            "runs a real policy gate. It may ALLOW (completes immediately), HOLD (payment "
            "captured, dispatch withheld for a buyer cooling-off window), or ESCALATE (needs "
            "merchant approval) -- all three are normal, expected outcomes, not failures. "
            "Explain the outcome to the user afterwards in plain language."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"cart_id": types.Schema(type=types.Type.STRING)},
            required=["cart_id"],
        ),
    ),
]


@dataclass
class AgentToolExecutor:
    """Holds one shopping session's state: the demo agent identity it
    registers for itself, the envelope it's granted, and every quote it
    has requested (keyed by `quote_id`) and cart it has confirmed (keyed
    by `cart_id`, mapping to the exact quotes it was built from) --
    exactly what a real agent SDK would track between tool calls.
    """

    base_url: str
    merchant_id: str
    user_ref: str
    user_whatsapp: str
    ceiling_paise: int
    max_single_txn_paise: int
    allowed_categories: list[str]
    min_reversibility: float = 0.0

    agent_did: str | None = None
    envelope_id: str | None = None
    _private_key: str | None = field(default=None, repr=False)
    _quotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    _cart_quotes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = getattr(
            self, f"_tool_{name}", None
        )
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            result: dict[str, Any] = await handler(args)
            return result
        except httpx.HTTPStatusError as exc:
            detail: Any
            try:
                detail = exc.response.json()
            except ValueError:
                detail = exc.response.text
            return {"error": f"HTTP {exc.response.status_code}", "detail": detail}

    async def _tool_register_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        async with _new_client(self.base_url) as client:
            resp = await client.post(
                "/api/agents/register",
                json={
                    "operator": args.get("operator", "praman-live-agent"),
                    "max_txn_paise": self.max_single_txn_paise,
                    "daily_cap_paise": self.ceiling_paise * 10,
                },
            )
            resp.raise_for_status()
        data = resp.json()
        self.agent_did = data["agent_did"]
        self._private_key = data.get("private_key")
        return {"agent_did": self.agent_did}

    async def _tool_envelope_submit(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.agent_did is None:
            return {"error": "call register_agent first"}
        async with _new_client(self.base_url) as client:
            resp = await client.post(
                "/api/envelopes",
                json={
                    "merchant_id": self.merchant_id,
                    "agent_did": self.agent_did,
                    "user_ref": self.user_ref,
                    "user_whatsapp": self.user_whatsapp,
                    "ceiling_paise": self.ceiling_paise,
                    "max_single_txn_paise": self.max_single_txn_paise,
                    "allowed_categories": self.allowed_categories,
                    "min_reversibility": self.min_reversibility,
                    "valid_hours": 24,
                },
            )
            resp.raise_for_status()
        data = resp.json()
        self.envelope_id = data["envelope_id"]
        return {"envelope_id": self.envelope_id, "ceiling_paise": self.ceiling_paise}

    async def _tool_catalog_search(self, args: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, str] = {"merchant_id": self.merchant_id}
        if args.get("q"):
            params["q"] = str(args["q"])
        async with _new_client(self.base_url) as client:
            resp = await client.get("/api/catalog/search", params=params)
            resp.raise_for_status()
        products = resp.json()
        return {
            "products": [
                {
                    "product_id": p["id"],
                    "sku": p["sku"],
                    "name": p["name"],
                    "category": p["category"],
                    "unit_price_paise": p["unit_price_paise"],
                    "stock": p.get("stock"),
                }
                for p in products
            ]
        }

    async def _tool_quote_request(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.agent_did is None or self._private_key is None:
            return {"error": "call register_agent first"}
        body = {
            "product_id": args["product_id"],
            "agent_did": self.agent_did,
            "qty": int(args["qty"]),
        }
        raw = json.dumps(body)
        headers = _sig_headers(self._private_key, "POST", raw.encode())
        async with _new_client(self.base_url) as client:
            resp = await client.post("/api/quotes", content=raw, headers=headers)
            resp.raise_for_status()
        quote = resp.json()
        self._quotes[quote["quote_id"]] = quote
        return {
            "quote_id": quote["quote_id"],
            "sku": quote["sku"],
            "unit_price_paise": quote["unit_price_paise"],
            "qty": quote["qty"],
            "total_paise": quote["total_paise"],
            "expires_at": quote["expires_at"],
        }

    async def _tool_cart_confirm(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.agent_did is None or self._private_key is None or self.envelope_id is None:
            return {"error": "call register_agent and envelope_submit first"}
        quote_ids = args.get("quote_ids") or []
        quotes = [self._quotes[q] for q in quote_ids if q in self._quotes]
        if not quotes:
            return {"error": "no matching quote_id(s) -- call quote_request first"}
        body = {"envelope_id": self.envelope_id, "agent_did": self.agent_did, "quotes": quotes}
        raw = json.dumps(body)
        headers = _sig_headers(self._private_key, "POST", raw.encode())
        async with _new_client(self.base_url) as client:
            resp = await client.post("/api/cart/confirm", content=raw, headers=headers)
            resp.raise_for_status()
        cart = resp.json()
        self._cart_quotes[cart["cart_id"]] = quotes
        return {
            "cart_id": cart["cart_id"],
            "band": cart["band"],
            "reversibility_score": cart["reversibility_score"],
            "reversibility_breakdown": cart["reversibility_breakdown"],
            "total_paise": cart["total_paise"],
        }

    async def _tool_checkout_execute(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.agent_did is None or self._private_key is None:
            return {"error": "call register_agent first"}
        cart_id = args["cart_id"]
        quotes = self._cart_quotes.get(cart_id)
        if quotes is None:
            return {"error": "unknown cart_id -- call cart_confirm first"}
        body = {"cart_id": cart_id, "agent_did": self.agent_did, "quotes": quotes}
        raw = json.dumps(body)
        headers = _sig_headers(self._private_key, "POST", raw.encode())
        async with _new_client(self.base_url) as client:
            resp = await client.post("/api/checkout/execute", content=raw, headers=headers)
            resp.raise_for_status()
        return dict(resp.json())
