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
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class EmailReparseRequest(BaseModel):
    mode: Literal["field_extract", "classification_and_extract"] = "field_extract"
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


class TicketItemUpsert(BaseModel):
    id: int | None = None
    line_no: int | None = Field(default=None, ge=1)
    material_code: str | None = Field(default=None, max_length=100)
    material_name: str | None = Field(default=None, max_length=255)
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


class ParseResultApplyRequest(BaseModel):
    action: Literal["apply", "partial_apply", "reject"] = "apply"
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
        "close_ticket",
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


class SnAssetImportItem(BaseModel):
    customer_code: str = Field(max_length=50)
    customer_name: str = Field(max_length=255)
    material_code: str = Field(max_length=100)
    material_name: str | None = Field(default=None, max_length=255)
    sn: str = Field(max_length=100)
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
    material_code: str = Field(max_length=100)
    material_name: str | None = Field(default=None, max_length=255)
    need_ship_to_beijing: bool = False
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


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    real_name: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    department: str | None = Field(default=None, max_length=100)
    status: Literal["active", "disabled"] = "active"
    roles: list[Literal["admin", "supervisor", "operator"]] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    real_name: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    department: str | None = Field(default=None, max_length=100)


class UserStatusRequest(BaseModel):
    status: Literal["active", "disabled"]


class UserRolesRequest(BaseModel):
    roles: list[Literal["admin", "supervisor", "operator"]] = Field(default_factory=list)


class UserResetPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ProfileUpdateRequest(BaseModel):
    real_name: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    department: str | None = Field(default=None, max_length=100)


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class ModelDumpMixin(BaseModel):
    model_config = ConfigDict(from_attributes=True)
