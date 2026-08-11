from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


CLASSIFICATION_VERSION = "rma-layered-v1"


class HandlingLevel(StrEnum):
    AUTO_REPAIR = "auto_repair"
    MANUAL_RMA_BUSINESS = "manual_rma_business"
    LIFECYCLE_ONLY = "lifecycle_only"
    UNKNOWN = "unknown"


class EmailIntent(StrEnum):
    NEW_REPAIR = "new_repair"
    THREAD_NEW_REPAIR = "thread_new_repair"
    CUSTOMER_SUPPLEMENT = "customer_supplement"
    COMPONENT_REPLACEMENT_REPAIR = "component_replacement_repair"
    ONSITE_SERVICE = "onsite_service"
    WARRANTY_STATUS_INQUIRY = "warranty_status_inquiry"
    REPAIR_THREAD_OTHER = "repair_thread_other"
    DEVICE_INTAKE_RECEIVED = "device_intake_received"
    REPAIRED_DEVICE_DISPATCHED = "repaired_device_dispatched"
    CUSTOMER_REPAIRED_DEVICE_RECEIVED = "customer_repaired_device_received"
    CONTRACT_CONFIRMATION = "contract_confirmation"
    INVOICE = "invoice"
    THIRD_PARTY_EQUIPMENT_QUOTE = "third_party_equipment_quote"
    UNKNOWN = "unknown"


INTENT_LEVEL: dict[str, HandlingLevel] = {
    EmailIntent.NEW_REPAIR: HandlingLevel.AUTO_REPAIR,
    EmailIntent.THREAD_NEW_REPAIR: HandlingLevel.AUTO_REPAIR,
    EmailIntent.CUSTOMER_SUPPLEMENT: HandlingLevel.AUTO_REPAIR,
    EmailIntent.COMPONENT_REPLACEMENT_REPAIR: HandlingLevel.MANUAL_RMA_BUSINESS,
    EmailIntent.ONSITE_SERVICE: HandlingLevel.MANUAL_RMA_BUSINESS,
    EmailIntent.WARRANTY_STATUS_INQUIRY: HandlingLevel.MANUAL_RMA_BUSINESS,
    EmailIntent.REPAIR_THREAD_OTHER: HandlingLevel.MANUAL_RMA_BUSINESS,
    EmailIntent.DEVICE_INTAKE_RECEIVED: HandlingLevel.LIFECYCLE_ONLY,
    EmailIntent.REPAIRED_DEVICE_DISPATCHED: HandlingLevel.LIFECYCLE_ONLY,
    EmailIntent.CUSTOMER_REPAIRED_DEVICE_RECEIVED: HandlingLevel.LIFECYCLE_ONLY,
    EmailIntent.CONTRACT_CONFIRMATION: HandlingLevel.LIFECYCLE_ONLY,
    EmailIntent.INVOICE: HandlingLevel.LIFECYCLE_ONLY,
    EmailIntent.THIRD_PARTY_EQUIPMENT_QUOTE: HandlingLevel.LIFECYCLE_ONLY,
    EmailIntent.UNKNOWN: HandlingLevel.UNKNOWN,
}

AUTO_INTENTS = frozenset(
    {EmailIntent.NEW_REPAIR, EmailIntent.THREAD_NEW_REPAIR, EmailIntent.CUSTOMER_SUPPLEMENT}
)
MANUAL_INTENTS = frozenset(intent for intent, level in INTENT_LEVEL.items() if level == HandlingLevel.MANUAL_RMA_BUSINESS)
LIFECYCLE_INTENTS = frozenset(intent for intent, level in INTENT_LEVEL.items() if level == HandlingLevel.LIFECYCLE_ONLY)


LEGACY_INTENT_ALIASES = {
    "customer_reply": EmailIntent.CUSTOMER_SUPPLEMENT,
    "internal_forward": EmailIntent.REPAIR_THREAD_OTHER,
    "normal_reply": EmailIntent.REPAIR_THREAD_OTHER,
    "device_received": EmailIntent.DEVICE_INTAKE_RECEIVED,
}


@dataclass(frozen=True)
class ClassificationDecision:
    handling_level: str
    intent_type: str
    reason_code: str


def normalize_intent(value: str | None, *, allow_legacy: bool = True) -> str:
    normalized = str(value or EmailIntent.UNKNOWN).strip().lower()
    if allow_legacy:
        normalized = str(LEGACY_INTENT_ALIASES.get(normalized, normalized))
    return normalized if normalized in INTENT_LEVEL else str(EmailIntent.UNKNOWN)


def decision_for_intent(value: str | None, *, reason_code: str = "CLASSIFIED") -> ClassificationDecision:
    intent = normalize_intent(value)
    return ClassificationDecision(
        handling_level=str(INTENT_LEVEL[intent]),
        intent_type=intent,
        reason_code=reason_code,
    )


def classification_catalog() -> list[dict[str, str]]:
    labels = {
        EmailIntent.NEW_REPAIR: "新报修",
        EmailIntent.THREAD_NEW_REPAIR: "回复链新报修",
        EmailIntent.CUSTOMER_SUPPLEMENT: "客户补充报修信息",
        EmailIntent.COMPONENT_REPLACEMENT_REPAIR: "物料/元器件替换维修",
        EmailIntent.ONSITE_SERVICE: "叫修/现场服务",
        EmailIntent.WARRANTY_STATUS_INQUIRY: "保修状态咨询",
        EmailIntent.REPAIR_THREAD_OTHER: "报修回复链其他问题",
        EmailIntent.DEVICE_INTAKE_RECEIVED: "待维修设备到达/入库通知",
        EmailIntent.REPAIRED_DEVICE_DISPATCHED: "维修后设备发出",
        EmailIntent.CUSTOMER_REPAIRED_DEVICE_RECEIVED: "客户收到维修设备",
        EmailIntent.CONTRACT_CONFIRMATION: "合同确认",
        EmailIntent.INVOICE: "发票",
        EmailIntent.THIRD_PARTY_EQUIPMENT_QUOTE: "非我司设备报价单",
        EmailIntent.UNKNOWN: "未知",
    }
    return [
        {"handling_level": str(INTENT_LEVEL[intent]), "intent_type": str(intent), "label": labels[intent]}
        for intent in EmailIntent
    ]
