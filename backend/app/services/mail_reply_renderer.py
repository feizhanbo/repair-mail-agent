from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email
from app.services.storage import (
    StorageConfigurationError,
    StorageUploadError,
    download_oss_object_bytes,
)


BLOCKED_TAGS = {
    "base",
    "button",
    "embed",
    "form",
    "iframe",
    "input",
    "link",
    "meta",
    "object",
    "script",
}
CID_URL_ATTRIBUTES = {"background", "href", "src"}
UNSAFE_CSS_RE = re.compile(
    r"(?:expression\s*\(|behavior\s*:|-moz-binding\s*:|@import|url\s*\(\s*['\"]?\s*(?:javascript|vbscript)\s*:)",
    re.IGNORECASE,
)
CID_RE = re.compile(r"cid\s*:\s*([^\s\"'<>\)]+)", re.IGNORECASE)


class ReplyRenderError(ValueError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class RelatedResource:
    content: bytes
    maintype: str
    subtype: str
    content_id: str
    original_content_id: str
    content_hash: str


@dataclass(frozen=True)
class ReplyHistory:
    plain: str
    html: str
    snapshot_hash: str
    resources: tuple[RelatedResource, ...]
    raw_eml_sha256: str


def _part_bytes(part: Message) -> bytes:
    value = part.get_payload(decode=True)
    if isinstance(value, bytes):
        return value
    try:
        content = part.get_content() if isinstance(part, EmailMessage) else None
    except Exception:
        content = None
    if isinstance(content, str):
        return content.encode(part.get_content_charset() or "utf-8", errors="replace")
    return b""


def _part_text(part: Message) -> str:
    try:
        content = part.get_content() if isinstance(part, EmailMessage) else None
    except Exception:
        content = None
    if isinstance(content, str):
        return content
    return _part_bytes(part).decode(part.get_content_charset() or "utf-8", errors="replace")


def _normalize_cid(value: str) -> str:
    return unquote(value).strip().strip("<>").strip().lower()


def _safe_css(value: str) -> str:
    return "" if UNSAFE_CSS_RE.search(value or "") else value


def _sanitize_and_collect_cids(source: str) -> tuple[str, set[str]]:
    soup = BeautifulSoup(source or "", "lxml")
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    for tag in soup.find_all(BLOCKED_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            lowered = attribute.lower()
            value = tag.attrs.get(attribute)
            rendered = " ".join(value) if isinstance(value, list) else str(value or "")
            if lowered.startswith("on") or lowered in {"formaction", "poster", "srcset"}:
                del tag.attrs[attribute]
                continue
            if lowered == "style":
                safe = _safe_css(rendered)
                if safe:
                    tag.attrs[attribute] = safe
                else:
                    del tag.attrs[attribute]
                continue
            if lowered in CID_URL_ATTRIBUTES:
                normalized = rendered.strip().lower()
                if normalized.startswith(("javascript:", "vbscript:")):
                    del tag.attrs[attribute]
        if tag.name == "style":
            safe = _safe_css(tag.get_text())
            if safe:
                tag.string = safe
            else:
                tag.decompose()

    cids: set[str] = set()
    for tag in soup.find_all(True):
        for attribute in CID_URL_ATTRIBUTES:
            value = tag.attrs.get(attribute)
            rendered = " ".join(value) if isinstance(value, list) else str(value or "")
            cids.update(_normalize_cid(item) for item in CID_RE.findall(rendered))
        style = str(tag.attrs.get("style") or "")
        cids.update(_normalize_cid(item) for item in CID_RE.findall(style))
        if tag.name == "style":
            cids.update(_normalize_cid(item) for item in CID_RE.findall(tag.get_text()))
    style_parts: list[str] = []
    for tag in soup.find_all("style"):
        style_parts.append(str(tag))
        tag.extract()
    body = soup.body
    body_html = body.decode_contents() if body is not None else str(soup)
    return "".join(style_parts) + body_html, {item for item in cids if item}


def _replace_cids(source: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        original = match.group(1)
        replacement = mapping.get(_normalize_cid(original))
        return f"cid:{replacement}" if replacement else match.group(0)

    return CID_RE.sub(replace, source)


def _content_id_index(message: Message) -> dict[str, list[Message]]:
    result: dict[str, list[Message]] = {}
    for part in message.walk() if message.is_multipart() else [message]:
        if part.is_multipart():
            continue
        content_id = _normalize_cid(str(part.get("Content-ID") or ""))
        if content_id:
            result.setdefault(content_id, []).append(part)
    return result


def _quote_header(message: Message, *, language: str) -> tuple[str, str]:
    labels = (
        ("发件人", "发送时间", "收件人", "抄送", "主题")
        if language == "zh-CN"
        else ("From", "Sent", "To", "Cc", "Subject")
    )
    values = (
        str(message.get("From") or ""),
        str(message.get("Date") or ""),
        str(message.get("To") or ""),
        str(message.get("Cc") or ""),
        str(message.get("Subject") or ""),
    )
    plain_lines = [f"{label}: {value}" for label, value in zip(labels, values) if value]
    html_lines = [
        f"<div><strong>{html.escape(label)}:</strong> {html.escape(value)}</div>"
        for label, value in zip(labels, values)
        if value
    ]
    return "\n".join(plain_lines), "".join(html_lines)


def render_reply_history_from_eml(
    raw_eml: bytes,
    *,
    parent_email_id: int,
    language: str,
) -> ReplyHistory:
    if not raw_eml:
        raise ReplyRenderError("REPLY_PARENT_RAW_EML_EMPTY")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_eml)
    except Exception as exc:
        raise ReplyRenderError("REPLY_PARENT_EML_INVALID") from exc

    html_part = message.get_body(preferencelist=("html",)) if isinstance(message, EmailMessage) else None
    plain_part = message.get_body(preferencelist=("plain",)) if isinstance(message, EmailMessage) else None
    source_html = _part_text(html_part) if html_part is not None else ""
    source_plain = _part_text(plain_part) if plain_part is not None else ""
    if not source_html and not source_plain:
        raise ReplyRenderError("REPLY_PARENT_BODY_MISSING")
    if not source_html:
        source_html = (
            '<div style="font-family:Arial,Helvetica,sans-serif;white-space:normal">'
            + html.escape(source_plain).replace("\n", "<br>\n")
            + "</div>"
        )

    sanitized_html, referenced_cids = _sanitize_and_collect_cids(source_html)
    cid_index = _content_id_index(message)
    raw_hash = hashlib.sha256(raw_eml).hexdigest()
    mapping: dict[str, str] = {}
    resources: list[RelatedResource] = []
    for index, original_cid in enumerate(sorted(referenced_cids), start=1):
        matches = cid_index.get(original_cid, [])
        if not matches:
            raise ReplyRenderError("REPLY_PARENT_CID_MISSING")
        if len(matches) != 1:
            raise ReplyRenderError("REPLY_PARENT_CID_CONFLICT")
        part = matches[0]
        content = _part_bytes(part)
        content_hash = hashlib.sha256(content).hexdigest()
        new_cid = f"history-{parent_email_id}-{raw_hash[:12]}-{index}@rma.accotest.com"
        mapping[original_cid] = new_cid
        maintype, subtype = (part.get_content_type().split("/", 1) + ["octet-stream"])[:2]
        resources.append(
            RelatedResource(
                content=content,
                maintype=maintype,
                subtype=subtype,
                content_id=new_cid,
                original_content_id=original_cid,
                content_hash=content_hash,
            )
        )
    quoted_html = _replace_cids(sanitized_html, mapping)
    if source_plain:
        quoted_plain = source_plain.strip()
    else:
        quoted_plain = BeautifulSoup(quoted_html, "lxml").get_text("\n", strip=True)
    header_plain, header_html = _quote_header(message, language=language)
    plain = f"{header_plain}\n\n{quoted_plain}".strip()
    html_body = (
        '<div class="rma-reply-history-header" style="font-family:Arial,Helvetica,sans-serif;'
        'font-size:12px;color:#555;margin-top:16px">'
        + header_html
        + "</div>"
        + '<blockquote class="rma-reply-history" style="margin:8px 0 0 0;padding-left:12px;'
        'border-left:2px solid #c8c8c8">'
        + quoted_html
        + "</blockquote>"
    )
    evidence: dict[str, Any] = {
        "parent_email_id": parent_email_id,
        "parent_message_id": str(message.get("Message-ID") or ""),
        "raw_eml_sha256": raw_hash,
        "plain_sha256": hashlib.sha256(plain.encode("utf-8")).hexdigest(),
        "html_sha256": hashlib.sha256(html_body.encode("utf-8")).hexdigest(),
        "resources": [
            {
                "content_id": resource.content_id,
                "content_type": f"{resource.maintype}/{resource.subtype}",
                "content_hash": resource.content_hash,
            }
            for resource in resources
        ],
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReplyHistory(
        plain=plain,
        html=html_body,
        snapshot_hash=snapshot_hash,
        resources=tuple(resources),
        raw_eml_sha256=raw_hash,
    )


async def render_reply_history(
    session: AsyncSession,
    *,
    parent: Email,
    language: str,
) -> ReplyHistory:
    if not parent.raw_eml_oss_object_id:
        raise ReplyRenderError("REPLY_PARENT_RAW_EML_REQUIRED")
    try:
        raw_eml = await download_oss_object_bytes(
            session,
            oss_object_id=parent.raw_eml_oss_object_id,
        )
    except StorageConfigurationError as exc:
        raise ReplyRenderError("REPLY_PARENT_OSS_NOT_CONFIGURED") from exc
    except (StorageUploadError, ValueError) as exc:
        raise ReplyRenderError("REPLY_PARENT_RAW_EML_UNAVAILABLE", retryable=True) from exc
    return render_reply_history_from_eml(
        raw_eml,
        parent_email_id=parent.id,
        language=language,
    )
