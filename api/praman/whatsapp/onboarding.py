"""Vendor onboarding state machine (the design spec's Phase 3):

    NEW -> AWAITING_NAME -> AWAITING_MEDIA -> EXTRACTING -> CONFIRMING_ITEMS
        -> SETTING_POLICY -> LIVE

AWAITING_NAME is a real addition, not in the original script: every
merchant used to be created as `f"Merchant {whatsapp_number}"` and never
asked for an actual shop name — harmless for the state machine itself,
but it meant a merchant's storefront, dashboard, and every agent-facing
listing displayed an ugly, unprofessional default forever unless someone
manually renamed the row afterward. Asking once, up front, fixes that at
the source instead of leaving it as a standing manual step.

Persisted in `Merchant.onboarding_state`. Every transition appends a ledger
event (which also publishes to the SSE bus, so `/onboard` can mirror the
thread live once the frontend exists) and logs the WhatsApp message.

Sandbox limitation: Twilio's WhatsApp Sandbox has no native interactive
buttons without a pre-approved content template, so every "[Yes] [No]" in
the spec's script is sent as plain text with an explicit reply instruction.
See `whatsapp/client.py`.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.adapters.llm import LLMClient
from praman.config import get_settings
from praman.core.ledger import append_event
from praman.crypto import did as did_module
from praman.crypto import keys as keys_module
from praman.ingest.extract import extract_from_image
from praman.ingest.normalise import normalise_batch
from praman.ingest.pipeline import DEFAULT_RESTOCKING_COST_PCT, slugify
from praman.models import Merchant, Product, WhatsAppMessage
from praman.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)

SPEND_OPTIONS_PAISE: dict[str, int] = {
    "1": 50_000,  # ₹500
    "2": 200_000,  # ₹2,000
    "3": 500_000,  # ₹5,000
}

_YES_WORDS = {"yes", "y", "haan", "ha"}
_NO_WORDS = {"no", "n", "nahi"}
_RUPEE_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _session_id_for(merchant: Merchant) -> str:
    return f"onboarding:{merchant.id}"


async def _log_and_emit(
    session: AsyncSession,
    merchant: Merchant,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await append_event(session, _session_id_for(merchant), merchant.did, event_type, payload)


async def _send(
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    merchant: Merchant,
    body: str,
) -> None:
    """Sends a reply — but a failed *delivery* must never block the state
    machine's actual work (extraction, product creation, state transitions)
    from happening or being recorded. A provider-side send failure (e.g. a
    Twilio account that can't send freeform WhatsApp messages) is logged and
    ledgered honestly as a failed delivery, not silently swallowed or
    allowed to crash the request.
    """
    sid: str | None = None
    delivery_error: str | None = None
    try:
        sid = await whatsapp.send_text(merchant.whatsapp_number, body)
    except Exception as exc:  # noqa: BLE001 — any provider/network failure, see docstring
        delivery_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "onboarding: failed to deliver WhatsApp reply to merchant %s: %s",
            merchant.id,
            delivery_error,
        )

    session.add(
        WhatsAppMessage(
            merchant_id=merchant.id,
            direction="outbound",
            wa_message_id=sid,
            body=body,
            media_urls=[],
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    payload: dict[str, Any] = {"body": body, "delivered": delivery_error is None}
    if delivery_error is not None:
        payload["delivery_error"] = delivery_error
    await _log_and_emit(session, merchant, "WHATSAPP_OUTBOUND", payload)


async def get_or_create_merchant(session: AsyncSession, whatsapp_number: str) -> Merchant:
    result = await session.execute(
        select(Merchant).where(Merchant.whatsapp_number == whatsapp_number)
    )
    merchant = result.scalar_one_or_none()
    if merchant is not None:
        return merchant

    private_key_hex, public_key_hex = keys_module.generate_keypair()
    private_key_enc = keys_module.encrypt_private_key(private_key_hex, get_settings().app_secret)
    merchant = Merchant(
        name=f"Merchant {whatsapp_number}",
        did=did_module.did_from_public_key(public_key_hex),
        public_key=public_key_hex,
        private_key_enc=private_key_enc,
        whatsapp_number=whatsapp_number,
        onboarding_state="NEW",
        agent_policy={},
        created_at=datetime.now(UTC),
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)
    await _log_and_emit(session, merchant, "MERCHANT_CREATED", {"whatsapp_number": whatsapp_number})
    return merchant


def _set_state(merchant: Merchant, state: str) -> None:
    merchant.onboarding_state = state


async def _transition(session: AsyncSession, merchant: Merchant, state: str) -> None:
    old = merchant.onboarding_state
    _set_state(merchant, state)
    await session.commit()
    await _log_and_emit(session, merchant, "ONBOARDING_STATE_CHANGED", {"from": old, "to": state})


async def _pending_review_products(session: AsyncSession, merchant: Merchant) -> list[Product]:
    result = await session.execute(
        select(Product)
        .where(Product.merchant_id == merchant.id, Product.needs_review.is_(True))
        .order_by(Product.id)
    )
    return list(result.scalars().all())


async def _ask_next_review_item(
    session: AsyncSession, whatsapp: WhatsAppClient, merchant: Merchant
) -> None:
    pending = await _pending_review_products(session, merchant)
    if not pending:
        await _transition(session, merchant, "SETTING_POLICY")
        await _send(
            session,
            whatsapp,
            merchant,
            "How much can an AI agent spend at your shop in one order without asking you?\n"
            "Reply 1 for ₹500, 2 for ₹2,000, 3 for ₹5,000 — or type your own amount in rupees.",
        )
        return

    item = pending[0]
    await _send(
        session,
        whatsapp,
        merchant,
        f"*{item.name}* — is the price ₹{item.unit_price_paise / 100:.2f}?\n"
        "Reply YES to confirm, or type the correct price.",
    )


async def _handle_new(session: AsyncSession, whatsapp: WhatsAppClient, merchant: Merchant) -> None:
    await _transition(session, merchant, "AWAITING_NAME")
    await _send(session, whatsapp, merchant, "Namaste! What's your shop's name?")


async def _handle_awaiting_name(
    session: AsyncSession, whatsapp: WhatsAppClient, merchant: Merchant, body: str
) -> None:
    name = body.strip()
    if not name:
        await _send(session, whatsapp, merchant, "Please reply with your shop's name.")
        return

    old_name = merchant.name
    merchant.name = name
    await session.commit()
    await _log_and_emit(session, merchant, "SHOP_NAME_SET", {"from": old_name, "to": name})

    await _transition(session, merchant, "AWAITING_MEDIA")
    await _send(
        session,
        whatsapp,
        merchant,
        f"Nice to meet you, {name}! Send photos of your price list or products — "
        "as many as you like.",
    )


async def _handle_awaiting_media(
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    merchant: Merchant,
    media: list[tuple[bytes, str]],
) -> None:
    if not media:
        await _send(
            session,
            whatsapp,
            merchant,
            "I need at least one photo of your price list or products to get started.",
        )
        return

    await _transition(session, merchant, "EXTRACTING")
    await _send(session, whatsapp, merchant, "Reading them…")

    extracted = []
    for image_bytes, mime_type in media:
        try:
            items = await extract_from_image(llm, image_bytes, mime_type=mime_type)
        except Exception:
            # One bad photo shouldn't sink the whole onboarding batch.
            logger.warning(
                "onboarding: failed to extract one photo for merchant %s",
                merchant.id,
                exc_info=True,
            )
            continue
        extracted.extend(items)

    normalised = normalise_batch(extracted)
    for product in normalised:
        session.add(
            Product(
                merchant_id=merchant.id,
                sku=slugify(product.name),
                name=product.name,
                category=product.category,
                category_class=product.category_class,
                unit_price_paise=product.unit_price_paise,
                stock=product.stock,
                return_window_days=product.return_window_days,
                fulfilment_hours=product.fulfilment_hours,
                restocking_cost_pct=DEFAULT_RESTOCKING_COST_PCT[product.category_class],
                is_personalised=product.is_personalised,
                field_confidence=product.field_confidence,
                needs_review=product.needs_review,
                source="whatsapp",
            )
        )
    await session.commit()

    clear_count = sum(1 for p in normalised if not p.needs_review)
    review_count = len(normalised) - clear_count
    await _log_and_emit(
        session,
        merchant,
        "CATALOG_EXTRACTED",
        {"total": len(normalised), "clear": clear_count, "needs_review": review_count},
    )

    await _transition(session, merchant, "CONFIRMING_ITEMS")
    await _send(
        session,
        whatsapp,
        merchant,
        f"Found *{len(normalised)} items*. {clear_count} are clear. "
        f"{review_count} need your confirmation.",
    )
    await _ask_next_review_item(session, whatsapp, merchant)


def _parse_rupees_to_paise(text: str) -> int | None:
    match = _RUPEE_NUMBER_RE.search(text)
    if not match:
        return None
    try:
        rupees = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return round(rupees * 100)


async def _handle_confirming_items(
    session: AsyncSession, whatsapp: WhatsAppClient, merchant: Merchant, body: str
) -> None:
    pending = await _pending_review_products(session, merchant)
    if not pending:
        await _ask_next_review_item(session, whatsapp, merchant)
        return

    item = pending[0]
    stripped = body.strip().lower()
    if stripped in _YES_WORDS:
        item.needs_review = False
    else:
        corrected_paise = _parse_rupees_to_paise(body)
        if corrected_paise is not None:
            item.unit_price_paise = corrected_paise
        else:
            item.name = body.strip()
        item.needs_review = False

    await session.commit()
    await _log_and_emit(
        session, merchant, "PRODUCT_CONFIRMED", {"product_id": item.id, "reply": body}
    )
    await _ask_next_review_item(session, whatsapp, merchant)


async def _handle_setting_policy(
    session: AsyncSession, whatsapp: WhatsAppClient, merchant: Merchant, body: str
) -> None:
    policy = dict(merchant.agent_policy or {})
    stripped = body.strip().lower()

    if "max_txn_paise" not in policy:
        paise = SPEND_OPTIONS_PAISE.get(stripped) or _parse_rupees_to_paise(body)
        if paise is None:
            await _send(
                session,
                whatsapp,
                merchant,
                "Sorry, I didn't catch that. Reply 1, 2, 3, or an amount in rupees.",
            )
            return
        policy["max_txn_paise"] = paise
        merchant.agent_policy = policy
        await session.commit()
        await _send(
            session,
            whatsapp,
            merchant,
            "Should I hold non-returnable orders for your approval? Reply YES or NO.",
        )
        return

    if stripped in _YES_WORDS:
        policy["cooling_off_hold"] = True
    elif stripped in _NO_WORDS:
        policy["cooling_off_hold"] = False
    else:
        await _send(session, whatsapp, merchant, "Please reply YES or NO.")
        return

    merchant.agent_policy = policy
    await session.commit()
    await _log_and_emit(session, merchant, "POLICY_SET", policy)

    await _transition(session, merchant, "LIVE")
    await _send(
        session,
        whatsapp,
        merchant,
        "*You're live.* Your shop can now be found and bought from by AI shopping "
        f"agents. Dashboard: {merchant.did}",
    )


async def handle_inbound_whatsapp(
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    *,
    from_number: str,
    body: str,
    media: list[tuple[bytes, str]],
) -> Merchant:
    """The webhook's single entrypoint: log the inbound message, route by
    the merchant's current onboarding_state, dispatch to the matching
    handler."""
    merchant = await get_or_create_merchant(session, from_number)

    session.add(
        WhatsAppMessage(
            merchant_id=merchant.id,
            direction="inbound",
            body=body,
            media_urls=[],
            handled_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await _log_and_emit(
        session, merchant, "WHATSAPP_INBOUND", {"body": body, "media_count": len(media)}
    )

    state = merchant.onboarding_state
    if state == "NEW":
        await _handle_new(session, whatsapp, merchant)
    elif state == "AWAITING_NAME":
        await _handle_awaiting_name(session, whatsapp, merchant, body)
    elif state == "AWAITING_MEDIA":
        await _handle_awaiting_media(session, whatsapp, llm, merchant, media)
    elif state == "CONFIRMING_ITEMS":
        await _handle_confirming_items(session, whatsapp, merchant, body)
    elif state == "SETTING_POLICY":
        await _handle_setting_policy(session, whatsapp, merchant, body)
    elif state == "LIVE":
        await _send(
            session,
            whatsapp,
            merchant,
            "Your shop is already live! Visit your dashboard to manage your catalog.",
        )
    else:
        await _send(session, whatsapp, merchant, "Something went wrong — please try again.")

    return merchant
