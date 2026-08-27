"""Tests for `agent_runner/tools.py::AgentToolExecutor` — the "agent SDK"
layer that holds the demo keypair and signs each real HTTP call on the
model's behalf. Exercises it end to end (register → envelope → search →
quote → cart → checkout) over the real app via an ASGI transport, the
same pattern `test_api_routes.py` uses — no live network, no live Gemini
call (the model's own decision loop is `agent_runner/runner.py`'s job,
not this module's).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from praman.adapters.llm import FakeLLMClient
from praman.adapters.razorpay_client import FakeRazorpayClient
from praman.agent_runner import tools as tools_module
from praman.agent_runner.tools import AgentToolExecutor
from praman.api.deps import (
    get_db_session,
    get_llm_dep,
    get_razorpay_dep,
    get_redis_dep,
    get_whatsapp_dep,
)
from praman.config import get_settings
from praman.crypto import did as did_module
from praman.crypto.keys import encrypt_private_key, generate_keypair
from praman.main import app
from praman.models import Merchant, Product
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def wired_app(
    db_session: AsyncSession,
) -> tuple[Merchant, Product]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()

    async def _get_db_session() -> AsyncSession:
        return db_session

    app.dependency_overrides[get_db_session] = _get_db_session
    app.dependency_overrides[get_redis_dep] = lambda: redis
    app.dependency_overrides[get_razorpay_dep] = lambda: razorpay
    app.dependency_overrides[get_whatsapp_dep] = lambda: whatsapp
    app.dependency_overrides[get_llm_dep] = lambda: FakeLLMClient()

    priv, pub = generate_keypair()
    merchant = Merchant(
        name="Sharma General Store",
        did=did_module.did_from_public_key(pub),
        public_key=pub,
        private_key_enc=encrypt_private_key(priv, get_settings().app_secret),
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=datetime.now(UTC),
    )
    db_session.add(merchant)
    await db_session.commit()
    await db_session.refresh(merchant)

    product = Product(
        merchant_id=merchant.id,
        sku="toor-dal-1kg",
        name="Toor Dal 1kg",
        category="pulses",
        category_class="consumable",
        unit_price_paise=18000,
        stock=50,
        return_window_days=14,
        fulfilment_hours=12,
        restocking_cost_pct=0.0,
        is_personalised=False,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    yield merchant, product
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _route_executor_through_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file wants `AgentToolExecutor`'s internal HTTP
    calls to hit the in-process app, not real network — see
    `tools.py::_new_client`'s own docstring for why this seam exists."""

    def _asgi_client(base_url: str) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    monkeypatch.setattr(tools_module, "_new_client", _asgi_client)


def _executor(merchant: Merchant) -> AgentToolExecutor:
    return AgentToolExecutor(
        base_url="http://test",
        merchant_id=merchant.id,
        user_ref="agent-runner-test-user",
        user_whatsapp="whatsapp:+919000000099",
        ceiling_paise=100_000,
        max_single_txn_paise=100_000,
        allowed_categories=["pulses"],
        min_reversibility=0.0,
    )


async def test_full_shopping_sequence_completes_a_real_checkout(
    wired_app: tuple[Merchant, Product],
) -> None:
    merchant, product = wired_app
    executor = _executor(merchant)

    reg = await executor.execute("register_agent", {"operator": "test-suite"})
    assert "agent_did" in reg
    assert executor.agent_did is not None

    env = await executor.execute("envelope_submit", {})
    assert "envelope_id" in env
    assert executor.envelope_id is not None

    search = await executor.execute("catalog_search", {"q": "toor"})
    assert any(p["product_id"] == product.id for p in search["products"])

    quote = await executor.execute("quote_request", {"product_id": product.id, "qty": 1})
    assert "quote_id" in quote
    assert quote["unit_price_paise"] == 18000

    cart = await executor.execute("cart_confirm", {"quote_ids": [quote["quote_id"]]})
    assert "cart_id" in cart
    assert cart["band"] in ("green", "amber", "red")

    checkout = await executor.execute("checkout_execute", {"cart_id": cart["cart_id"]})
    assert checkout["decision"] in ("ALLOW", "HOLD", "ESCALATE", "SUBSTITUTE", "BLOCK")


async def test_quote_request_before_registration_returns_an_error_not_a_crash(
    wired_app: tuple[Merchant, Product],
) -> None:
    merchant, product = wired_app
    executor = _executor(merchant)

    result = await executor.execute("quote_request", {"product_id": product.id, "qty": 1})
    assert "error" in result


async def test_checkout_with_unknown_cart_id_returns_an_error(
    wired_app: tuple[Merchant, Product],
) -> None:
    merchant, _product = wired_app
    executor = _executor(merchant)
    await executor.execute("register_agent", {"operator": "test-suite"})

    result = await executor.execute("checkout_execute", {"cart_id": "does-not-exist"})
    assert result == {"error": "unknown cart_id -- call cart_confirm first"}


async def test_unknown_tool_name_returns_an_error(wired_app: tuple[Merchant, Product]) -> None:
    merchant, _product = wired_app
    executor = _executor(merchant)

    result = await executor.execute("delete_everything", {})
    assert result == {"error": "unknown tool delete_everything"}
