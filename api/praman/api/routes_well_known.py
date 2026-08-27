"""`GET /.well-known/agent-commerce.json` — the discovery document an AI
shopping agent (or its operator) fetches first to learn how to transact
with this storefront: where the REST/MCP surfaces live, what the money-path
and read-only tool names are, and how requests must be signed. Kept as a
plain dict rather than a static file so `public_base_url` is always
accurate for whichever environment (local/Railway) is actually serving it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from praman.config import get_settings

router = APIRouter(tags=["discovery"])


@router.get("/.well-known/agent-commerce.json")
async def agent_commerce_manifest() -> dict[str, Any]:
    base = get_settings().public_base_url.rstrip("/")
    return {
        "protocol": "praman-agent-commerce",
        "version": "0.1.0",
        "api_base_url": base,
        "mcp_url": f"{base}/mcp",
        "auth": {
            "scheme": "ed25519-detached",
            "signed_message": "method\\nsha256(body)\\ntimestamp\\nnonce",
            "clock_skew_tolerance_seconds": 60,
        },
        "tools": {
            "read_only": ["catalog_search", "catalog_get", "policy_get", "order_status"],
            "idempotent": ["quote_request"],
            "state_changing": [
                "envelope_submit",
                "cart_confirm",
                "substitution_accept",
                "order_undo",
            ],
            "destructive": ["checkout_execute"],
        },
        "reversibility_ladder": {
            "bands": ["green", "amber", "red"],
            "description": "Autonomy scales inversely with how hard a purchase is to undo. "
            "green: full autonomy inside the envelope. amber: dispatch held for a cooling-off "
            "window, buyer can undo. red: a human must step up before the order proceeds.",
        },
    }
