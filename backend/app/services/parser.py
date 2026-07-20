from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from app.config import settings
from app.models import Email
from app.services.business_rules import required_missing_for_values


@dataclass(frozen=True)
class RuleAnalysisResult:
    intent_type: str
    classification_confidence: float
    classification_reason: str
    body: str
    fields: dict[str, Any]
    items: list[dict[str, Any]]
    missing_fields: dict[str, Any]
    conflict_fields: dict[str, Any]
    confidence_score: float
    field_confidences: dict[str, float]
    evidence: dict[str, Any]

    def to_parse_payload(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type,
            "extracted_fields": self.fields,
            "extracted_items": {"items": self.items},
            "missing_fields": self.missing_fields,
            "conflict_fields": self.conflict_fields,
            "confidence_score": self.confidence_score,
            "field_confidences": self.field_confidences,
            "evidence": {
                **self.evidence,
                "classification": {
                    "intent_type": self.intent_type,
                    "confidence": self.classification_confidence,
                    "reason": self.classification_reason,
                },
                "mode": "pre_archive_rule_analysis",
                "candidate_only": True,
            },
        }

    def summary(self) -> dict[str, Any]:
        return {
            "intent_type": self.intent_type,
            "classification_confidence": self.classification_confidence,
            "confidence_score": self.confidence_score,
            "field_keys": sorted(self.fields),
            "item_count": len(self.items),
            "missing_field_keys": sorted(self.missing_fields),
            "conflict_field_keys": sorted(self.conflict_fields),
        }

SN_PATTERN = re.compile(r"(?:SN(?:号|號)?|S/N|序列号|设备编号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,})", re.IGNORECASE)
LONG_SN_PATTERN = re.compile(r"\b[A-Z0-9]{12,}\b", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HISTORY_MARKER = re.compile(
    r"^(?:-{2,}\s*Original Message\s*-{2,}|原始邮件|发件人[：:]|From\s*:|On .+ wrote:)",
    re.IGNORECASE,
)
SIGNATURE_MARKER = re.compile(
    r"^(?:Best Regards!?|Thanks\s*&\s*Best Regards|Sent from my|此致|祝好)[\s!！。]*$",
    re.IGNORECASE,
)


def _problem_text_from_line(line: str) -> str:
    """Remove transport labels from a fault-evidence line without losing its text."""
    value = line.strip()
    value = SN_PATTERN.sub("", value, count=1).strip(" -—:：;,，")
    value = re.sub(
        r"^(?:故障(?:描述|现象)?|问题(?:描述)?|failure|fault)\s*[:：-]?\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return value or line.strip()


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return unescape(BeautifulSoup(html, "lxml").get_text("\n"))


def normalize_email_body(value: str | None) -> str:
    return re.sub(r"\n{3,}", "\n\n", (value or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def extract_latest_reply_segment(value: str | None) -> str:
    lines = normalize_email_body(value).splitlines()
    selected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if selected and (HISTORY_MARKER.match(stripped) or SIGNATURE_MARKER.match(stripped)):
            break
        selected.append(line.rstrip())
    return "\n".join(selected).strip()


def clean_email_body(email: Email) -> str:
    text = email.latest_reply_segment or email.clean_body or email.text_body or html_to_text(email.html_body) or ""
    return normalize_email_body(text)


def classify_email(email: Email, body: str) -> tuple[str, float, str]:
    subject = email.subject or ""
    text = f"{subject}\n{body}".lower()
    receipt_terms = ("我们收到货了", "请入库", "已收到设备", "设备已收到", "received the unit", "received the device")
    if any(keyword in text for keyword in receipt_terms):
        return "device_received", 0.95, "最新回复确认维修设备已经收到。"
    if email.mail_direction == "outbound" and "rma" in text:
        return "rma_sent", 0.9, "系统外发的 RMA 邮件。"
    if email.in_reply_to or email.references_header:
        supplement_terms = ("补充", "sn", "s/n", "序列号", "故障", "地址", "电话", "fault", "serial", "address")
        if any(term in text for term in supplement_terms):
            return "customer_supplement", 0.88, "回复链包含新增报修信息。"
        return "normal_reply", 0.85, "存在 In-Reply-To 或 References，按普通回复处理。"
    if any(keyword in text for keyword in ("退订", "unsubscribe", "广告", "newsletter")):
        return "irrelevant", 0.9, "命中无关邮件关键词。"
    if any(keyword in text for keyword in ("fwd:", "fw:", "转发")):
        return "normal_reply", 0.65, "主题疑似转发，按不触发 RMA 的普通链路邮件处理。"
    if any(keyword in text for keyword in ("报修", "维修", "故障", "repair", "rma", "sn")):
        return "new_repair", 0.8, "命中报修关键词。"
    return "unknown", 0.45, "未命中明确分类规则。"


def extract_fields(email: Email) -> dict[str, Any]:
    body = clean_email_body(email)
    conversation = normalize_email_body(email.clean_body or email.text_body or body)
    subject = email.subject or ""
    combined = f"{subject}\n{body}\n{conversation}"
    labeled_sns = [match.group(1).strip().upper() for match in SN_PATTERN.finditer(combined)]
    long_sns = [
        match.group(0).strip().upper()
        for match in LONG_SN_PATTERN.finditer(combined)
        if not match.group(0).upper().startswith(("RMA", "HTTP", "MESSAGE"))
        and sum(char.isdigit() for char in match.group(0)) >= 6
        and any(char.isalpha() for char in match.group(0))
    ]
    sns = list(dict.fromkeys([*labeled_sns, *long_sns]))
    contact_emails = list(dict.fromkeys(EMAIL_PATTERN.findall(combined)))
    problem_lines = [
        _problem_text_from_line(line)
        for line in combined.splitlines()
        if any(keyword in line.lower() for keyword in ("故障", "问题", "现象", "不能", "无法", "异常", "损坏", "failure", "fault"))
    ]
    problem_description = "\n".join(problem_lines[:3]) or (body[:500] if body else None)
    fields: dict[str, Any] = {
        "contact_email": contact_emails[0] if contact_emails else None,
        "problem_description": problem_description,
    }
    items = [{"line_no": index + 1, "sn": sn, "failure_description": problem_description} for index, sn in enumerate(sns)]
    missing_fields: dict[str, str] = {}
    if not sns:
        missing_fields["sn"] = "缺少设备 SN。"
    if not problem_description:
        missing_fields["problem_description"] = "缺少故障描述。"
    if not fields["contact_email"] and not email.from_address:
        missing_fields["contact_email"] = "缺少联系人邮箱。"
    confidence = 0.9
    if missing_fields:
        confidence = 0.58 if items else 0.42
    if confidence < settings.CONFIDENCE_THRESHOLD:
        fields["confidence_warning"] = "规则解析置信度低于阈值，建议人工复核。"
    return {
        "body": body,
        "fields": {key: value for key, value in fields.items() if value is not None},
        "items": items,
        "missing_fields": missing_fields,
        "conflict_fields": {},
        "confidence_score": confidence,
        "field_confidences": {
            "sn": 0.85 if sns else 0.0,
            "problem_description": 0.75 if problem_description else 0.0,
            "contact_email": 0.8 if fields.get("contact_email") else 0.3,
        },
        "evidence": {
            "source_type": "rule",
            "email_id": email.id,
            "subject": email.subject,
            "sn_matches": sns,
            "snippet": body[:500],
            "ai_reserved": True,
        },
    }


def analyze_email_rules(email: Email) -> RuleAnalysisResult:
    body = clean_email_body(email)
    intent_type, classification_confidence, classification_reason = classify_email(email, body)
    extracted = extract_fields(email)
    extracted["missing_fields"] = required_missing_for_values(
        intent_type=intent_type,
        fields=extracted["fields"],
        items=extracted["items"],
        reported_missing=extracted["missing_fields"],
    )
    return RuleAnalysisResult(
        intent_type=intent_type,
        classification_confidence=classification_confidence,
        classification_reason=classification_reason,
        body=extracted["body"],
        fields=extracted["fields"],
        items=extracted["items"],
        missing_fields=extracted["missing_fields"],
        conflict_fields=extracted["conflict_fields"],
        confidence_score=min(float(extracted["confidence_score"]), classification_confidence),
        field_confidences=extracted["field_confidences"],
        evidence=extracted["evidence"],
    )
