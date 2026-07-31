from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomerServicePolicy
from app.services.common import model_to_dict, utcnow


POLICY_FIELDS = (
    "id",
    "policy_code",
    "customer_code",
    "customer_name",
    "policy_type",
    "charge_status",
    "customer_scope",
    "effective_from",
    "effective_until",
    "repair_price",
    "currency",
    "tax_rate",
    "shipping_fee_text",
    "reply_salutation",
    "hide_company_name",
    "force_manual_review",
    "enabled",
    "source_file_name",
    "source_row_no",
    "imported_by_user_id",
    "imported_at",
    "created_at",
    "updated_at",
)
POLICY_MUTABLE_FIELDS = {
    "customer_name",
    "policy_type",
    "charge_status",
    "customer_scope",
    "effective_from",
    "effective_until",
    "repair_price",
    "currency",
    "tax_rate",
    "shipping_fee_text",
    "reply_salutation",
    "hide_company_name",
    "force_manual_review",
    "enabled",
}
FREE_POLICY_TYPES = {"permanent_free", "annual_free"}
CHARGE_STATUSES = {"free", "annual_contract", "chargeable", "manual_confirmation"}
CUSTOMER_SCOPES = {"domestic", "overseas"}


def charge_status_for_policy_type(policy_type: str) -> str:
    return {
        "permanent_free": "free",
        "annual_free": "annual_contract",
        "special_out_of_warranty": "chargeable",
    }.get(policy_type, "manual_confirmation")


def serialize_policy(policy: CustomerServicePolicy) -> dict[str, Any]:
    return model_to_dict(policy, POLICY_FIELDS)


def _validate_policy_values(values: dict[str, Any]) -> None:
    policy_type = str(values.get("policy_type") or "")
    charge_status = str(
        values.get("charge_status") or charge_status_for_policy_type(policy_type)
    )
    customer_scope = values.get("customer_scope")
    effective_from = values.get("effective_from")
    effective_until = values.get("effective_until")
    if policy_type == "annual_free" and values.get("enabled") and (not effective_from or not effective_until):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ANNUAL_POLICY_DATES_REQUIRED",
        )
    if effective_from and effective_until and effective_from > effective_until:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="POLICY_DATE_RANGE_INVALID",
        )
    if Decimal(str(values.get("repair_price", 0))) < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="POLICY_REPAIR_PRICE_INVALID")
    if not str(values.get("currency") or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="POLICY_CURRENCY_REQUIRED")
    if not str(values.get("shipping_fee_text") or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="POLICY_SHIPPING_FEE_REQUIRED")
    if charge_status not in CHARGE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="POLICY_CHARGE_STATUS_INVALID",
        )
    if values.get("enabled") and customer_scope not in CUSTOMER_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="POLICY_CUSTOMER_SCOPE_REQUIRED",
        )


async def list_policies(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    keyword: str | None = None,
    customer_code: str | None = None,
    policy_type: str | None = None,
    enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    filters = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        filters.append(
            or_(
                CustomerServicePolicy.customer_code.like(pattern),
                CustomerServicePolicy.customer_name.like(pattern),
                CustomerServicePolicy.policy_code.like(pattern),
            )
        )
    if customer_code:
        filters.append(CustomerServicePolicy.customer_code == customer_code.strip().upper())
    if policy_type:
        filters.append(CustomerServicePolicy.policy_type == policy_type)
    if enabled is not None:
        filters.append(CustomerServicePolicy.enabled.is_(enabled))
    total = int(
        (
            await session.scalar(
                select(func.count()).select_from(CustomerServicePolicy).where(*filters)
            )
        )
        or 0
    )
    rows = (
        await session.execute(
            select(CustomerServicePolicy)
            .where(*filters)
            .order_by(
                CustomerServicePolicy.customer_code,
                CustomerServicePolicy.policy_type,
                CustomerServicePolicy.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return [serialize_policy(row) for row in rows], total


async def create_policy(
    session: AsyncSession,
    *,
    values: dict[str, Any],
    user_id: int,
) -> dict[str, Any]:
    payload = dict(values)
    payload["customer_code"] = str(payload.get("customer_code") or "").strip().upper()
    payload["currency"] = str(payload.get("currency") or "CNY").strip().upper()
    payload["charge_status"] = str(
        payload.get("charge_status")
        or charge_status_for_policy_type(str(payload.get("policy_type") or ""))
    )
    if not payload["customer_code"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="POLICY_CUSTOMER_CODE_REQUIRED")
    _validate_policy_values(payload)
    existing = await session.scalar(
        select(CustomerServicePolicy).where(
            CustomerServicePolicy.policy_code == str(payload.get("policy_code") or "").strip()
        )
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="POLICY_CODE_EXISTS")
    policy = CustomerServicePolicy(
        **payload,
        imported_by_user_id=user_id,
        imported_at=utcnow(),
    )
    session.add(policy)
    await session.flush()
    return serialize_policy(policy)


async def update_policy(
    session: AsyncSession,
    *,
    policy_id: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    policy = await session.get(CustomerServicePolicy, policy_id, with_for_update=True)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="POLICY_NOT_FOUND")
    merged = serialize_policy(policy)
    merged.update({key: value for key, value in values.items() if key in POLICY_MUTABLE_FIELDS})
    if "policy_type" in values and "charge_status" not in values:
        merged["charge_status"] = charge_status_for_policy_type(
            str(merged.get("policy_type") or "")
        )
    if "currency" in merged:
        merged["currency"] = str(merged["currency"] or "").strip().upper()
    _validate_policy_values(merged)
    for key in POLICY_MUTABLE_FIELDS:
        if key in values:
            setattr(policy, key, merged[key])
    await session.flush()
    return serialize_policy(policy)


def _policy_snapshot(policy: CustomerServicePolicy, *, source: str) -> dict[str, Any]:
    return {
        "policy_id": policy.id,
        "policy_code": policy.policy_code,
        "customer_code": policy.customer_code,
        "policy_type": policy.policy_type,
        "charge_status": policy.charge_status,
        "customer_scope": policy.customer_scope,
        "repair_price": str(policy.repair_price),
        "currency": policy.currency,
        "tax_rate": str(policy.tax_rate),
        "shipping_fee_text": policy.shipping_fee_text,
        "reply_salutation": policy.reply_salutation,
        "hide_company_name": policy.hide_company_name,
        "force_manual_review": policy.force_manual_review,
        "source": source,
    }


async def resolve_customer_policy(
    session: AsyncSession,
    *,
    customer_code: str,
    requested_on: date,
    in_warranty: bool,
) -> dict[str, Any]:
    normalized = customer_code.strip().upper()
    candidates = (
        await session.execute(
            select(CustomerServicePolicy).where(
                CustomerServicePolicy.customer_code == normalized,
                CustomerServicePolicy.enabled.is_(True),
                or_(
                    CustomerServicePolicy.effective_from.is_(None),
                    CustomerServicePolicy.effective_from <= requested_on,
                ),
                or_(
                    CustomerServicePolicy.effective_until.is_(None),
                    CustomerServicePolicy.effective_until >= requested_on,
                ),
            )
        )
    ).scalars().all()

    forced_manual = [policy for policy in candidates if policy.force_manual_review]
    if forced_manual:
        return {
            "status": "conflict",
            "error_code": "CUSTOMER_POLICY_FORCES_MANUAL_REVIEW",
            "policy_codes": sorted(policy.policy_code for policy in forced_manual),
        }
    if not candidates:
        return {
            "status": "missing",
            "error_code": "CUSTOMER_POLICY_MISSING",
        }
    if len(candidates) > 1:
        return {
            "status": "conflict",
            "error_code": "CUSTOMER_POLICY_CONFLICT",
            "policy_codes": sorted(policy.policy_code for policy in candidates),
        }
    policy = candidates[0]
    if policy.customer_scope not in CUSTOMER_SCOPES:
        return {
            "status": "missing",
            "error_code": "CUSTOMER_SCOPE_UNRESOLVED",
            "policy_codes": [policy.policy_code],
        }
    snapshot = _policy_snapshot(policy, source="customer_policy")
    if in_warranty:
        snapshot["charge_status"] = "free"
        snapshot["repair_price"] = "0.00"
        snapshot["source"] = "sn_warranty+customer_policy"
    if snapshot["charge_status"] == "manual_confirmation":
        return {
            "status": "manual_confirmation",
            "error_code": "CUSTOMER_POLICY_REQUIRES_MANUAL_CONFIRMATION",
            "policy": snapshot,
        }
    return {"status": "resolved", "policy": snapshot}
