"""Pydantic request/response schemas. Populated incrementally as each
phase's routes are built; Phase 1 only needs the ledger-verification shape.
"""

from __future__ import annotations

from pydantic import BaseModel


class ChainVerifyResponse(BaseModel):
    ok: bool
    first_bad_index: int | None
    expected: str | None
    actual: str | None
    checked: int
