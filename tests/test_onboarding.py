"""End-to-end walk through the onboarding state machine (CLAUDE.md Phase 3):
NEW -> AWAITING_MEDIA -> EXTRACTING -> CONFIRMING_ITEMS -> SETTING_POLICY -> LIVE.
"""

from __future__ import annotations

import json

from praman.adapters.llm import FakeLLMClient
from praman.config import get_settings
from praman.crypto.keys import decrypt_private_key
from praman.models import Merchant, Product
from praman.whatsapp.client import FakeWhatsAppClient
from praman.whatsapp.onboarding import handle_inbound_whatsapp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

WHATSAPP_NUMBER = "whatsapp:+919876543210"

_CLEAR_ITEM = {
    "name": "Toor Dal",
    "category": "pulses",
    "category_class": "consumable",
    "unit_price_paise": 18000,
    "stock": None,
    "return_window_days": 1,
    "fulfilment_hours": 24,
    "is_personalised": False,
    "field_confidence": {
        "name": 0.95,
        "category": 0.9,
        "category_class": 0.9,
        "unit_price_paise": 0.95,
        "stock": 0.0,
        "return_window_days": 0.85,
        "fulfilment_hours": 0.85,
        "is_personalised": 0.95,
    },
}

_REVIEW_ITEM = {
    "name": "Mystery Item",
    "category": "unknown",
    "category_class": "consumable",
    "unit_price_paise": 5000,
    "stock": None,
    "return_window_days": 1,
    "fulfilment_hours": 24,
    "is_personalised": False,
    "field_confidence": {
        "name": 0.4,  # illegible — genuinely low confidence, drives needs_review
        "category": 0.3,
        "category_class": 0.5,
        "unit_price_paise": 0.4,
        "stock": 0.0,
        "return_window_days": 0.85,
        "fulfilment_hours": 0.85,
        "is_personalised": 0.9,
    },
}


async def _get_merchant(session: AsyncSession) -> Merchant:
    result = await session.execute(
        select(Merchant).where(Merchant.whatsapp_number == WHATSAPP_NUMBER)
    )
    return result.scalar_one()


async def test_full_onboarding_flow(db_session: AsyncSession) -> None:
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    # --- NEW -> AWAITING_MEDIA ---
    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hi", media=[]
    )
    assert merchant.onboarding_state == "AWAITING_MEDIA"
    assert "Namaste" in whatsapp.sent_messages[-1].body

    # --- AWAITING_MEDIA -> EXTRACTING -> CONFIRMING_ITEMS ---
    llm.enqueue(json.dumps([_CLEAR_ITEM, _REVIEW_ITEM]))
    merchant = await handle_inbound_whatsapp(
        db_session,
        whatsapp,
        llm,
        from_number=WHATSAPP_NUMBER,
        body="",
        media=[(b"fake-image-bytes", "image/png")],
    )
    assert merchant.onboarding_state == "CONFIRMING_ITEMS"

    products_result = await db_session.execute(
        select(Product).where(Product.merchant_id == merchant.id)
    )
    products = list(products_result.scalars().all())
    assert len(products) == 2
    review_products = [p for p in products if p.needs_review]
    assert len(review_products) == 1
    assert review_products[0].name == "Mystery Item"

    found_message = next(m for m in whatsapp.sent_messages if "Found" in m.body)
    assert "2 items" in found_message.body
    assert "1 are clear" in found_message.body
    assert "1 need your confirmation" in found_message.body
    assert "Mystery Item" in whatsapp.sent_messages[-1].body

    # --- CONFIRMING_ITEMS: confirm the flagged item as-is ---
    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="yes", media=[]
    )
    assert merchant.onboarding_state == "SETTING_POLICY"

    await db_session.refresh(review_products[0])
    assert review_products[0].needs_review is False

    spend_prompt = whatsapp.sent_messages[-1].body
    assert "spend" in spend_prompt.lower()

    # --- SETTING_POLICY: spend limit, then cooling-off ---
    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="1", media=[]
    )
    assert merchant.onboarding_state == "SETTING_POLICY"
    assert merchant.agent_policy["max_txn_paise"] == 50_000
    assert "hold non-returnable" in whatsapp.sent_messages[-1].body.lower()

    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="yes", media=[]
    )
    assert merchant.onboarding_state == "LIVE"
    assert merchant.agent_policy["cooling_off_hold"] is True
    assert "You're live" in whatsapp.sent_messages[-1].body


async def test_new_merchant_private_key_is_encrypted_at_rest(db_session: AsyncSession) -> None:
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hi", media=[]
    )
    merchant = await _get_merchant(db_session)

    # stored value must not be the raw private key hex — and must decrypt
    # back to a key whose derived public key matches what we generated.
    assert merchant.private_key_enc != merchant.public_key
    decrypted = decrypt_private_key(merchant.private_key_enc, get_settings().app_secret)
    assert decrypted != merchant.private_key_enc
    assert len(decrypted) == 64  # 32-byte Ed25519 private key, hex-encoded


async def test_awaiting_media_with_no_photos_reprompts(db_session: AsyncSession) -> None:
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hi", media=[]
    )
    assert merchant.onboarding_state == "AWAITING_MEDIA"

    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="ok sending soon", media=[]
    )
    # no media attached yet — must stay in AWAITING_MEDIA, not advance
    assert merchant.onboarding_state == "AWAITING_MEDIA"
    assert "at least one photo" in whatsapp.sent_messages[-1].body


async def test_confirming_items_correction_updates_price(db_session: AsyncSession) -> None:
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hi", media=[]
    )
    llm.enqueue(json.dumps([_REVIEW_ITEM]))
    merchant = await handle_inbound_whatsapp(
        db_session,
        whatsapp,
        llm,
        from_number=WHATSAPP_NUMBER,
        body="",
        media=[(b"fake-image-bytes", "image/png")],
    )
    assert merchant.onboarding_state == "CONFIRMING_ITEMS"

    # correct the price instead of confirming
    await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="₹75", media=[]
    )

    result = await db_session.execute(select(Product).where(Product.merchant_id == merchant.id))
    product = result.scalar_one()
    assert product.needs_review is False
    assert product.unit_price_paise == 7500


async def test_setting_policy_rejects_unparseable_reply(db_session: AsyncSession) -> None:
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hi", media=[]
    )
    llm.enqueue(json.dumps([_CLEAR_ITEM]))
    await handle_inbound_whatsapp(
        db_session,
        whatsapp,
        llm,
        from_number=WHATSAPP_NUMBER,
        body="",
        media=[(b"fake-image-bytes", "image/png")],
    )
    merchant = await _get_merchant(db_session)
    assert merchant.onboarding_state == "SETTING_POLICY"

    merchant = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="huh?", media=[]
    )
    assert merchant.onboarding_state == "SETTING_POLICY"
    assert "didn't catch that" in whatsapp.sent_messages[-1].body


async def test_message_after_live_gets_a_polite_reply(db_session: AsyncSession) -> None:
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    merchant = await _seed_live_merchant(db_session, whatsapp, llm)

    result = await handle_inbound_whatsapp(
        db_session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hello again", media=[]
    )
    assert result.onboarding_state == "LIVE"
    assert merchant.id == result.id
    assert "already live" in whatsapp.sent_messages[-1].body.lower()


async def _seed_live_merchant(
    session: AsyncSession, whatsapp: FakeWhatsAppClient, llm: FakeLLMClient
) -> Merchant:
    await handle_inbound_whatsapp(
        session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="hi", media=[]
    )
    llm.enqueue(json.dumps([_CLEAR_ITEM]))
    await handle_inbound_whatsapp(
        session,
        whatsapp,
        llm,
        from_number=WHATSAPP_NUMBER,
        body="",
        media=[(b"fake-image-bytes", "image/png")],
    )
    await handle_inbound_whatsapp(
        session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="1", media=[]
    )
    return await handle_inbound_whatsapp(
        session, whatsapp, llm, from_number=WHATSAPP_NUMBER, body="yes", media=[]
    )
