"""`POST /api/agent/run` — kicks off a genuine LLM-driven shopping session
(see `agent_runner/runner.py`). Publishes its trace live to the SSE bus
under `agent:{run_id}` as it runs; the caller should subscribe to
`GET /api/events/stream?session_id=agent:{run_id}` *before* calling this
route, since `events.py`'s bus doesn't replay to a subscriber that
connects after a given event was published.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from praman.agent_runner.runner import run_agent
from praman.config import get_settings

router = APIRouter(prefix="/api", tags=["agent"])


class AgentRunIn(BaseModel):
    goal: str
    merchant_id: str
    merchant_name: str
    user_ref: str
    user_whatsapp: str
    ceiling_paise: int
    max_single_txn_paise: int
    allowed_categories: list[str]
    min_reversibility: float = 0.0
    run_id: str | None = None


class AgentRunOut(BaseModel):
    run_id: str
    agent_did: str | None
    cart_id: str | None
    order_id: str | None
    decision: str | None
    summary: str


@router.post("/agent/run")
async def agent_run(body: AgentRunIn) -> AgentRunOut:
    settings = get_settings()
    if settings.llm_provider != "gemini" or not settings.llm_api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "no live LLM configured (LLM_PROVIDER/LLM_API_KEY) — the agent runner needs a "
                "real model to reason with, unlike catalog extraction/substitution ranking "
                "which fall back to a Fake client in tests"
            ),
        )
    run_id = body.run_id or uuid.uuid4().hex
    result = await run_agent(
        settings,
        run_id=run_id,
        goal=body.goal,
        merchant_id=body.merchant_id,
        merchant_name=body.merchant_name,
        user_ref=body.user_ref,
        user_whatsapp=body.user_whatsapp,
        ceiling_paise=body.ceiling_paise,
        max_single_txn_paise=body.max_single_txn_paise,
        allowed_categories=body.allowed_categories,
        min_reversibility=body.min_reversibility,
    )
    return AgentRunOut(
        run_id=result.run_id,
        agent_did=result.agent_did,
        cart_id=result.cart_id,
        order_id=result.order_id,
        decision=result.decision,
        summary=result.summary,
    )
