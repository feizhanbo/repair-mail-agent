from __future__ import annotations

from fastapi import APIRouter

from app.core.response import ok

router = APIRouter()


@router.get("/summary")
async def summary_placeholder() -> dict:
    return ok(
        {
            "new_emails": 0,
            "pending_parse": 0,
            "manual_review": 0,
            "need_customer_info": 0,
            "error": 0,
            "ready_for_export": 0,
            "ai_low_confidence": 0,
        }
    )

