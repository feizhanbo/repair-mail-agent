from __future__ import annotations

import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from app.config import settings
from app.models import Email

SN_PATTERN = re.compile(r"(?:SN|S/N|序列号|设备编号)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,})", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_PATTERN = re.compile(r"(?:电话|手机|联系方式|联系电话)\s*[:：]?\s*([0-9+\-()\s]{6,})")


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return unescape(BeautifulSoup(html, "lxml").get_text("\n"))


def clean_email_body(email: Email) -> str:
    text = email.clean_body or email.latest_reply_segment or email.text_body or html_to_text(email.html_body) or ""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def classify_email(email: Email, body: str) -> tuple[str, float, str]:
    subject = email.subject or ""
    text = f"{subject}\n{body}".lower()
    if email.in_reply_to:
        return "customer_reply", 0.85, "存在 In-Reply-To，按客户补充处理。"
    if any(keyword in text for keyword in ("退订", "unsubscribe", "广告", "newsletter")):
        return "irrelevant", 0.9, "命中无关邮件关键词。"
    if any(keyword in text for keyword in ("fwd:", "fw:", "转发")):
        return "internal_forward", 0.65, "主题疑似内部转发。"
    if any(keyword in text for keyword in ("报修", "维修", "故障", "repair", "rma", "sn")):
        return "new_repair", 0.8, "命中报修关键词。"
    return "unknown", 0.45, "未命中明确分类规则。"


def extract_fields(email: Email) -> dict[str, Any]:
    body = clean_email_body(email)
    subject = email.subject or ""
    combined = f"{subject}\n{body}"
    sns = list(dict.fromkeys(match.group(1).strip().upper() for match in SN_PATTERN.finditer(combined)))
    contact_emails = list(dict.fromkeys(EMAIL_PATTERN.findall(combined)))
    phone_match = PHONE_PATTERN.search(combined)
    problem_lines = [
        line.strip()
        for line in body.splitlines()
        if any(keyword in line for keyword in ("故障", "问题", "现象", "不能", "无法", "异常", "损坏"))
    ]
    problem_description = "\n".join(problem_lines[:3]) or (body[:500] if body else None)
    fields: dict[str, Any] = {
        "contact_email": contact_emails[0] if contact_emails else None,
        "contact_phone": phone_match.group(1).strip() if phone_match else None,
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
