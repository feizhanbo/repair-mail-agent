from __future__ import annotations

import csv
import io
import re
import zipfile
from html import unescape
from pathlib import PurePath
from typing import Any
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.ai_provider import AiProviderError
from app.integrations.qwen_provider import QwenProvider
from app.models import EmailAttachment
from app.services.common import utcnow
from app.services.storage import download_oss_object_bytes, generate_presigned_url_for_object, upload_bytes_to_oss


SUPPORTED_ATTACHMENT_TYPES = {"docx", "xlsx", "csv", "txt", "html", "image", "pdf"}


class AttachmentParseJson(BaseModel):
    file_type: str
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    extracted_items: list[dict[str, Any]] = Field(default_factory=list)
    raw_text: str = ""
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False


def attachment_type(attachment: EmailAttachment) -> str | None:
    content_type = (attachment.content_type or "").lower().split(";", 1)[0].strip()
    suffix = PurePath(attachment.file_name or "").suffix.lower().lstrip(".")
    if content_type.startswith("image/"):
        return "image"
    if content_type == "application/pdf" or suffix == "pdf":
        return "pdf"
    if suffix in {"docx", "xlsx", "csv", "txt", "html", "htm"}:
        return "html" if suffix == "htm" else suffix
    if content_type in {"text/plain", "application/json", "application/xml", "text/xml"}:
        return "txt"
    if content_type in {"text/csv", "application/csv"}:
        return "csv"
    if content_type in {"text/html", "application/xhtml+xml"}:
        return "html"
    return None


def _qwen_configured() -> bool:
    return (
        settings.MULTIMODAL_PROVIDER.lower() == "qwen"
        and bool(settings.QWEN_API_KEY)
        and bool(settings.QWEN_VL_MODEL or settings.QWEN_MODEL)
    )


def _qwen_provider() -> QwenProvider:
    return QwenProvider(
        api_key=settings.QWEN_API_KEY,
        base_url=settings.QWEN_BASE_URL,
        model=settings.QWEN_VL_MODEL or settings.QWEN_MODEL,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
    )


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _compact_lines(text: str, *, limit: int = 12) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line][:limit]


def _local_summary(text: str) -> tuple[str, list[str]]:
    lines = _compact_lines(text, limit=8)
    return ("\n".join(lines)[:1000], lines[:5])


def _truncate_text(text: str) -> tuple[str, bool]:
    limit = max(1000, settings.ATTACHMENT_TEXT_MAX_CHARS)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _extract_txt(content: bytes) -> str:
    return _decode_text(content)


def _extract_html(content: bytes) -> str:
    html = _decode_text(content)
    return unescape(BeautifulSoup(html, "lxml").get_text("\n"))


def _extract_csv(content: bytes) -> str:
    text = _decode_text(content)
    reader = csv.reader(io.StringIO(text))
    rows: list[str] = []
    for index, row in enumerate(reader):
        if index >= 200:
            rows.append("... truncated after 200 rows ...")
            break
        rows.append(" | ".join(str(cell).strip() for cell in row))
    return "\n".join(rows)


def _extract_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
    except Exception as exc:
        raise ValueError("DOCX_TEXT_EXTRACT_FAILED") from exc
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_xlsx(content: bytes) -> str:
    try:
        from openpyxl import load_workbook  # type: ignore
    except Exception as exc:
        raise ValueError("XLSX_READER_NOT_AVAILABLE") from exc
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("XLSX_TEXT_EXTRACT_FAILED") from exc
    blocks: list[str] = []
    for sheet in workbook.worksheets[:10]:
        blocks.append(f"# sheet: {sheet.title}")
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > 200:
                blocks.append("... truncated after 200 rows ...")
                break
            values = ["" if value is None else str(value) for value in row[:40]]
            if any(value.strip() for value in values):
                blocks.append(" | ".join(values))
    return "\n".join(blocks)


def _pdf_page_count(content: bytes) -> int | None:
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(content))
        return len(reader.pages)
    except Exception:
        matches = re.findall(rb"/Type\s*/Page\b", content)
        return len(matches) or None


def _extract_pdf_text(content: bytes, *, max_pages: int) -> tuple[str, int | None]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return "", _pdf_page_count(content)
    try:
        reader = PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
        texts = [(reader.pages[index].extract_text() or "") for index in range(min(page_count, max_pages))]
        return "\n".join(part.strip() for part in texts if part.strip()), page_count
    except Exception:
        return "", _pdf_page_count(content)


def _first_pdf_pages(content: bytes, *, max_pages: int) -> bytes | None:
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) <= max_pages:
            return None
        writer = PdfWriter()
        for index in range(max_pages):
            writer.add_page(reader.pages[index])
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception:
        return None


def _fallback_json(file_type: str, text: str, *, warnings: list[str], truncated: bool) -> AttachmentParseJson:
    summary, key_points = _local_summary(text)
    return AttachmentParseJson(
        file_type=file_type,
        summary=summary,
        key_points=key_points,
        raw_text=text,
        warnings=warnings,
        truncated=truncated,
    )


async def _qwen_text_parse(*, file_type: str, file_name: str, text: str, warnings: list[str], truncated: bool) -> AttachmentParseJson:
    if not _qwen_configured():
        raise AiProviderError("QWEN_API_KEY_NOT_CONFIGURED")
    summary, key_points = _local_summary(text)
    prompt = (
        "请将附件内容解析为维修邮件附件级 JSON。只能输出 JSON，字段固定为 "
        "file_type, summary, key_points, extracted_fields, extracted_items, raw_text, warnings, truncated。\n"
        f"file_name={file_name}\nfile_type={file_type}\ntruncated={truncated}\n"
        f"local_summary={summary}\nlocal_key_points={key_points}\ncontent:\n{text}"
    )
    completion = await _qwen_provider().chat_json(
        messages=[
            {
                "role": "system",
                "content": "你是维修邮件附件解析助手，只输出 JSON，不编造附件中没有的信息。",
            },
            {"role": "user", "content": prompt},
        ],
        response_model=AttachmentParseJson,
        temperature=0.1,
    )
    parsed = completion.parsed
    parsed.file_type = parsed.file_type or file_type
    parsed.warnings = [*warnings, *(parsed.warnings or [])]
    parsed.truncated = bool(parsed.truncated or truncated)
    return parsed


async def _qwen_visual_parse(*, file_type: str, file_name: str, url: str, warnings: list[str], truncated: bool) -> AttachmentParseJson:
    if not _qwen_configured():
        raise AiProviderError("QWEN_API_KEY_NOT_CONFIGURED")
    prompt = (
        "请识别并提取图片或 PDF 附件中的维修报修相关信息。只能输出 JSON，字段固定为 "
        "file_type, summary, key_points, extracted_fields, extracted_items, raw_text, warnings, truncated。"
        f"文件名：{file_name}；文件类型：{file_type}；是否截断：{truncated}。"
    )
    completion = await _qwen_provider().vl_chat(
        image_urls=[url],
        prompt=prompt,
        response_model=AttachmentParseJson,
        temperature=0.1,
    )
    parsed = completion.parsed
    parsed.file_type = parsed.file_type or file_type
    parsed.warnings = [*warnings, *(parsed.warnings or [])]
    parsed.truncated = bool(parsed.truncated or truncated)
    return parsed


async def _visual_url_for_attachment(
    session: AsyncSession,
    attachment: EmailAttachment,
    *,
    pdf_content: bytes | None = None,
    truncated: bool = False,
) -> str:
    object_id = attachment.oss_object_id
    if object_id is None:
        raise ValueError("ATTACHMENT_NOT_ARCHIVED")
    if pdf_content is not None and truncated:
        preview = _first_pdf_pages(pdf_content, max_pages=settings.PDF_MAX_PARSE_PAGES)
        if preview:
            preview_object = await upload_bytes_to_oss(
                session,
                content=preview,
                original_file_name=f"{attachment.file_name}.first-{settings.PDF_MAX_PARSE_PAGES}.pdf",
                content_type="application/pdf",
                source_type="email_attachment_preview",
            )
            object_id = preview_object.id
    return await generate_presigned_url_for_object(session, oss_object_id=object_id, expires_seconds=1800)


def _mark_attachment(
    attachment: EmailAttachment,
    *,
    status: str,
    parsed: AttachmentParseJson | None,
    text: str | None,
    error: str | None = None,
) -> dict[str, Any] | None:
    attachment.parse_status = status
    attachment.extracted_text = text
    attachment.extracted_json = {
        **(parsed.model_dump() if parsed else {}),
        "parsed_at": utcnow().isoformat(),
    } if parsed else None
    attachment.parse_error = error
    return attachment.extracted_json


async def parse_attachment(session: AsyncSession, attachment: EmailAttachment) -> dict[str, Any] | None:
    file_type = attachment_type(attachment)
    if file_type not in SUPPORTED_ATTACHMENT_TYPES:
        return _mark_attachment(
            attachment,
            status="unsupported",
            parsed=None,
            text=None,
            error="UNSUPPORTED_FILE_TYPE",
        )

    if not attachment.oss_object_id:
        return _mark_attachment(
            attachment,
            status="needs_manual_review",
            parsed=_fallback_json(file_type, "", warnings=["ATTACHMENT_NOT_ARCHIVED"], truncated=False),
            text=None,
            error="ATTACHMENT_NOT_ARCHIVED",
        )

    if attachment.file_size and attachment.file_size > settings.ATTACHMENT_MAX_AUTO_PARSE_BYTES:
        return _mark_attachment(
            attachment,
            status="needs_manual_review",
            parsed=_fallback_json(file_type, "", warnings=["FILE_TOO_LARGE_FOR_AUTO_PARSE"], truncated=True),
            text=None,
            error="FILE_TOO_LARGE_FOR_AUTO_PARSE",
        )

    warnings: list[str] = []
    truncated = False
    text = ""
    try:
        if file_type == "image":
            url = await _visual_url_for_attachment(session, attachment)
            parsed = await _qwen_visual_parse(file_type=file_type, file_name=attachment.file_name, url=url, warnings=warnings, truncated=False)
            return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text)

        content = await download_oss_object_bytes(session, oss_object_id=attachment.oss_object_id)
        if file_type == "txt":
            text = _extract_txt(content)
        elif file_type == "csv":
            text = _extract_csv(content)
        elif file_type == "html":
            text = _extract_html(content)
        elif file_type == "docx":
            text = _extract_docx(content)
        elif file_type == "xlsx":
            text = _extract_xlsx(content)
        elif file_type == "pdf":
            text, page_count = _extract_pdf_text(content, max_pages=settings.PDF_MAX_PARSE_PAGES)
            if page_count and page_count > settings.PDF_MAX_PARSE_PAGES:
                truncated = True
                warnings.append(f"PDF_TRUNCATED_TO_{settings.PDF_MAX_PARSE_PAGES}_PAGES")
            if not text.strip():
                url = await _visual_url_for_attachment(session, attachment, pdf_content=content, truncated=truncated)
                parsed = await _qwen_visual_parse(file_type=file_type, file_name=attachment.file_name, url=url, warnings=warnings, truncated=truncated)
                return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text)

        text, text_truncated = _truncate_text(text)
        truncated = truncated or text_truncated
        if text_truncated:
            warnings.append("TEXT_TRUNCATED_FOR_MODEL_INPUT")
        parsed = await _qwen_text_parse(file_type=file_type, file_name=attachment.file_name, text=text, warnings=warnings, truncated=truncated)
        return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text or text)
    except AiProviderError as exc:
        parsed = _fallback_json(file_type, text, warnings=[*warnings, str(exc)], truncated=truncated)
        return _mark_attachment(attachment, status="needs_manual_review", parsed=parsed, text=text or None, error=str(exc))
    except Exception as exc:
        parsed = _fallback_json(file_type, text, warnings=[*warnings, exc.__class__.__name__], truncated=truncated)
        return _mark_attachment(attachment, status="needs_manual_review", parsed=parsed, text=text or None, error=exc.__class__.__name__)
