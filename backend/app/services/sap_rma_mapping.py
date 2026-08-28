from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.models import RepairTicket, RepairTicketItem, SnAsset


RMA1_REQUIRED_FIELDS = (
    "RequestID", "internalSN", "itemCode", "customer", "BPBillAddr", "BPCellular", "insID"
)
RMA1_OPTIONAL_MAPPED_FIELDS = (
    "itemName", "custmrName", "U_expdate", "U_BXDate", "U_FailurePhenomena",
    "BPE_Mail", "U_cur", "U_DeliveryPaid", "U_WSPrice",
)
RMA1_NULL_FIELDS = (
    "U_CustomerNum", "U_qttday", "U_IsNReplace", "U_IsNUrgent", "U_ChanPXL1",
    "U_Status", "U_Byer", "U_PrintBatch", "U_CloseDate", "U_JFBK",
    "U_ReleaseStatus", "U_IsNpaid", "U_EndDate", "U_Upgrade", "U_Pingbi",
    "U_BackDate", "U_outofdatereason", "U_CQDH", "U_wxht", "U_htfwhj",
    "U_Upgradesoft", "U_ISUP", "U_gongshi", "U_ysdate", "U_gzfl", "U_GZCODE",
)
RMA1_UNKNOWN_FIELDS = (
    "NAME1", "U_TEST", "U_BXName", "descrption", "U_FailureData", "U_Selftest",
    "U_Calibration", "BPShipAddr", "BPPhone1", "TEST", "u_memo", "U_Comments",
    "contctCode", "U_RepairPaid", "U_detail", "U_FailDataName", "U_MEMO3", "U_fmemo", "U_acccustomer",
)
RMA1_DATABASE_OWNED_FIELDS = ("callID", "U_ModVersion")
RMA1_ALL_FIELDS = (
    "NAME1", "callID", "U_CustomerNum", "internalSN", "itemName", "U_expdate",
    "U_qttday", "U_TEST", "U_IsNReplace", "U_IsNUrgent", "U_BXName", "U_BXDate",
    "U_ChanPXL1", "U_ModVersion", "U_WSPrice", "custmrName", "customer",
    "descrption", "U_cur", "U_DeliveryPaid", "U_RepairPaid", "U_FailureData",
    "U_Selftest", "U_Calibration", "U_FailurePhenomena", "BPShipAddr", "BPBillAddr",
    "contctCode", "BPPhone1", "BPE_Mail", "BPCellular", "TEST", "u_memo", "insID",
    "U_Status", "U_Byer", "U_Comments", "U_PrintBatch", "U_CloseDate", "U_JFBK",
    "U_ReleaseStatus", "U_IsNpaid", "U_EndDate", "U_Upgrade", "U_Pingbi",
    "U_BackDate", "U_outofdatereason", "U_CQDH", "U_wxht", "U_detail", "U_htfwhj",
    "U_Upgradesoft", "U_ISUP", "U_gongshi", "U_FailDataName", "U_ysdate", "U_MEMO3",
    "U_gzfl", "U_GZCODE", "U_fmemo", "U_acccustomer", "RequestID", "itemCode",
)

_MAX_LENGTHS = {
    "RequestID": 36, "internalSN": 36, "itemCode": 100, "customer": 15,
    "BPBillAddr": 254, "BPCellular": 50, "itemName": 200, "custmrName": 200,
    "U_FailurePhenomena": None, "contctCode": 245, "BPE_Mail": 100,
    "U_cur": 10, "U_DeliveryPaid": 50, "U_RepairPaid": 50,
}


class RmaSubmissionValidationError(ValueError):
    def __init__(self, missing: list[str] | None = None, invalid: list[str] | None = None):
        self.missing = missing or []
        self.invalid = invalid or []
        details = ",".join([*(f"missing:{v}" for v in self.missing), *(f"invalid:{v}" for v in self.invalid)])
        super().__init__(f"SAP_EXPORT_REQUIRED_FIELDS_MISSING:{details}")


@dataclass(frozen=True)
class RmaSubmissionDTO:
    request_id: UUID
    sn: str
    sql_parameters: dict[str, Any]


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def build_rma_submission(
    *,
    request_id: str,
    ticket: RepairTicket,
    item: RepairTicketItem,
    asset: SnAsset,
    policy: dict[str, Any],
) -> RmaSubmissionDTO:
    try:
        parsed_id = UUID(str(request_id))
    except (TypeError, ValueError) as exc:
        raise RmaSubmissionValidationError(invalid=["RequestID"]) from exc
    if parsed_id.version != 4 or str(parsed_id) != str(request_id).lower():
        raise RmaSubmissionValidationError(invalid=["RequestID"])

    currency = _clean(policy.get("currency"))
    if str(currency or "").upper() == "RMB":
        currency = "CNY"
    requested_on: date | datetime | None = ticket.request_date
    repair_price = _clean(policy.get("repair_price"))
    try:
        repair_price = Decimal(str(repair_price)) if repair_price not in (None, "") else None
    except (InvalidOperation, ValueError) as exc:
        raise RmaSubmissionValidationError(invalid=["U_WSPrice:type=decimal"]) from exc
    values: dict[str, Any] = {
        "RequestID": str(parsed_id),
        "internalSN": _clean(asset.sn),
        "itemCode": _clean(asset.material_code),
        "customer": _clean(asset.customer_code),
        "BPBillAddr": _clean(ticket.mailing_address),
        "BPCellular": _clean(ticket.contact_phone),
        "insID": asset.ins_id,
        "itemName": _clean(asset.material_name),
        "custmrName": _clean(asset.customer_name),
        "U_expdate": asset.warranty_end_date,
        "U_BXDate": requested_on,
        "U_FailurePhenomena": _clean(item.failure_description or ticket.problem_description),
        "BPE_Mail": _clean(ticket.contact_email),
        "U_cur": currency,
        "U_DeliveryPaid": _clean(policy.get("shipping_fee_text")),
        "U_WSPrice": repair_price,
    }
    missing = [name for name in RMA1_REQUIRED_FIELDS if values.get(name) in (None, "")]
    invalid: list[str] = []
    for name, limit in _MAX_LENGTHS.items():
        value = values.get(name)
        if limit is not None and value is not None and len(str(value)) > limit:
            invalid.append(f"{name}:max_length={limit}")
    if not isinstance(values.get("insID"), int):
        invalid.append("insID:type=int")
    if item.sn_asset_id != asset.id or str(item.sn or "").strip().upper() != str(asset.sn).strip().upper():
        invalid.append("internalSN:stale_asset_link")
    if missing or invalid:
        raise RmaSubmissionValidationError(missing=missing, invalid=invalid)
    return RmaSubmissionDTO(request_id=parsed_id, sn=str(values["internalSN"]), sql_parameters=values)


assert set(RMA1_ALL_FIELDS) == (
    set(RMA1_REQUIRED_FIELDS)
    | set(RMA1_OPTIONAL_MAPPED_FIELDS)
    | set(RMA1_NULL_FIELDS)
    | set(RMA1_UNKNOWN_FIELDS)
    | set(RMA1_DATABASE_OWNED_FIELDS)
)
assert len(RMA1_ALL_FIELDS) == 63
