"""Agent registration. Not one of the spec's MCP tools (CLAUDE.md §6 lists
`catalog_search`/`catalog_get`/`policy_get`/`quote_request`/
`envelope_submit`/`cart_confirm`/`checkout_execute`/`order_status`/
`substitution_accept`/`order_undo` — no registration tool), so this stays
REST-only and is never wrapped into `mcp/server.py`. It exists because
something has to create the `agents` rows the rest of the REST surface
assumes already exist — in a real deployment an agent operator would
register out-of-band (or via NPCI's future UAP, see `core/registry.py`);
this route is that out-of-band step made callable for this build.

If the caller supplies their own `public_key`, the server never sees or
generates a private key — exactly how a real agent identity should work,
since `Agent` (unlike `Merchant`) has no `private_key_enc` column: this
server is never meant to hold an agent's signing key. Omitting `public_key`
is a demo-only convenience (generates a keypair server-side and returns the
private key once, in the response, never stored) for the harness/quickstart
flow where there's no separate agent operator to hold one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from praman.api.deps import DbSession
from praman.crypto import did as did_module
from praman.crypto import keys as keys_module
from praman.models import Agent
from praman.schemas import AgentRegisterIn, AgentRegisterOut

router = APIRouter(prefix="/api", tags=["agents"])


@router.post("/agents/register")
async def register_agent(session: DbSession, body: AgentRegisterIn) -> AgentRegisterOut:
    private_key_hex: str | None = None
    if body.public_key is not None:
        public_key_hex = body.public_key
    else:
        private_key_hex, public_key_hex = keys_module.generate_keypair()

    agent_did = did_module.did_from_public_key(public_key_hex)
    agent = Agent(
        agent_did=agent_did,
        operator=body.operator,
        public_key=public_key_hex,
        trust_tier=body.trust_tier,
        max_txn_paise=body.max_txn_paise,
        daily_cap_paise=body.daily_cap_paise,
        registered_at=datetime.now(UTC),
        revoked_at=None,
    )
    session.add(agent)
    await session.commit()

    return AgentRegisterOut(
        agent_did=agent_did, public_key=public_key_hex, private_key=private_key_hex
    )
