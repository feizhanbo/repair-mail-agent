from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


NEW_REPAIR_REQUIRED_FIELDS: tuple[str, ...] = (
    "customer_name",
    "contact_person",
    "contact_phone",
    "contact_email",
    "request_date",
    "mailing_address",
    "problem_description",
)

CUSTOMER_REQUIRED_FIELD_MESSAGES: dict[str, str] = {
    "customer_name": "缺少客户名称。",
    "contact_person": "缺少联系人。",
    "contact_phone": "缺少联系电话。",
    "contact_email": "缺少可用于回复客户的邮箱地址。",
    "request_date": "缺少报修日期。",
    "mailing_address": "缺少设备维修后的寄回地址。",
    "problem_description": "缺少明确的故障描述。",
    "sn": "缺少设备 SN，无法校验资产。",
}

CUSTOMER_REQUIRED_FIELD_SET = frozenset((*NEW_REPAIR_REQUIRED_FIELDS, "sn"))
FOLLOWUP_REPLY_TYPES = frozenset({"missing_fields", "followup"})


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _has_item_sn(items: Iterable[Mapping[str, Any]] | None) -> bool:
    return any(not _missing(item.get("sn")) for item in (items or ()))


def required_missing_for_values(
    *,
    intent_type: str | None,
    fields: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]] | None,
    reported_missing: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return only customer-actionable missing fields for a parsed email."""
    if intent_type in {"new_repair", "thread_new_repair"}:
        missing = {
            name: CUSTOMER_REQUIRED_FIELD_MESSAGES[name]
            for name in NEW_REPAIR_REQUIRED_FIELDS
            if _missing(fields.get(name))
        }
        if not _has_item_sn(items):
            missing["sn"] = CUSTOMER_REQUIRED_FIELD_MESSAGES["sn"]
        return missing
    if intent_type == "customer_supplement":
        return {
            str(name): str(reason or CUSTOMER_REQUIRED_FIELD_MESSAGES[str(name)])
            for name, reason in (reported_missing or {}).items()
            if str(name) in CUSTOMER_REQUIRED_FIELD_SET
        }
    return {}


def required_missing_for_ticket(ticket: Any, items: Iterable[Any]) -> dict[str, str]:
    missing = {
        name: CUSTOMER_REQUIRED_FIELD_MESSAGES[name]
        for name in NEW_REPAIR_REQUIRED_FIELDS
        if _missing(getattr(ticket, name, None))
    }
    if not any(not _missing(getattr(item, "sn", None)) for item in items):
        missing["sn"] = CUSTOMER_REQUIRED_FIELD_MESSAGES["sn"]
    return missing


def is_followup_reply_type(reply_type: str | None) -> bool:
    return (reply_type or "") in FOLLOWUP_REPLY_TYPES
