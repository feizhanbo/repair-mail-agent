from __future__ import annotations

import re
from email.utils import parseaddr
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from app.config import settings
from app.models import Email

SN_PATTERN = re.compile(r"(?:维修板卡\s*)?(?:SN|S/N|序列号|设备编号)\s*(?:[:：#]|为|是)?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,})", re.IGNORECASE)
LONG_SN_PATTERN = re.compile(r"\b[A-Z][0-9]{8,}[A-Z0-9_-]*\b", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w.-])")
PHONE_PATTERN = re.compile(r"(?:电话|手机|联系方式|联系电话)\s*[:：]?\s*([0-9+\-()\s]{6,})")
SN_HEADER_TOKENS = {"NAME", "MODEL", "TYPE", "SN", "S/N", "SERIAL", "NUMBER", "NO", "NO."}
PROBLEM_LABELS = ("故障现象", "故障描述", "问题描述", "问题现象", "failure", "problem")
PROBLEM_STOP_LABELS = ("联系人", "联系电话", "联系方式", "邮箱", "维修板卡", "附件", "收件人", "发件人")
PROBLEM_TABLE_NOISE = {"NO", "NO.", "DESCRIPTION", "S/N", "SN", "NAME"}


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return unescape(BeautifulSoup(html, "lxml").get_text("\n"))


def clean_email_body(email: Email) -> str:
    text = email.clean_body or email.latest_reply_segment or email.text_body or html_to_text(email.html_body) or ""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _normalize_sn(candidate: str) -> str | None:
    sn = candidate.strip().strip("：:;；,.，。()（）[]【】").upper()
    if len(sn) < 5 or sn in SN_HEADER_TOKENS:
        return None
    if not any(char.isdigit() for char in sn):
        return None
    return sn


def _extract_sns(text: str) -> list[str]:
    matches: list[str] = []
    for match in SN_PATTERN.finditer(text):
        sn = _normalize_sn(match.group(1))
        if sn:
            matches.append(sn)
    for match in LONG_SN_PATTERN.finditer(text):
        sn = _normalize_sn(match.group(0))
        if sn:
            matches.append(sn)
    return list(dict.fromkeys(matches))


def _valid_contact_email(candidate: str | None) -> str | None:
    if not candidate:
        return None
    _, address = parseaddr(candidate)
    if not address or not EMAIL_PATTERN.fullmatch(address):
        return None
    local = address.split("@", 1)[0].lower()
    if local.startswith(("image", "cid", "logo")):
        return None
    return address


def _extract_contact_email(text: str, fallback_from_address: str | None) -> str | None:
    for candidate in EMAIL_PATTERN.findall(text):
        address = _valid_contact_email(candidate)
        if address:
            return address
    return _valid_contact_email(fallback_from_address)


def _split_label_value(line: str) -> str:
    if "：" in line:
        return line.split("：", 1)[1].strip()
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return line.strip()


def _is_problem_noise(line: str) -> bool:
    normalized = line.strip().strip("：:;；,.，。").upper()
    if not normalized:
        return True
    if normalized in PROBLEM_TABLE_NOISE or normalized.isdigit():
        return True
    if line.lower().startswith("[cid:"):
        return True
    return _normalize_sn(line) is not None


def _extract_problem_description(body: str) -> str | None:
    lines = [line.strip() for line in body.splitlines()]
    candidates: list[str] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        lower_line = line.lower()
        if not any(label in lower_line for label in PROBLEM_LABELS):
            continue
        first_value = _split_label_value(line)
        if first_value and not any(first_value == label for label in PROBLEM_LABELS) and not _is_problem_noise(first_value):
            candidates.append(first_value)
        for next_line in lines[index + 1 : index + 16]:
            if not next_line:
                continue
            lower_next = next_line.lower()
            if any(label in lower_next for label in PROBLEM_STOP_LABELS):
                break
            if _is_problem_noise(next_line):
                continue
            candidates.append(next_line)
            if len(candidates) >= 3:
                break
        if candidates:
            break
    if not candidates:
        candidates = [
            line
            for line in lines
            if any(keyword in line for keyword in ("故障", "问题", "现象", "不能", "无法", "异常", "损坏"))
            and _split_label_value(line)
            and not _is_problem_noise(_split_label_value(line))
        ][:3]
    problem_description = "\n".join(dict.fromkeys(candidate for candidate in candidates if candidate))
    return problem_description or (body[:500] if body else None)


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
    sns = _extract_sns(combined)
    contact_email = _extract_contact_email(combined, getattr(email, "from_address", None))
    phone_match = PHONE_PATTERN.search(combined)
    problem_description = _extract_problem_description(body)
    fields: dict[str, Any] = {
        "contact_email": contact_email,
        "contact_phone": phone_match.group(1).strip() if phone_match else None,
        "problem_description": problem_description,
    }
    items = [{"line_no": index + 1, "sn": sn, "failure_description": problem_description} for index, sn in enumerate(sns)]
    missing_fields: dict[str, str] = {}
    if not sns:
        missing_fields["sn"] = "缺少设备 SN。"
    if not problem_description:
        missing_fields["problem_description"] = "缺少故障描述。"
    if not fields["contact_email"] and not getattr(email, "from_address", None):
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
