from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class EmailIngestRequest(BaseModel):
    mailbox_account: str = Field(default="manual", max_length=255)
    folder_name: str | None = Field(default="INBOX", max_length=255)
    imap_uid: str | None = Field(default=None, max_length=100)
    fetch_job_run_id: int | None = None
    message_id: str | None = Field(default=None, max_length=500)
    in_reply_to: str | None = Field(default=None, max_length=500)
    references_header: str | None = None
    from_address: str = Field(max_length=500)
    to_addresses: str | None = None
    cc_addresses: str | None = None
    subject: str | None = Field(default=None, max_length=500)
    text_body: str | None = None
    html_body: str | None = None
    sent_at: datetime | None = None
    received_at: datetime | None = None
    raw_eml_oss_object_id: int | None = None
    raw_eml_sha256: str | None = Field(default=None, max_length=64)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class EmailReparseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class EmailThreadMergeRequest(BaseModel):
    target_thread_id: int
    reason: str | None = Field(default=None, max_length=500)


class EmailThreadSplitRequest(BaseModel):
    email_ids: list[int] = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=500)


class TicketFieldPatchRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=500)


class TicketTransitionRequest(BaseModel):
    to_status_code: str = Field(max_length=50)
    trigger_event: str = Field(max_length=100)
    reason: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] | None = None


class DeviceReceivedConfirmRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


class TicketExportConfirmRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class SapSubmitReconcileRequest(BaseModel):
    outcome: Literal["accepted", "not_inserted"]
    reason: str = Field(min_length=1, max_length=500)
    call_id: str | None = Field(default=None, max_length=191)


class RmaManualPolicyApprovalRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    confirm_policy_values: Literal[True]
    confirm_template_thread_and_archive: Literal[True]


class TicketPolicyOverrideRequest(BaseModel):
    charge_status: Literal["free", "annual_contract", "chargeable", "manual_confirmation"]
    customer_scope: Literal["domestic", "overseas"] | None = None
    reason: str = Field(min_length=3, max_length=500)


class TicketReturnRouteManualRequest(BaseModel):
    return_location: Literal["beijing", "tianjin"]
    reason: str = Field(min_length=3, max_length=500)


class TicketItemUpsert(BaseModel):
    id: int | None = None
    line_no: int | None = Field(default=None, ge=1)
    material_code: str | None = Field(default=None, max_length=100)
    material_name: str | None = Field(default=None, max_length=255)
    board_code: str | None = Field(default=None, max_length=100)
    board_name: str | None = Field(default=None, max_length=255)
    sn: str | None = Field(default=None, max_length=100)
    quantity: int | None = Field(default=None, ge=1)
    failure_description: str | None = None
    failure_information: str | None = None
    data_info: str | None = None
    remarks: str | None = None
    accessories: str | None = Field(default=None, max_length=500)
    manual_locked: bool | None = None


class TicketItemsPatchRequest(BaseModel):
    items: list[TicketItemUpsert] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=500)


class TicketOwnerUpdateRequest(BaseModel):
    owner_user_id: int
    reason: str = Field(min_length=1, max_length=500)


class ParseResultApplyRequest(BaseModel):
    action: Literal["apply", "partial_apply", "reject"] = "apply"
    selected_fields: list[str] = Field(default_factory=list)
    selected_item_indices: list[int] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=500)


class ManualTaskAssignRequest(BaseModel):
    assigned_user_id: int | None = None
    reason: str | None = Field(default=None, max_length=500)


class ManualTaskResolveRequest(BaseModel):
    resolution: str = Field(min_length=1)
    resolution_type: str | None = Field(default=None, max_length=50)
    result_payload: dict[str, Any] | None = None
    next_action: Literal[
        "transition_ready_for_export",
        "generate_followup",
        "wait_customer_info",
        "reparse",
        "keep_manual_review",
    ] = "keep_manual_review"


class ManualTaskReparseRequest(BaseModel):
    mode: Literal["field_extract", "classification_and_extract"] = "field_extract"
    reason: str | None = Field(default=None, max_length=500)


class ReplyDraftRequest(BaseModel):
    reply_type: str | None = Field(default=None, max_length=50)
    related_email_id: int | None = None
    language: str = Field(default="zh-CN", max_length=20)
    missing_fields: dict[str, Any] | None = None


class ReplyUpdateRequest(BaseModel):
    subject: str | None = Field(default=None, max_length=500)
    final_body: str | None = None
    to_addresses: str | None = None
    cc_addresses: str | None = None


class ReplyRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ReplySendReconcileRequest(BaseModel):
    outcome: Literal["sent", "failed"]
    reason: str = Field(min_length=1, max_length=500)
    smtp_message_id: str | None = Field(default=None, max_length=500)


class SnAssetImportItem(BaseModel):
    customer_code: str = Field(max_length=50)
    customer_name: str = Field(max_length=255)
    material_code: str = Field(max_length=100)
    material_name: str | None = Field(default=None, max_length=255)
    sn: str = Field(max_length=100)
    service_tracking_card_no: str | None = Field(default=None, max_length=100)
    parent_sn: str | None = Field(default=None, max_length=100)
    top_sn: str | None = Field(default=None, max_length=100)
    parent_material_code: str | None = Field(default=None, max_length=100)
    top_material_code: str | None = Field(default=None, max_length=100)
    asset_status: str = Field(default="valid", max_length=30)
    warranty_start_date: date | None = None
    warranty_end_date: date | None = None
    source_row_no: int | None = None
    raw_data: dict[str, Any] | None = None


class SnAssetImportRequest(BaseModel):
    items: list[SnAssetImportItem]
    source_file_name: str | None = Field(default=None, max_length=255)
    source_file_hash: str | None = Field(default=None, max_length=64)


class BoardCardImportItem(BaseModel):
    board_code: str | None = Field(default=None, max_length=100)
    board_name: str | None = Field(default=None, max_length=255)
    return_location: Literal["beijing", "tianjin"] | None = None
    route_type: Literal["board_rule", "scope_default"] = "board_rule"
    customer_scope: Literal["domestic", "overseas"] = "domestic"
    # Deprecated compatibility aliases.
    material_code: str | None = Field(default=None, max_length=100)
    material_name: str | None = Field(default=None, max_length=255)
    need_ship_to_beijing: bool | None = None
    shipping_address: str | None = Field(default=None, max_length=500)
    shipping_contact: str | None = Field(default=None, max_length=100)
    shipping_phone: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    status: str = Field(default="active", max_length=20)
    source_row_no: int | None = None
    raw_data: dict[str, Any] | None = None


class BoardCardImportRequest(BaseModel):
    items: list[BoardCardImportItem]
    source_file_name: str | None = Field(default=None, max_length=255)
    source_file_hash: str | None = Field(default=None, max_length=64)


class CustomerServicePolicyCreateRequest(BaseModel):
    policy_code: str = Field(min_length=1, max_length=100)
    customer_code: str = Field(min_length=1, max_length=50)
    customer_name: str | None = Field(default=None, max_length=255)
    policy_type: Literal[
        "default",
        "permanent_free",
        "annual_free",
        "special_out_of_warranty",
    ]
    charge_status: Literal["free", "annual_contract", "chargeable", "manual_confirmation"] | None = None
    customer_scope: Literal["domestic", "overseas"] | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    repair_price: float = Field(ge=0)
    currency: str = Field(default="CNY", min_length=1, max_length=10)
    tax_rate: float = Field(default=13, ge=0, le=100)
    shipping_fee_text: str = Field(default="one-way charge/单次收费", min_length=1, max_length=100)
    reply_salutation: str | None = Field(default=None, max_length=100)
    hide_company_name: bool = False
    force_manual_review: bool = False
    enabled: bool = True


class CustomerServicePolicyUpdateRequest(BaseModel):
    customer_name: str | None = Field(default=None, max_length=255)
    policy_type: Literal[
        "default",
        "permanent_free",
        "annual_free",
        "special_out_of_warranty",
    ] | None = None
    charge_status: Literal["free", "annual_contract", "chargeable", "manual_confirmation"] | None = None
    customer_scope: Literal["domestic", "overseas"] | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    repair_price: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=1, max_length=10)
    tax_rate: float | None = Field(default=None, ge=0, le=100)
    shipping_fee_text: str | None = Field(default=None, min_length=1, max_length=100)
    reply_salutation: str | None = Field(default=None, max_length=100)
    hide_company_name: bool | None = None
    force_manual_review: bool | None = None
    enabled: bool | None = None


class SystemConfigUpdateRequest(BaseModel):
    auto_send_enabled: bool | None = None
    auto_followup_enabled: bool | None = None
    rma_auto_send_enabled: bool | None = None
    reply_send_mode: Literal["human_review", "auto_send"] | None = Field(
        default=None,
        json_schema_extra={"deprecated": True},
    )
    auto_apply_min_confidence: float | None = Field(default=None, ge=0, le=1)
    auto_send_min_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    max_follow_up: int | None = Field(default=None, ge=1, le=10)


class IdsRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


class ReplyTemplateCreateRequest(BaseModel):
    template_code: str = Field(min_length=1, max_length=100)
    template_name: str = Field(min_length=1, max_length=100)
    template_type: str = Field(min_length=1, max_length=50)
    language: str = Field(default="zh-CN", min_length=1, max_length=20)
    version: str = Field(default="1", min_length=1, max_length=30)
    subject_template: str | None = Field(default=None, max_length=500)
    body_template: str = Field(min_length=1)
    enabled: bool = True


class ReplyTemplateUpdateRequest(BaseModel):
    template_name: str | None = Field(default=None, min_length=1, max_length=100)
    subject_template: str | None = Field(default=None, max_length=500)
    body_template: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    real_name: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    status: Literal["active", "disabled"] = "active"
    roles: list[Literal["admin", "operator"]] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    real_name: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)


class UserStatusRequest(BaseModel):
    status: Literal["active", "disabled"]


class UserRolesRequest(BaseModel):
    roles: list[Literal["admin", "operator"]] = Field(default_factory=list)


class UserResetPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ProfileUpdateRequest(BaseModel):
    real_name: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class ModelDumpMixin(BaseModel):
    model_config = ConfigDict(from_attributes=True)
