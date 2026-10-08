from __future__ import annotations

from enum import StrEnum
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.email_classification import EmailIntent


CLASSIFICATION_SCHEMA_VERSION = "rma-mail-classification-schema-v3"
ATTACHMENT_SCHEMA_VERSION = "rma-attachment-evidence-schema-v2"
REPAIR_EXTRACTION_SCHEMA_VERSION = "rma-repair-extraction-schema-v4"


class StrictAiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClassificationReasonCode(StrEnum):
    EXPLICIT_NEW_REPAIR = "EXPLICIT_NEW_REPAIR"
    THREAD_NEW_REPAIR = "THREAD_NEW_REPAIR"
    CUSTOMER_SUPPLEMENT = "CUSTOMER_SUPPLEMENT"
    COMPONENT_REPLACEMENT = "COMPONENT_REPLACEMENT"
    ONSITE_SERVICE = "ONSITE_SERVICE"
    WARRANTY_INQUIRY = "WARRANTY_INQUIRY"
    REPAIR_THREAD_OTHER = "REPAIR_THREAD_OTHER"
    DEVICE_INTAKE = "DEVICE_INTAKE"
    REPAIR_DISPATCHED = "REPAIR_DISPATCHED"
    CUSTOMER_RECEIVED = "CUSTOMER_RECEIVED"
    CONTRACT = "CONTRACT"
    INVOICE = "INVOICE"
    THIRD_PARTY_QUOTE = "THIRD_PARTY_QUOTE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    ATTACHMENT_REQUIRED = "ATTACHMENT_REQUIRED"


class ClassificationOutcomeCode(StrEnum):
    CLASSIFIED = "CLASSIFIED"
    PRECLASSIFICATION_PROVIDER_FAILED = "PRECLASSIFICATION_PROVIDER_FAILED"
    PRECLASSIFICATION_SCHEMA_FAILED = "PRECLASSIFICATION_SCHEMA_FAILED"
    PRECLASSIFICATION_LOW_CONFIDENCE = "PRECLASSIFICATION_LOW_CONFIDENCE"
    PRECLASSIFICATION_INTENT_CONFLICT = "PRECLASSIFICATION_INTENT_CONFLICT"
    SMOKE_FORCE = "SMOKE_FORCE"


class IntentCandidate(StrictAiModel):
    intent: EmailIntent
    confidence: float = Field(ge=0, le=1)


class ClassificationEvidence(StrictAiModel):
    source_type: Literal[
        "subject", "latest_message", "rfc_relation", "thread_context",
        "existing_ticket_context", "attachment_metadata", "attachment_content",
    ]
    text: str = Field(min_length=1, max_length=2000)
    attachment_id: int | None
    file_name: str | None

    @model_validator(mode="after")
    def validate_attachment_provenance(self) -> "ClassificationEvidence":
        if self.source_type in {"attachment_metadata", "attachment_content"} and not self.file_name:
            raise ValueError("ATTACHMENT_EVIDENCE_FILE_NAME_REQUIRED")
        return self


class ClassificationAttachmentMetadata(StrictAiModel):
    file_name: str
    content_type: str | None = None
    file_size: int | None = Field(default=None, ge=0)
    summary: str | None = None
    content: str | None = None
    truncated: bool = False
    visual_available: bool = False


class ClassificationThreadContext(StrictAiModel):
    thread_id: int | None = None
    thread_root_message_id: str | None = None
    latest_intent: str | None = None
    latest_handling_level: str | None = None
    ticket_id: int | None = None
    ticket_category: str | None = None
    ticket_status: str | None = None
    ticket_missing_fields: list[str] = Field(default_factory=list)
    known_serial_numbers: list[str] = Field(default_factory=list)
    rma_status: str | None = None
    has_rma: bool = False


class ClassificationRuleSignals(StrictAiModel):
    has_reply_chain: bool = False
    body_has_sn: bool = False
    matched_keywords: list[str] = Field(default_factory=list)


class ClassificationInput(StrictAiModel):
    message_id: str | None = None
    subject: str | None = None
    sender: str | None = None
    recipients: list[str] = Field(default_factory=list)
    latest_message: str = ""
    conversation_body: str = ""
    in_reply_to: str | None = None
    references: str | None = None
    latest_message_truncated: bool = False
    conversation_body_truncated: bool = False
    thread_context: ClassificationThreadContext = Field(default_factory=ClassificationThreadContext)
    rule_signals: ClassificationRuleSignals = Field(default_factory=ClassificationRuleSignals)
    attachments: list[ClassificationAttachmentMetadata] = Field(default_factory=list)


class MailPreclassificationResponse(StrictAiModel):
    intent: EmailIntent
    confidence: float = Field(ge=0, le=1)
    candidates: list[IntentCandidate] = Field(max_length=5)
    reason_code: ClassificationReasonCode
    needs_attachment_content: bool
    evidence: list[ClassificationEvidence] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_reason_for_intent(self) -> "MailPreclassificationResponse":
        expected = {
            EmailIntent.NEW_REPAIR: ClassificationReasonCode.EXPLICIT_NEW_REPAIR,
            EmailIntent.THREAD_NEW_REPAIR: ClassificationReasonCode.THREAD_NEW_REPAIR,
            EmailIntent.CUSTOMER_SUPPLEMENT: ClassificationReasonCode.CUSTOMER_SUPPLEMENT,
            EmailIntent.COMPONENT_REPLACEMENT_REPAIR: ClassificationReasonCode.COMPONENT_REPLACEMENT,
            EmailIntent.ONSITE_SERVICE: ClassificationReasonCode.ONSITE_SERVICE,
            EmailIntent.WARRANTY_STATUS_INQUIRY: ClassificationReasonCode.WARRANTY_INQUIRY,
            EmailIntent.REPAIR_THREAD_OTHER: ClassificationReasonCode.REPAIR_THREAD_OTHER,
            EmailIntent.DEVICE_INTAKE_RECEIVED: ClassificationReasonCode.DEVICE_INTAKE,
            EmailIntent.REPAIRED_DEVICE_DISPATCHED: ClassificationReasonCode.REPAIR_DISPATCHED,
            EmailIntent.CUSTOMER_REPAIRED_DEVICE_RECEIVED: ClassificationReasonCode.CUSTOMER_RECEIVED,
            EmailIntent.CONTRACT_CONFIRMATION: ClassificationReasonCode.CONTRACT,
            EmailIntent.INVOICE: ClassificationReasonCode.INVOICE,
            EmailIntent.THIRD_PARTY_EQUIPMENT_QUOTE: ClassificationReasonCode.THIRD_PARTY_QUOTE,
        }
        if self.intent == EmailIntent.UNKNOWN:
            allowed = {
                ClassificationReasonCode.INSUFFICIENT_EVIDENCE,
                ClassificationReasonCode.CONFLICTING_EVIDENCE,
                ClassificationReasonCode.ATTACHMENT_REQUIRED,
            }
            if self.reason_code not in allowed:
                raise ValueError("REASON_CODE_INTENT_MISMATCH")
        elif self.reason_code != expected[self.intent]:
            raise ValueError("REASON_CODE_INTENT_MISMATCH")
        if self.reason_code == ClassificationReasonCode.ATTACHMENT_REQUIRED and not self.needs_attachment_content:
            raise ValueError("ATTACHMENT_REQUIRED_FLAG_MISMATCH")
        return self


class AttachmentFileType(StrEnum):
    DOCX = "docx"
    XLSX = "xlsx"
    CSV = "csv"
    TXT = "txt"
    PRC = "prc"
    HTML = "html"
    IMAGE = "image"
    PDF = "pdf"


class AttachmentWarningCode(StrEnum):
    CONTENT_TRUNCATED = "CONTENT_TRUNCATED"
    OCR_LOW_QUALITY = "OCR_LOW_QUALITY"
    PARTIAL_PARSE = "PARTIAL_PARSE"
    UNSUPPORTED_STRUCTURE = "UNSUPPORTED_STRUCTURE"
    ENCRYPTED_ARCHIVE = "ENCRYPTED_ARCHIVE"
    MISSING_TEXT_LAYER = "MISSING_TEXT_LAYER"
    TABLE_STRUCTURE_LOST = "TABLE_STRUCTURE_LOST"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    UNKNOWN = "UNKNOWN"


class AttachmentWarning(StrictAiModel):
    code: AttachmentWarningCode
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1, max_length=1000)


class EvidenceLocation(StrictAiModel):
    page: int | None = Field(ge=1)
    sheet: str | None
    cell: str | None
    cell_range: str | None
    line: int | None = Field(ge=1)


class AttachmentEvidenceSource(StrictAiModel):
    attachment_id: int
    file_name: str
    source_type: Literal["raw_text", "ocr_text", "table", "document_text", "unmapped_text"]
    location: EvidenceLocation | None
    text: str = Field(min_length=1, max_length=4000)


class AttachmentModelEvidenceSource(StrictAiModel):
    source_type: Literal["raw_text", "ocr_text", "table", "document_text", "unmapped_text"]
    location: EvidenceLocation | None
    text: str = Field(min_length=1, max_length=4000)


class AttachmentBusinessField(StrEnum):
    CUSTOMER_NAME = "customer_name"
    CONTACT_PERSON = "contact_person"
    CONTACT_PHONE = "contact_phone"
    CONTACT_EMAIL = "contact_email"
    REQUEST_DATE = "request_date"
    MAILING_ADDRESS = "mailing_address"
    PROBLEM_DESCRIPTION = "problem_description"


class AttachmentItemField(StrEnum):
    SN = "sn"
    BOARD_CODE = "board_code"
    BOARD_NAME = "board_name"
    FAILURE_DESCRIPTION = "failure_description"
    LINE_NO = "line_no"
    REMARKS = "remarks"


class AttachmentFieldCandidate(StrictAiModel):
    field: AttachmentBusinessField
    value: str = Field(min_length=1, max_length=4000)
    source: AttachmentEvidenceSource


class AttachmentItemValue(StrictAiModel):
    field: AttachmentItemField
    value: str = Field(min_length=1, max_length=4000)
    source: AttachmentEvidenceSource


class AttachmentItemCandidate(StrictAiModel):
    candidate_index: int = Field(ge=0)
    values: list[AttachmentItemValue] = Field(min_length=1)


class AttachmentEvidence(StrictAiModel):
    field: AttachmentBusinessField | AttachmentItemField | None
    value: str | None
    source: AttachmentEvidenceSource


class AttachmentModelFieldCandidate(StrictAiModel):
    field: AttachmentBusinessField
    value: str = Field(min_length=1, max_length=4000)
    source: AttachmentModelEvidenceSource


class AttachmentModelItemValue(StrictAiModel):
    field: AttachmentItemField
    value: str = Field(min_length=1, max_length=4000)
    source: AttachmentModelEvidenceSource


class AttachmentModelItemCandidate(StrictAiModel):
    candidate_index: int = Field(ge=0)
    values: list[AttachmentModelItemValue] = Field(min_length=1)


class AttachmentModelEvidence(StrictAiModel):
    field: AttachmentBusinessField | AttachmentItemField | None
    value: str | None
    source: AttachmentModelEvidenceSource


class AttachmentMetadata(StrictAiModel):
    attachment_id: int
    file_name: str
    file_type: AttachmentFileType
    mime_type: str | None = None
    truncated: bool


class AttachmentParseInput(StrictAiModel):
    metadata: AttachmentMetadata
    local_summary: str = ""
    local_key_points: list[str] = Field(default_factory=list)
    content: str = ""


class AttachmentContentEvidence(StrictAiModel):
    summary: str
    key_points: list[str]
    candidate_fields: list[AttachmentModelFieldCandidate]
    candidate_items: list[AttachmentModelItemCandidate]
    evidence: list[AttachmentModelEvidence]
    warnings: list[AttachmentWarning]
    ocr_text: str | None

    @model_validator(mode="after")
    def validate_candidate_indexes(self) -> "AttachmentContentEvidence":
        indexes = [item.candidate_index for item in self.candidate_items]
        if len(indexes) != len(set(indexes)):
            raise ValueError("DUPLICATE_ATTACHMENT_CANDIDATE_INDEX")
        return self


class AttachmentParseResult(StrictAiModel):
    schema_version: Literal[ATTACHMENT_SCHEMA_VERSION] = ATTACHMENT_SCHEMA_VERSION
    parser_version: str
    metadata: AttachmentMetadata
    summary: str
    key_points: list[str]
    candidate_fields: list[AttachmentFieldCandidate]
    candidate_items: list[AttachmentItemCandidate]
    evidence: list[AttachmentEvidence]
    warnings: list[AttachmentWarning]
    raw_text: str | None
    ocr_text: str | None
    normalized_text: str | None


class RepairFields(StrictAiModel):
    customer_name: str | None = Field(min_length=1, max_length=255)
    contact_person: str | None = Field(min_length=1, max_length=100)
    contact_phone: str | None = Field(min_length=1, max_length=100)
    contact_email: str | None = Field(min_length=1, max_length=255)
    request_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    mailing_address: str | None = Field(min_length=1, max_length=500)
    problem_description: str | None = Field(min_length=1, max_length=10000)

    @field_validator("request_date")
    @classmethod
    def validate_request_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from datetime import date

        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("REQUEST_DATE_MUST_BE_ISO_DATE") from exc
        return value


class RepairItem(StrictAiModel):
    sn: str | None = Field(min_length=1, max_length=100)
    board_code: str | None = Field(min_length=1, max_length=100)
    board_name: str | None = Field(min_length=1, max_length=255)
    failure_description: str | None = Field(min_length=1, max_length=10000)
    line_no: int | None = Field(ge=1)
    remarks: str | None = Field(min_length=1, max_length=4000)


class ConflictValue(StrictAiModel):
    value: str = Field(min_length=1, max_length=4000)
    source_type: Literal["latest_message", "thread_context", "existing_ticket", "attachment"]
    attachment_id: int | None
    file_name: str | None

    @model_validator(mode="after")
    def validate_attachment_provenance(self) -> "ConflictValue":
        if self.source_type == "attachment" and self.attachment_id is None and not self.file_name:
            raise ValueError("ATTACHMENT_CONFLICT_PROVENANCE_REQUIRED")
        return self


class ConflictField(StrictAiModel):
    field_path: str
    values: list[ConflictValue] = Field(min_length=2)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_field_path(self) -> "ConflictField":
        if not _valid_repair_field_path(self.field_path):
            raise ValueError("INVALID_REPAIR_FIELD_PATH")
        return self


_REPAIR_FIELD_PATH = re.compile(
    r"^(?:fields\.(?:customer_name|contact_person|contact_phone|contact_email|request_date|mailing_address|problem_description)"
    r"|items\[\d+\]\.(?:sn|board_code|board_name|failure_description|line_no|remarks))$"
)


def _valid_repair_field_path(value: str) -> bool:
    return bool(_REPAIR_FIELD_PATH.fullmatch(value))


class FieldConfidence(StrictAiModel):
    path: str
    score: float = Field(ge=0, le=1)
    reasons: list[str]

    @model_validator(mode="after")
    def validate_path(self) -> "FieldConfidence":
        if not _valid_repair_field_path(self.path):
            raise ValueError("INVALID_FIELD_CONFIDENCE_PATH")
        return self


class ManualReviewReasonCode(StrEnum):
    FIELD_CONFLICT = "FIELD_CONFLICT"
    AMBIGUOUS_EVIDENCE = "AMBIGUOUS_EVIDENCE"
    LOW_SEMANTIC_CONFIDENCE = "LOW_SEMANTIC_CONFIDENCE"
    ATTACHMENT_TRUNCATED = "ATTACHMENT_TRUNCATED"
    OCR_LOW_QUALITY = "OCR_LOW_QUALITY"
    INCOMPLETE_CONTEXT = "INCOMPLETE_CONTEXT"


class ManualReviewSuggestion(StrictAiModel):
    required: bool
    reason_codes: list[ManualReviewReasonCode]
    instruction: str | None

    @model_validator(mode="after")
    def validate_required_consistency(self) -> "ManualReviewSuggestion":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("DUPLICATE_MANUAL_REVIEW_REASON")
        if self.required and not self.reason_codes:
            raise ValueError("MANUAL_REVIEW_REASON_REQUIRED")
        if self.required and not str(self.instruction or "").strip():
            raise ValueError("MANUAL_REVIEW_INSTRUCTION_REQUIRED")
        if not self.required and self.reason_codes:
            raise ValueError("MANUAL_REVIEW_REASON_WITHOUT_REVIEW")
        if not self.required and str(self.instruction or "").strip():
            raise ValueError("MANUAL_REVIEW_INSTRUCTION_WITHOUT_REVIEW")
        return self


class ExistingTicketContext(StrictAiModel):
    ticket_id: int | None = None
    ticket_category: str | None = None
    ticket_status: str | None = None
    missing_field_names: list[str] = Field(default_factory=list)
    has_rma: bool = False


class RepairThreadContext(StrictAiModel):
    thread_id: int | None = None
    has_reply_headers: bool = False


class RepairExtractionInput(StrictAiModel):
    locked_intent: EmailIntent
    latest_message: str
    thread_context: RepairThreadContext
    existing_ticket_context: ExistingTicketContext
    attachment_results: list[AttachmentParseResult]


class RepairExtractionResult(StrictAiModel):
    fields: RepairFields
    items: list[RepairItem]
    conflicts: list[ConflictField]
    confidence_score: float = Field(ge=0, le=1)
    field_confidences: list[FieldConfidence]
    manual_review_suggestion: ManualReviewSuggestion

    @model_validator(mode="after")
    def validate_referenced_item_paths(self) -> "RepairExtractionResult":
        paths = [item.path for item in self.field_confidences]
        if len(paths) != len(set(paths)):
            raise ValueError("DUPLICATE_FIELD_CONFIDENCE_PATH")
        referenced_paths = [
            *paths,
            *(item.field_path for item in self.conflicts),
        ]
        for path in referenced_paths:
            match = re.match(r"^items\[(\d+)\]\.", path)
            if match and int(match.group(1)) >= len(self.items):
                raise ValueError("ITEM_PATH_OUT_OF_RANGE")
        return self
