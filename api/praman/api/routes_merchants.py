"""Merchant listing — frontend plumbing (`/onboard`'s and `/live`'s
merchant picker), not one of CLAUDE.md's ten MCP tools, so REST-only.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from praman.api.deps import DbSession
from praman.models import Merchant
from praman.schemas import MerchantOut

router = APIRouter(prefix="/api", tags=["merchants"])


def _to_out(m: Merchant) -> MerchantOut:
    return MerchantOut(
        id=m.id,
        name=m.name,
        did=m.did,
        whatsapp_number=m.whatsapp_number,
        onboarding_state=m.onboarding_state,
        agent_policy=m.agent_policy or {},
    )


@router.get("/merchants")
async def list_merchants(session: DbSession) -> list[MerchantOut]:
    result = await session.execute(select(Merchant).order_by(Merchant.created_at.desc()))
    return [_to_out(m) for m in result.scalars().all()]


@router.get("/merchants/{merchant_id}")
async def get_merchant(session: DbSession, merchant_id: str) -> MerchantOut:
    result = await session.execute(select(Merchant).where(Merchant.id == merchant_id))
    merchant = result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(status_code=404, detail="merchant not found")
    return _to_out(merchant)
