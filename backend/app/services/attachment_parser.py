from __future__ import annotations

import asyncio
import base64
import csv
import io
import mimetypes
import re
import zipfile
from html import unescape
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from app.attachments.detector import SUPPORTED_ATTACHMENT_TYPES, detect_file_type
from app.attachments.archive import extract_archive_members
from app.attachments.schemas import AttachmentParseJson, NormalizedAttachmentContent
from app.attachments.safety import inspect_archive_safety
from app.config import settings
from app.integrations.ai_provider import AiProviderError
from app.integrations.qwen_provider import QwenProvider
from app.models import EmailAttachment
from app.services.attachment_precheck import (
    ARCHIVE_CONTENT_TYPES,
    detect_archive_format,
    engineering_reference_metadata,
)
from app.services.common import utcnow
from app.services.logging_safety import safe_error_code
from app.services.storage import download_oss_object_bytes, generate_presigned_url_for_object


_file_parse_semaphore = asyncio.Semaphore(max(1, settings.FILE_PARSE_CONCURRENCY))

def attachment_type(attachment: EmailAttachment) -> str | None:
    """Compatibility wrapper used by existing email and preview services."""
    return detect_file_type(file_name=attachment.file_name, content_type=attachment.content_type)


def _qwen_configured(*, visual: bool = False) -> bool:
    return (
        settings.MULTIMODAL_PROVIDER.lower() == "qwen"
        and bool(settings.QWEN_API_KEY)
        and bool(settings.QWEN_VL_MODEL if visual else settings.QWEN_MODEL)
    )


def _qwen_provider(*, visual: bool = False) -> QwenProvider:
    return QwenProvider(
        api_key=settings.QWEN_API_KEY,
        base_url=settings.QWEN_BASE_URL,
        model=settings.QWEN_VL_MODEL if visual else settings.QWEN_MODEL,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_tokens=settings.AI_MAX_TOKENS,
        structured_output_method=settings.AI_STRUCTURED_OUTPUT_METHOD,
    )


async def _file_io(func, *args, **kwargs):
    async with _file_parse_semaphore:
        return await asyncio.to_thread(func, *args, **kwargs)


def _qwen_retryable(error: AiProviderError) -> bool:
    code = str(error).upper()
    return any(
        marker in code
        for marker in ("TIMEOUT", "HTTP_429", "HTTP_5", "OUTPUT_NOT_JSON", "OUTPUT_SCHEMA_INVALID")
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


def _extract_prc(content: bytes) -> str:
    if not content or content.count(b"\x00") / len(content) > 0.02:
        raise ValueError("PRC_TEXT_UNRECOGNIZED")
    text = _decode_text(content)
    sample = text[:10000]
    printable = sum(character.isprintable() or character in "\r\n\t" for character in sample)
    if sample and printable / len(sample) < 0.9:
        raise ValueError("PRC_TEXT_UNRECOGNIZED")
    return text


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


def _validate_zip_archive(content: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
    except Exception as exc:
        raise ValueError("ARCHIVE_INVALID") from exc
    if len(members) > 1000:
        raise ValueError("ARCHIVE_MEMBER_LIMIT_EXCEEDED")
    expanded_size = sum(member.file_size for member in members)
    compressed_size = sum(max(1, member.compress_size) for member in members)
    if expanded_size > 100 * 1024 * 1024:
        raise ValueError("ARCHIVE_EXPANDED_SIZE_EXCEEDED")
    if expanded_size / max(1, compressed_size) > 100:
        raise ValueError("ARCHIVE_COMPRESSION_RATIO_EXCEEDED")


def _extract_docx(content: bytes) -> tuple[str, list[list[list[str]]], int]:
    _validate_zip_archive(content)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
            embedded_image_count = sum(
                name.startswith("word/media/") and not name.endswith("/")
                for name in archive.namelist()
            )
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
    tables: list[list[list[str]]] = []
    for table in root.iter(f"{namespace}tbl"):
        rows: list[list[str]] = []
        for row in table.findall(f"{namespace}tr"):
            cells = [
                "".join(node.text or "" for node in cell.iter(f"{namespace}t")).strip()
                for cell in row.findall(f"{namespace}tc")
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    table_text = [
        f"# table {index}\n" + "\n".join(" | ".join(row) for row in table)
        for index, table in enumerate(tables, start=1)
    ]
    return "\n".join([*paragraphs, *table_text]), tables, embedded_image_count


def _extract_docx_images(content: bytes) -> list[tuple[str, bytes]]:
    _validate_zip_archive(content)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return [
                (name, archive.read(name))
                for name in sorted(archive.namelist())
                if name.startswith("word/media/") and not name.endswith("/")
            ]
    except Exception as exc:
        raise ValueError("DOCX_IMAGE_EXTRACT_FAILED") from exc


def _extract_xlsx(content: bytes) -> str:
    _validate_zip_archive(content)
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
        if reader.is_encrypted:
            raise ValueError("PDF_ENCRYPTED")
        page_count = len(reader.pages)
        texts = [(reader.pages[index].extract_text() or "") for index in range(min(page_count, max_pages))]
        return "\n".join(part.strip() for part in texts if part.strip()), page_count
    except ValueError:
        raise
    except Exception:
        raise ValueError("PDF_READ_FAILED")


def _inspect_pdf_layout(
    content: bytes,
    *,
    max_pages: int,
) -> tuple[list[list[list[str]]], int]:
    """Extract tables and count embedded page images without model inference."""
    try:
        import fitz  # type: ignore
    except Exception:
        return [], 0
    try:
        document = fitz.open(stream=content, filetype="pdf")
        if document.needs_pass:
            raise ValueError("PDF_ENCRYPTED")
        tables: list[list[list[str]]] = []
        embedded_image_count = 0
        for index in range(min(document.page_count, max_pages)):
            page = document.load_page(index)
            embedded_image_count += len(page.get_images(full=True))
            finder = getattr(page, "find_tables", None)
            if finder is None:
                continue
            for table in finder().tables:
                rows = [
                    ["" if cell is None else str(cell).strip() for cell in row]
                    for row in table.extract()
                ]
                if any(any(cell for cell in row) for row in rows):
                    tables.append(rows)
        document.close()
        return tables, embedded_image_count
    except ValueError:
        raise
    except Exception:
        # Text extraction remains useful when optional layout inspection cannot
        # understand an otherwise valid PDF.
        return [], 0


def parse_binary_content(
    file_type: str,
    content: bytes,
    *,
    max_pdf_pages: int,
) -> NormalizedAttachmentContent:
    """Route raw bytes through deterministic parsers before any model call."""
    parser_name = file_type
    warnings: list[str] = []
    page_count: int | None = None
    if file_type == "txt":
        text = _extract_txt(content)
    elif file_type == "prc":
        text = _extract_prc(content)
    elif file_type == "csv":
        text = _extract_csv(content)
    elif file_type == "html":
        text = _extract_html(content)
    elif file_type == "docx":
        text, tables, embedded_image_count = _extract_docx(content)
    elif file_type == "xlsx":
        text = _extract_xlsx(content)
    elif file_type == "pdf":
        text, page_count = _extract_pdf_text(content, max_pages=max_pdf_pages)
        tables, embedded_image_count = _inspect_pdf_layout(content, max_pages=max_pdf_pages)
        if tables:
            text = "\n".join([
                text,
                *(
                    f"# table {index}\n" + "\n".join(" | ".join(row) for row in table)
                    for index, table in enumerate(tables, start=1)
                ),
            ]).strip()
        if page_count and page_count > max_pdf_pages:
            warnings.append(f"PDF_TRUNCATED_TO_{max_pdf_pages}_PAGES")
    else:
        raise ValueError("NO_DETERMINISTIC_BINARY_PARSER")

    tables = tables if file_type in {"docx", "pdf"} else []
    embedded_image_count = embedded_image_count if file_type in {"docx", "pdf"} else 0
    text, text_truncated = _truncate_text(text)
    if text_truncated:
        warnings.append("TEXT_TRUNCATED_FOR_MODEL_INPUT")
    return NormalizedAttachmentContent(
        file_type=file_type,
        parser=parser_name,
        text=text,
        tables=tables,
        embedded_image_count=embedded_image_count,
        page_count=page_count,
        warnings=warnings,
        truncated=text_truncated or bool(page_count and page_count > max_pdf_pages),
        semantic_mode=(
            "vision"
            if (file_type == "pdf" and (not text.strip() or embedded_image_count > 0))
            or (file_type == "docx" and embedded_image_count > 0)
            else "text"
        ),
    )


def render_pdf_pages(content: bytes, *, max_pages: int) -> tuple[list[bytes], int]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise ValueError("PDF_RENDERER_NOT_AVAILABLE") from exc
    try:
        document = fitz.open(stream=content, filetype="pdf")
        if document.needs_pass:
            raise ValueError("PDF_ENCRYPTED")
        page_count = document.page_count
        pages = [
            document.load_page(index).get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).tobytes("png")
            for index in range(min(page_count, max_pages))
        ]
        document.close()
        return pages, page_count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF_RENDER_FAILED") from exc


def _image_dimensions(content: bytes) -> tuple[int, int] | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if content[:6] in {b"GIF87a", b"GIF89a"} and len(content) >= 10:
        return int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
    if content.startswith(b"\xff\xd8"):
        index = 2
        start_of_frame = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
        while index + 9 < len(content):
            if content[index] != 0xFF:
                index += 1
                continue
            marker = content[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(content):
                break
            segment_size = int.from_bytes(content[index:index + 2], "big")
            if marker in start_of_frame and index + 7 < len(content):
                height = int.from_bytes(content[index + 3:index + 5], "big")
                width = int.from_bytes(content[index + 5:index + 7], "big")
                return width, height
            index += max(segment_size, 2)
    return None


def _fallback_json(file_type: str, text: str, *, warnings: list[str], truncated: bool) -> AttachmentParseJson:
    summary, key_points = _local_summary(text)
    return AttachmentParseJson(
        file_type=file_type,
        parser="deterministic_fallback",
        semantic_mode="none",
        summary=summary,
        key_points=key_points,
        raw_text=text,
        warnings=warnings,
        truncated=truncated,
    )


async def _invoke_qwen(
    session: AsyncSession,
    *,
    attachment: EmailAttachment,
    call_type: str,
    visual: bool,
    input_payload: dict[str, Any],
    invoke,
) -> AttachmentParseJson:
    if not _qwen_configured(visual=visual):
        raise AiProviderError("QWEN_VL_NOT_CONFIGURED" if visual else "QWEN_TEXT_NOT_CONFIGURED")

    max_attempts = max(1, min(3, settings.AI_MAX_RETRIES + 1))
    last_error: AiProviderError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            completion = await invoke(_qwen_provider(visual=visual))
            if hasattr(session, "add"):
                from app.services.ai import persist_ai_log

                await persist_ai_log(
                    session,
                    trace_id=getattr(completion, "trace_id", uuid4().hex),
                    call_type=call_type,
                    input_payload=input_payload,
                    request_payload=getattr(completion, "request_payload", None),
                    output_payload=getattr(completion, "response_payload", None),
                    parsed=completion.parsed,
                    latency_ms=getattr(completion, "latency_ms", None),
                    input_summary=f"attachment_id={attachment.id}; file_type={input_payload.get('file_type')}",
                    output_summary=f"attachment_type={completion.parsed.file_type}; warnings={len(completion.parsed.warnings)}",
                    email_id=attachment.email_id,
                    attachment_id=attachment.id,
                    provider_name="qwen",
                    model_name=settings.QWEN_VL_MODEL if visual else settings.QWEN_MODEL,
                    attempt_count=attempt,
                )
            return completion.parsed
        except AiProviderError as exc:
            last_error = exc
            error_code = safe_error_code(exc, "QWEN_CALL_FAILED")
            if hasattr(session, "add"):
                from app.services.ai import persist_ai_log

                await persist_ai_log(
                    session,
                    trace_id=getattr(exc, "trace_id", uuid4().hex),
                    call_type=call_type,
                    input_payload=input_payload,
                    request_payload=getattr(exc, "request_payload", None),
                    output_payload=(
                        getattr(exc, "response_payload", None)
                        or {"raw_output": getattr(exc, "raw_output", None)}
                    ),
                    parsed=None,
                    latency_ms=getattr(exc, "latency_ms", None),
                    input_summary=f"attachment_id={attachment.id}; file_type={input_payload.get('file_type')}",
                    output_summary=error_code,
                    email_id=attachment.email_id,
                    attachment_id=attachment.id,
                    provider_name="qwen",
                    model_name=settings.QWEN_VL_MODEL if visual else settings.QWEN_MODEL,
                    attempt_count=attempt,
                    error_message=error_code,
                )
            if attempt >= max_attempts or not _qwen_retryable(exc):
                break
            await asyncio.sleep(settings.AI_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    assert last_error is not None
    raise last_error


async def _qwen_text_parse(
    session: AsyncSession,
    *,
    attachment: EmailAttachment,
    file_type: str,
    file_name: str,
    text: str,
    warnings: list[str],
    truncated: bool,
) -> AttachmentParseJson:
    if not _qwen_configured(visual=False):
        raise AiProviderError("QWEN_API_KEY_NOT_CONFIGURED")
    summary, key_points = _local_summary(text)
    prompt = (
        "请将附件内容解析为维修邮件附件级 JSON。只能输出 JSON，字段固定为 "
        "file_type, summary, key_points, extracted_fields, extracted_items, raw_text, warnings, truncated。\n"
        f"file_name={file_name}\nfile_type={file_type}\ntruncated={truncated}\n"
        f"local_summary={summary}\nlocal_key_points={key_points}\ncontent:\n{text}"
    )
    messages = [
            {
                "role": "system",
                "content": "你是维修邮件附件解析助手，只输出 JSON，不编造附件中没有的信息。",
            },
            {"role": "user", "content": prompt},
        ]
    parsed = await _invoke_qwen(
        session,
        attachment=attachment,
        call_type="attachment_text_parse",
        visual=False,
        input_payload={"file_type": file_type, "text": text, "truncated": truncated},
        invoke=lambda provider: provider.chat_json(
            messages=messages,
            response_model=AttachmentParseJson,
            temperature=0.1,
        ),
    )
    parsed.file_type = parsed.file_type or file_type
    parsed.parser = "qwen_text"
    parsed.semantic_mode = "text"
    parsed.warnings = [*warnings, *(parsed.warnings or [])]
    parsed.truncated = bool(parsed.truncated or truncated)
    return parsed


async def _qwen_visual_parse(
    session: AsyncSession,
    *,
    attachment: EmailAttachment,
    file_type: str,
    file_name: str,
    urls: list[str],
    warnings: list[str],
    truncated: bool,
    text_context: str = "",
) -> AttachmentParseJson:
    if not _qwen_configured(visual=True):
        raise AiProviderError("QWEN_API_KEY_NOT_CONFIGURED")
    prompt = (
        "请识别并提取图片或 PDF 附件中的维修报修相关信息。只能输出 JSON，字段固定为 "
        "file_type, summary, key_points, extracted_fields, extracted_items, raw_text, warnings, truncated。"
        f"文件名：{file_name}；文件类型：{file_type}；是否截断：{truncated}。"
    )
    if text_context:
        prompt += f"\nDeterministically extracted text/table context:\n{text_context[:20000]}"
    parsed = await _invoke_qwen(
        session,
        attachment=attachment,
        call_type="attachment_visual_parse",
        visual=True,
        input_payload={
            "file_type": file_type,
            "visual_count": len(urls),
            "truncated": truncated,
            "text_context_length": len(text_context),
        },
        invoke=lambda provider: provider.vl_chat(
            image_urls=urls,
            prompt=prompt,
            response_model=AttachmentParseJson,
            temperature=0.1,
        ),
    )
    parsed.file_type = parsed.file_type or file_type
    parsed.parser = "qwen_vision"
    parsed.semantic_mode = "vision"
    parsed.warnings = [*warnings, *(parsed.warnings or [])]
    parsed.truncated = bool(parsed.truncated or truncated)
    return parsed


async def _visual_url_for_attachment(
    session: AsyncSession,
    attachment: EmailAttachment,
) -> str:
    object_id = attachment.oss_object_id
    if object_id is None:
        raise ValueError("ATTACHMENT_NOT_ARCHIVED")
    return await generate_presigned_url_for_object(session, oss_object_id=object_id, expires_seconds=1800)


def _mark_attachment(
    attachment: EmailAttachment,
    *,
    status: str,
    parsed: AttachmentParseJson | None,
    text: str | None,
    error: str | None = None,
    extracted_json: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    attachment.parse_status = status
    attachment.extracted_text = text
    attachment.extracted_json = extracted_json or ({
        **parsed.model_dump(),
        "parsed_at": utcnow().isoformat(),
    } if parsed else None)
    attachment.parse_error = error
    return attachment.extracted_json


async def _parse_archive_attachment(
    session: AsyncSession,
    *,
    attachment: EmailAttachment,
    content: bytes,
    archive_format: str,
    detection_warnings: list[str],
) -> dict[str, Any] | None:
    try:
        extraction = await _file_io(
            extract_archive_members,
            content,
            archive_format,
            max_depth=settings.ARCHIVE_MAX_DEPTH,
            max_members=settings.ARCHIVE_MAX_MEMBERS,
            max_expanded_bytes=settings.ARCHIVE_MAX_EXPANDED_BYTES,
            max_compression_ratio=settings.ARCHIVE_MAX_COMPRESSION_RATIO,
        )
    except Exception as exc:
        code = safe_error_code(exc, "ARCHIVE_EXTRACTION_FAILED") or "ARCHIVE_EXTRACTION_FAILED"
        safety = await _file_io(
            inspect_archive_safety,
            content,
            archive_format,
            max_members=settings.ARCHIVE_MAX_MEMBERS,
            max_expanded_bytes=settings.ARCHIVE_MAX_EXPANDED_BYTES,
            max_compression_ratio=settings.ARCHIVE_MAX_COMPRESSION_RATIO,
        )
        metadata = engineering_reference_metadata(archive_format, detection_warnings, safety=safety)
        metadata.update({"parse_skip_reason": None, "member_results": [], "archive_error": code})
        return _mark_attachment(
            attachment,
            status="needs_manual_review",
            parsed=None,
            text=None,
            error=code,
            extracted_json=metadata,
        )

    member_results: list[dict[str, Any]] = []
    aggregate_text: list[str] = []
    requires_manual = False
    for member in extraction.members:
        file_name = member.path
        content_type = mimetypes.guess_type(file_name)[0]
        file_type = detect_file_type(file_name=file_name, content_type=content_type)
        if file_type not in SUPPORTED_ATTACHMENT_TYPES:
            member_results.append(
                {
                    "path": file_name,
                    "file_type": file_type or "unsupported",
                    "parse_status": "unsupported",
                    "error_code": "UNSUPPORTED_FILE_TYPE",
                    "archive_depth": member.archive_depth,
                }
            )
            requires_manual = True
            continue
        try:
            parsed = await _parse_archive_member(
                session,
                attachment=attachment,
                file_name=file_name,
                file_type=file_type,
                content=member.content,
            )
            payload = parsed.model_dump()
            payload.update(
                {
                    "path": file_name,
                    "parse_status": "parsed",
                    "archive_depth": member.archive_depth,
                }
            )
            member_results.append(payload)
            if parsed.raw_text:
                aggregate_text.append(f"# {file_name}\n{parsed.raw_text}")
        except Exception as exc:
            code = safe_error_code(exc, "ARCHIVE_MEMBER_PARSE_FAILED") or "ARCHIVE_MEMBER_PARSE_FAILED"
            member_results.append(
                {
                    "path": file_name,
                    "file_type": file_type,
                    "parse_status": "failed",
                    "error_code": code,
                    "archive_depth": member.archive_depth,
                }
            )
            requires_manual = True

    combined_text, truncated = _truncate_text("\n\n".join(aggregate_text))
    metadata = engineering_reference_metadata(
        archive_format,
        detection_warnings,
        safety=extraction.safety,
    )
    metadata.update(
        {
            "parser": "archive_router",
            "semantic_mode": "mixed",
            "parse_skip_reason": None,
            "member_results": member_results,
            "parsed_member_count": sum(item.get("parse_status") == "parsed" for item in member_results),
            "failed_member_count": sum(item.get("parse_status") != "parsed" for item in member_results),
            "archive_warnings": list(extraction.warnings),
            "raw_text": combined_text,
            "truncated": truncated,
            "blocks_ticket_flow": requires_manual,
        }
    )
    return _mark_attachment(
        attachment,
        status="needs_manual_review" if requires_manual else "parsed",
        parsed=None,
        text=combined_text or None,
        error="ARCHIVE_MEMBER_REVIEW_REQUIRED" if requires_manual else None,
        extracted_json=metadata,
    )


async def _parse_archive_member(
    session: AsyncSession,
    *,
    attachment: EmailAttachment,
    file_name: str,
    file_type: str,
    content: bytes,
) -> AttachmentParseJson:
    if file_type == "image":
        data_url = f"data:{mimetypes.guess_type(file_name)[0] or 'image/png'};base64,{base64.b64encode(content).decode('ascii')}"
        return await _qwen_visual_parse(
            session,
            attachment=attachment,
            file_type=file_type,
            file_name=file_name,
            urls=[data_url],
            warnings=[],
            truncated=False,
        )
    normalized = await _file_io(
        parse_binary_content,
        file_type,
        content,
        max_pdf_pages=settings.PDF_MAX_PARSE_PAGES,
    )
    if file_type == "docx" and normalized.embedded_image_count:
        images = await _file_io(_extract_docx_images, content)
        if not images:
            raise ValueError("DOCX_EMBEDDED_IMAGE_MISSING")
        parsed = await _qwen_visual_parse(
            session,
            attachment=attachment,
            file_type=file_type,
            file_name=file_name,
            urls=[
                f"data:{mimetypes.guess_type(name)[0] or 'application/octet-stream'};base64,{base64.b64encode(image).decode('ascii')}"
                for name, image in images
            ],
            warnings=normalized.warnings,
            truncated=normalized.truncated,
            text_context=normalized.text,
        )
        parsed.extracted_fields.setdefault("embedded_image_count", normalized.embedded_image_count)
        parsed.extracted_fields.setdefault("table_count", len(normalized.tables))
        return parsed
    if file_type == "pdf" and normalized.semantic_mode == "vision":
        pages, page_count = await _file_io(render_pdf_pages, content, max_pages=settings.PDF_MAX_PARSE_PAGES)
        if not pages:
            raise ValueError("PDF_RENDER_EMPTY")
        parsed = await _qwen_visual_parse(
            session,
            attachment=attachment,
            file_type=file_type,
            file_name=file_name,
            urls=[f"data:image/png;base64,{base64.b64encode(page).decode('ascii')}" for page in pages],
            warnings=normalized.warnings,
            truncated=page_count > settings.PDF_MAX_PARSE_PAGES,
            text_context=normalized.text,
        )
        parsed.extracted_fields.setdefault("embedded_image_count", normalized.embedded_image_count)
        parsed.extracted_fields.setdefault("table_count", len(normalized.tables))
        return parsed
    if not _qwen_configured(visual=False) or (
        file_type == "txt" and "TEXT_TRUNCATED_FOR_MODEL_INPUT" in normalized.warnings
    ):
        return _fallback_json(
            file_type,
            normalized.text,
            warnings=[*normalized.warnings, "ARCHIVE_MEMBER_LOCAL_FALLBACK"],
            truncated=normalized.truncated,
        )
    try:
        return await _qwen_text_parse(
            session,
            attachment=attachment,
            file_type=file_type,
            file_name=file_name,
            text=normalized.text,
            warnings=normalized.warnings,
            truncated=normalized.truncated,
        )
    except AiProviderError:
        if normalized.text.strip():
            return _fallback_json(
                file_type,
                normalized.text,
                warnings=[*normalized.warnings, "QWEN_FAILED_LOCAL_TEXT_FALLBACK"],
                truncated=normalized.truncated,
            )
        raise


async def parse_attachment(session: AsyncSession, attachment: EmailAttachment) -> dict[str, Any] | None:
    archive_format, warnings = detect_archive_format(
        file_name=attachment.file_name,
        content_type=attachment.content_type,
    )
    if archive_format:
        attachment.content_type = ARCHIVE_CONTENT_TYPES[archive_format]
        if attachment.file_size and attachment.file_size > settings.ATTACHMENT_MAX_ARCHIVE_BYTES:
            safety = inspect_archive_safety(None, archive_format)
            safety = safety.__class__(
                status="unsafe",
                safe=False,
                warnings=("ARCHIVE_SIZE_LIMIT_EXCEEDED",),
            )
        elif attachment.oss_object_id:
            try:
                content = await download_oss_object_bytes(session, oss_object_id=attachment.oss_object_id)
                return await _parse_archive_attachment(
                    session,
                    attachment=attachment,
                    content=content,
                    archive_format=archive_format,
                    detection_warnings=warnings,
                )
            except Exception:
                safety = inspect_archive_safety(None, archive_format)
        else:
            safety = inspect_archive_safety(None, archive_format)
        return _mark_attachment(
            attachment,
            status="skipped" if safety.safe else "needs_manual_review",
            parsed=None,
            text=None,
            error=None if safety.safe else safety.warnings[0],
            extracted_json=engineering_reference_metadata(archive_format, warnings, safety=safety),
        )

    file_type = attachment_type(attachment)
    if file_type not in SUPPORTED_ATTACHMENT_TYPES:
        content = None
        if attachment.oss_object_id:
            try:
                content = await download_oss_object_bytes(session, oss_object_id=attachment.oss_object_id)
            except Exception:
                content = None
        archive_format, warnings = detect_archive_format(
            file_name=attachment.file_name,
            content_type=attachment.content_type,
            content=content,
        )
        if archive_format:
            attachment.content_type = ARCHIVE_CONTENT_TYPES[archive_format]
            if content is not None:
                return await _parse_archive_attachment(
                    session,
                    attachment=attachment,
                    content=content,
                    archive_format=archive_format,
                    detection_warnings=warnings,
                )
            safety = inspect_archive_safety(None, archive_format)
            return _mark_attachment(attachment, status="needs_manual_review", parsed=None, text=None, error=safety.warnings[0], extracted_json=engineering_reference_metadata(archive_format, warnings, safety=safety))
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
            content = await download_oss_object_bytes(session, oss_object_id=attachment.oss_object_id)
            dimensions = _image_dimensions(content)
            if attachment.is_inline and dimensions and (
                dimensions[0] < settings.INLINE_IMAGE_MIN_PARSE_WIDTH
                or dimensions[1] < settings.INLINE_IMAGE_MIN_PARSE_HEIGHT
            ):
                parsed = _fallback_json(
                    file_type,
                    "",
                    warnings=["INLINE_DECORATIVE_SKIPPED"],
                    truncated=False,
                )
                parsed.extracted_fields = {"image_width": dimensions[0], "image_height": dimensions[1]}
                return _mark_attachment(
                    attachment,
                    status="skipped_decorative",
                    parsed=parsed,
                    text=None,
                )
            url = await _visual_url_for_attachment(session, attachment)
            parsed = await _qwen_visual_parse(session, attachment=attachment, file_type=file_type, file_name=attachment.file_name, urls=[url], warnings=warnings, truncated=False)
            return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text)

        content = await download_oss_object_bytes(session, oss_object_id=attachment.oss_object_id)
        normalized = await _file_io(
            parse_binary_content,
            file_type,
            content,
            max_pdf_pages=settings.PDF_MAX_PARSE_PAGES,
        )
        text = normalized.text
        truncated = normalized.truncated
        warnings.extend(normalized.warnings)
        if file_type == "docx" and normalized.embedded_image_count:
            images = await _file_io(_extract_docx_images, content)
            if not images:
                raise ValueError("DOCX_EMBEDDED_IMAGE_MISSING")
            urls = [
                f"data:{mimetypes.guess_type(name)[0] or 'application/octet-stream'};base64,{base64.b64encode(image).decode('ascii')}"
                for name, image in images
            ]
            parsed = await _qwen_visual_parse(
                session,
                attachment=attachment,
                file_type=file_type,
                file_name=attachment.file_name,
                urls=urls,
                warnings=warnings,
                truncated=truncated,
                text_context=text,
            )
            parsed.extracted_fields.setdefault("embedded_image_count", normalized.embedded_image_count)
            parsed.extracted_fields.setdefault("table_count", len(normalized.tables))
            return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text or text)
        if file_type == "pdf" and normalized.semantic_mode == "vision":
                rendered_pages, rendered_page_count = await _file_io(
                    render_pdf_pages,
                    content,
                    max_pages=settings.PDF_MAX_PARSE_PAGES,
                )
                if rendered_page_count > settings.PDF_MAX_PARSE_PAGES and not truncated:
                    truncated = True
                    warnings.append(f"PDF_TRUNCATED_TO_{settings.PDF_MAX_PARSE_PAGES}_PAGES")
                if not rendered_pages:
                    raise ValueError("PDF_RENDER_EMPTY")
                urls = [
                    f"data:image/png;base64,{base64.b64encode(page).decode('ascii')}"
                    for page in rendered_pages
                ]
                parsed = await _qwen_visual_parse(
                    session,
                    attachment=attachment,
                    file_type=file_type,
                    file_name=attachment.file_name,
                    urls=urls,
                    warnings=warnings,
                    truncated=truncated,
                    text_context=text,
                )
                parsed.extracted_fields.setdefault("embedded_image_count", normalized.embedded_image_count)
                parsed.extracted_fields.setdefault("table_count", len(normalized.tables))
                return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text)
        if file_type == "txt" and "TEXT_TRUNCATED_FOR_MODEL_INPUT" in warnings:
            parsed = _fallback_json(
                file_type,
                text,
                warnings=[*warnings, "QWEN_SKIPPED_LARGE_TEXT_LOCAL_FALLBACK"],
                truncated=True,
            )
            return _mark_attachment(attachment, status="parsed", parsed=parsed, text=text)
        parsed = await _qwen_text_parse(session, attachment=attachment, file_type=file_type, file_name=attachment.file_name, text=text, warnings=warnings, truncated=truncated)
        return _mark_attachment(attachment, status="parsed", parsed=parsed, text=parsed.raw_text or text)
    except AiProviderError as exc:
        parsed = _fallback_json(file_type, text, warnings=[*warnings, str(exc)], truncated=truncated)
        if file_type in {"txt", "prc"} and text.strip():
            parsed.warnings.append("QWEN_FAILED_LOCAL_TEXT_FALLBACK")
            return _mark_attachment(attachment, status="parsed", parsed=parsed, text=text)
        return _mark_attachment(attachment, status="needs_manual_review", parsed=parsed, text=text or None, error=str(exc))
    except Exception as exc:
        parsed = _fallback_json(file_type, text, warnings=[*warnings, exc.__class__.__name__], truncated=truncated)
        return _mark_attachment(attachment, status="needs_manual_review", parsed=parsed, text=text or None, error=exc.__class__.__name__)
