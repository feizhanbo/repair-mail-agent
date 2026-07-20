from __future__ import annotations

import asyncio
import base64
from typing import Any

from bs4 import BeautifulSoup
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email, EmailAttachment
from app.config import settings
from app.core.request_context import get_correlation_id
from app.services.attachment_parser import attachment_type, render_pdf_pages
from app.services.storage import (
    StorageConfigurationError,
    StorageUploadError,
    download_oss_object_bytes,
    generate_presigned_url_for_object,
)

BLOCKED_TAGS = {"script", "style", "iframe", "object", "embed", "form", "input", "button", "meta", "link", "base"}


def _preview_http_error(code: str, *, http_status: int, stage: str, retryable: bool) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={
            "code": code,
            "data": {
                "stage": stage,
                "retryable": retryable,
                "correlation_id": get_correlation_id(),
            },
        },
    )


def _map_storage_error(exc: Exception, *, stage: str) -> HTTPException:
    if isinstance(exc, StorageConfigurationError):
        return _preview_http_error("OSS_NOT_CONFIGURED", http_status=503, stage=stage, retryable=False)
    if isinstance(exc, ValueError):
        return _preview_http_error("OSS_OBJECT_NOT_FOUND", http_status=404, stage=stage, retryable=False)
    code = str(exc)
    cause_text = f"{exc.__cause__.__class__.__name__}:{exc.__cause__}" if exc.__cause__ else ""
    combined = f"{code} {cause_text}".lower()
    if code == "OSS_OBJECT_NOT_READY":
        return _preview_http_error("OSS_OBJECT_NOT_READY", http_status=409, stage=stage, retryable=True)
    if any(marker in combined for marker in ("accessdenied", "forbidden", "permission", "status: 403")):
        return _preview_http_error("OSS_ACCESS_DENIED", http_status=403, stage=stage, retryable=False)
    if any(marker in combined for marker in ("nosuchkey", "notfound", "status: 404")):
        return _preview_http_error("OSS_OBJECT_NOT_FOUND", http_status=404, stage=stage, retryable=False)
    if any(marker in combined for marker in ("timeout", "connection", "network", "dns", "temporarily unavailable")):
        return _preview_http_error("OSS_NETWORK_UNREACHABLE", http_status=502, stage=stage, retryable=True)
    return _preview_http_error("OSS_DOWNLOAD_FAILED", http_status=502, stage=stage, retryable=True)


def _missing_archive_error(attachment: EmailAttachment, *, stage: str) -> HTTPException:
    pending = (attachment.parse_status or "").lower() in {"pending", "queued", "processing", "uploading"}
    return _preview_http_error(
        "OSS_OBJECT_NOT_READY" if pending else "OSS_OBJECT_NOT_FOUND",
        http_status=409 if pending else 404,
        stage=stage,
        retryable=pending,
    )


async def _presigned_url(session: AsyncSession, object_id: int, *, stage: str) -> str:
    try:
        return await generate_presigned_url_for_object(session, oss_object_id=object_id, expires_seconds=900)
    except Exception as exc:
        raise _map_storage_error(exc, stage=stage) from exc


async def _download_bytes(session: AsyncSession, object_id: int, *, stage: str) -> bytes:
    try:
        return await download_oss_object_bytes(session, oss_object_id=object_id)
    except Exception as exc:
        raise _map_storage_error(exc, stage=stage) from exc


def sanitize_email_html(value: str, *, cid_urls: dict[str, str] | None = None) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for tag in soup.find_all(BLOCKED_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            lowered = attribute.lower()
            if lowered.startswith("on") or lowered in {"srcset", "formaction", "poster"}:
                del tag.attrs[attribute]
        if tag.name == "img":
            source = str(tag.get("src") or "")
            if source.lower().startswith("cid:"):
                cid = source[4:].strip("<> ").lower()
                replacement = (cid_urls or {}).get(cid)
                if replacement:
                    tag["src"] = replacement
                else:
                    tag.decompose()
            elif source.startswith("data:") or source.startswith("http:") or source.startswith("https:") or source.startswith("//"):
                tag.decompose()
        if tag.name == "a":
            href = str(tag.get("href") or "")
            if href and not href.lower().startswith(("https://", "http://", "mailto:")):
                del tag.attrs["href"]
            tag["rel"] = "noopener noreferrer"
            tag["target"] = "_blank"
    return str(soup.body or soup)


async def build_email_preview(session: AsyncSession, email_id: int) -> dict[str, Any]:
    email = await session.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    attachments = (
        await session.execute(
            select(EmailAttachment).where(
                EmailAttachment.email_id == email.id,
                EmailAttachment.is_inline == True,  # noqa: E712
            )
        )
    ).scalars().all()
    cid_urls: dict[str, str] = {}
    warnings: list[dict[str, Any]] = []
    for attachment in attachments:
        if not attachment.content_id or not attachment.oss_object_id or attachment.parse_status == "skipped_decorative":
            continue
        try:
            cid_urls[attachment.content_id.strip("<> ").lower()] = await _presigned_url(
                session, attachment.oss_object_id, stage="email_cid_url"
            )
        except HTTPException as exc:
            warnings.append(
                {
                    "code": exc.detail.get("code") if isinstance(exc.detail, dict) else "CID_PREVIEW_FAILED",
                    "attachment_id": attachment.id,
                    "message": "内嵌图片加载失败，邮件正文已降级显示。",
                }
            )
    if email.html_body:
        return {"email_id": email.id, "mode": "html", "html": sanitize_email_html(email.html_body, cid_urls=cid_urls), "text": None, "warnings": warnings}
    return {"email_id": email.id, "mode": "text", "html": None, "text": email.text_body or email.clean_body or "", "warnings": warnings}


async def build_attachment_preview(session: AsyncSession, attachment_id: int) -> dict[str, Any]:
    attachment = await session.get(EmailAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ATTACHMENT_NOT_FOUND")
    file_type = attachment_type(attachment)
    base = {
        "attachment_id": attachment.id,
        "file_name": attachment.file_name,
        "file_type": file_type,
        "parse_status": attachment.parse_status,
    }
    if file_type == "image":
        if not attachment.oss_object_id:
            raise _missing_archive_error(attachment, stage="attachment_image_lookup")
        return {
            **base,
            "mode": file_type,
            "url": await _presigned_url(session, attachment.oss_object_id, stage="attachment_image_url"),
            "text": None,
            "html": None,
            "extracted_json": attachment.extracted_json,
        }
    if file_type == "pdf":
        if not attachment.oss_object_id:
            raise _missing_archive_error(attachment, stage="attachment_pdf_lookup")
        content = await _download_bytes(session, attachment.oss_object_id, stage="attachment_pdf_download")
        try:
            pages, page_count = await asyncio.to_thread(
                render_pdf_pages,
                content,
                max_pages=max(1, settings.PDF_PREVIEW_MAX_PAGES),
            )
        except (ValueError, RuntimeError) as exc:
            raise _preview_http_error(
                "ATTACHMENT_PDF_INVALID",
                http_status=422,
                stage="attachment_pdf_render",
                retryable=False,
            ) from exc
        return {
            **base,
            "mode": "pdf_pages",
            "url": None,
            "text": None,
            "html": None,
            "pages": [f"data:image/png;base64,{base64.b64encode(page).decode('ascii')}" for page in pages],
            "page_count": page_count,
            "truncated": page_count > len(pages),
            "extracted_json": attachment.extracted_json,
        }
    if file_type in {"docx", "xlsx"}:
        return {**base, "mode": "extracted", "url": None, "text": attachment.extracted_text or "", "html": None, "extracted_json": attachment.extracted_json}
    if file_type is None:
        return {
            **base,
            "mode": "download_only",
            "url": None,
            "text": None,
            "html": None,
            "extracted_json": attachment.extracted_json,
            "warnings": [{"code": "ATTACHMENT_PREVIEW_UNSUPPORTED", "message": "该格式仅支持下载。"}],
        }
    text = attachment.extracted_text
    if not text and attachment.oss_object_id:
        content = await _download_bytes(session, attachment.oss_object_id, stage="attachment_text_download")
        text = content[:2_000_000].decode("utf-8", errors="replace")
    if file_type == "html":
        return {**base, "mode": "html", "url": None, "text": None, "html": sanitize_email_html(text or ""), "extracted_json": attachment.extracted_json}
    return {**base, "mode": "text", "url": None, "text": text or "", "html": None, "extracted_json": attachment.extracted_json}
