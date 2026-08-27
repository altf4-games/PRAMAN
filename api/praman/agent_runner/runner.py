"""The genuine LLM-driven shopping agent. Given a natural-language goal
and a merchant/envelope context, the model itself decides which tools to
call and in what order, using Gemini's native function calling against
the tool set in `agent_runner/tools.py` -- the same real, signed HTTP
calls an external MCP-calling agent would make (`mcp/server.py` wraps the
identical routes). Every step is published to the SSE bus under
`agent:{run_id}` as it happens, so a frontend watching that channel sees
the model's own reasoning and tool calls arrive live, not a scripted
sequence a page's own JS decided to run.

This module is explicitly *not* part of the money path in the sense
CLAUDE.md's non-negotiable rule means: the model never runs inside
`core/gate.py`/`core/envelope.py`/`core/reversibility.py`, and every call
it makes goes through the exact same real REST routes (and therefore the
exact same real gate) any other caller would have to. The LLM decides
*what to ask for*; it has no way to influence *whether the gate allows
it*.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google import genai
from google.genai import types

from praman.agent_runner.tools import TOOL_DECLARATIONS, AgentToolExecutor
from praman.config import Settings
from praman.events import BusEvent, bus

_MAX_TURNS = 10

_SYSTEM_INSTRUCTION = """You are an autonomous AI shopping agent, transacting on behalf of a
real buyer against a merchant's storefront through the PRAMAN protocol.

You have your own cryptographic identity and a merchant-countersigned spending envelope (a
one-time consent with a ceiling). You never see or handle card details or hold money
yourself -- after you check out, the platform's own policy gate decides whether the purchase
completes immediately, is held for a buyer cooling-off window, or needs the merchant's
approval. All three are normal, expected outcomes, not failures on your part.

Work through the buyer's goal step by step, in order:
1. register_agent -- establish your own identity.
2. envelope_submit -- get your spending consent.
3. catalog_search -- find the product(s) that match the goal.
4. quote_request -- get a signed price/stock quote for the exact item and quantity requested.
5. cart_confirm -- build a cart from the quote(s) you requested.
6. checkout_execute -- run checkout.

Do not ask the buyer for permission between steps -- you are operating autonomously within
the envelope you were granted. After checkout_execute returns, explain the outcome in one or
two plain-language sentences: mention the price, and if the order was held or escalated, say
why and what happens next.
"""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    run_id: str
    agent_did: str | None
    cart_id: str | None
    order_id: str | None
    decision: str | None
    summary: str


async def _publish(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    await bus.publish(
        BusEvent(
            session_id=f"agent:{run_id}",
            event_type=event_type,
            payload={
                "event_id": uuid.uuid4().hex,
                "ts": datetime.now(UTC).isoformat(),
                **payload,
            },
        )
    )


def _response_text(response: Any) -> str:
    candidates = response.candidates or []
    if not candidates or not candidates[0].content or not candidates[0].content.parts:
        return ""
    return " ".join(
        part.text.strip() for part in candidates[0].content.parts if part.text and part.text.strip()
    )


async def run_agent(
    settings: Settings,
    *,
    run_id: str,
    goal: str,
    merchant_id: str,
    merchant_name: str,
    user_ref: str,
    user_whatsapp: str,
    ceiling_paise: int,
    max_single_txn_paise: int,
    allowed_categories: list[str],
    min_reversibility: float,
) -> AgentRunResult:
    executor = AgentToolExecutor(
        base_url=settings.public_base_url,
        merchant_id=merchant_id,
        user_ref=user_ref,
        user_whatsapp=user_whatsapp,
        ceiling_paise=ceiling_paise,
        max_single_txn_paise=max_single_txn_paise,
        allowed_categories=allowed_categories,
        min_reversibility=min_reversibility,
    )

    client = genai.Client(api_key=settings.llm_api_key)
    tool = types.Tool(function_declarations=TOOL_DECLARATIONS)
    chat = client.aio.chats.create(
        model=settings.gemini_model,
        config=types.GenerateContentConfig(tools=[tool], system_instruction=_SYSTEM_INSTRUCTION),
    )

    user_message = (
        f"Shop: {merchant_name} (merchant_id={merchant_id}). "
        f"Allowed categories: {', '.join(allowed_categories) or 'any'}. "
        f"Your spending envelope ceiling will be ₹{ceiling_paise / 100:.2f}. "
        f"Buyer's goal: {goal}"
    )
    await _publish(run_id, "AGENT_GOAL", {"goal": goal, "merchant_id": merchant_id})

    decision: str | None = None
    cart_id: str | None = None
    order_id: str | None = None
    final_text = ""

    response = await chat.send_message(user_message)
    for _turn in range(_MAX_TURNS):
        thought = _response_text(response)
        if thought:
            await _publish(run_id, "AGENT_THOUGHT", {"text": thought})

        calls = response.function_calls
        if not calls:
            final_text = thought or final_text
            break

        response_parts: list[Any] = []
        for call in calls:
            args = dict(call.args or {})
            await _publish(run_id, "AGENT_TOOL_CALL", {"tool": call.name, "args": args})
            result = await executor.execute(call.name or "", args)
            await _publish(run_id, "AGENT_TOOL_RESULT", {"tool": call.name, "result": result})

            if call.name == "cart_confirm" and "cart_id" in result:
                cart_id = result["cart_id"]
            if call.name == "checkout_execute":
                decision = result.get("decision")
                order_id = result.get("order_id")

            response_parts.append(
                types.Part.from_function_response(name=call.name or "", response=result)
            )

        response = await chat.send_message(response_parts)
    else:
        final_text = _response_text(response) or "Reached the step limit without finishing."

    await _publish(
        run_id,
        "AGENT_DONE",
        {"summary": final_text, "decision": decision, "cart_id": cart_id, "order_id": order_id},
    )

    return AgentRunResult(
        run_id=run_id,
        agent_did=executor.agent_did,
        cart_id=cart_id,
        order_id=order_id,
        decision=decision,
        summary=final_text,
    )
