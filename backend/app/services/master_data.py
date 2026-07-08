from __future__ import annotations

import csv
import hashlib
import html
import io
import re
import zipfile
from datetime import date
from typing import Any
from xml.etree import ElementTree

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BoardCard, JobRunLog, SnAsset
from app.schemas.business import BoardCardImportItem, SnAssetImportItem
from app.services.audit import log_operation
from app.services.common import model_to_dict, paginate_scalars, utcnow

SN_ASSET_FIELDS = (
    "id",
    "customer_code",
    "customer_name",
    "material_code",
    "material_name",
    "sn",
    "asset_status",
    "warranty_start_date",
    "warranty_end_date",
    "source_file_name",
    "source_file_hash",
    "source_row_no",
    "raw_data",
    "imported_by_user_id",
    "imported_at",
    "created_at",
    "updated_at",
)

BOARD_CARD_FIELDS = (
    "id",
    "material_code",
    "material_name",
    "need_ship_to_beijing",
    "shipping_address",
    "shipping_contact",
    "shipping_phone",
    "postal_code",
    "status",
    "source_file_name",
    "source_file_hash",
    "source_row_no",
    "raw_data",
    "imported_by_user_id",
    "imported_at",
    "created_at",
    "updated_at",
)

EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def list_sn_assets(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    sn: str | None = None,
    customer: str | None = None,
    material: str | None = None,
    asset_status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = _sn_asset_statement(keyword=keyword, sn=sn, customer=customer, material=material, asset_status=asset_status)
    statement = statement.order_by(SnAsset.updated_at.desc(), SnAsset.id.desc())
    rows, total = await paginate_scalars(session, statement, page, page_size)
    return [model_to_dict(row, SN_ASSET_FIELDS) for row in rows], total


def _sn_asset_statement(
    *,
    keyword: str | None = None,
    sn: str | None = None,
    customer: str | None = None,
    material: str | None = None,
    asset_status: str | None = None,
):
    statement = select(SnAsset)
    if asset_status:
        statement = statement.where(SnAsset.asset_status == asset_status)
    if sn:
        statement = statement.where(SnAsset.sn.like(f"%{sn.strip().upper()}%"))
    if customer:
        like = f"%{customer}%"
        statement = statement.where(or_(SnAsset.customer_code.like(like), SnAsset.customer_name.like(like)))
    if material:
        like = f"%{material}%"
        statement = statement.where(or_(SnAsset.material_code.like(like), SnAsset.material_name.like(like)))
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(
            or_(SnAsset.sn.like(like), SnAsset.customer_code.like(like), SnAsset.customer_name.like(like), SnAsset.material_code.like(like))
        )
    return statement


async def import_sn_assets(
    session: AsyncSession,
    *,
    items: list[SnAssetImportItem],
    source_file_name: str | None,
    source_file_hash: str | None,
    user_id: int,
) -> dict[str, Any]:
    job = JobRunLog(job_name="sn_assets_import", job_type="master_data_import", status="running", processed_count=len(items), metadata_json={})
    session.add(job)
    await session.flush()
    created = 0
    updated = 0
    for item in items:
        data = item.model_dump()
        sn = data["sn"].strip().upper()
        row = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
        payload = {
            **data,
            "sn": sn,
            "source_file_name": source_file_name,
            "source_file_hash": source_file_hash,
            "imported_by_user_id": user_id,
            "imported_at": utcnow(),
        }
        if row is None:
            session.add(SnAsset(**payload))
            created += 1
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            updated += 1
    job.status = "success"
    job.finished_at = utcnow()
    job.success_count = created + updated
    job.metadata_json = {"created": created, "updated": updated, "source_file_name": source_file_name}
    await log_operation(
        session,
        user_id=user_id,
        operation_type="sn_assets_imported",
        target_type="sn_assets",
        target_id=None,
        after_data=job.metadata_json,
    )
    return {"job_run_id": job.id, "created": created, "updated": updated, "processed": len(items)}


async def list_board_cards(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    material_code: str | None = None,
    material_name: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = _board_card_statement(keyword=keyword, material_code=material_code, material_name=material_name, status=status)
    statement = statement.order_by(BoardCard.updated_at.desc(), BoardCard.id.desc())
    rows, total = await paginate_scalars(session, statement, page, page_size)
    return [model_to_dict(row, BOARD_CARD_FIELDS) for row in rows], total


def _board_card_statement(
    *,
    keyword: str | None = None,
    material_code: str | None = None,
    material_name: str | None = None,
    status: str | None = None,
):
    statement = select(BoardCard)
    if status:
        statement = statement.where(BoardCard.status == status)
    if material_code:
        statement = statement.where(BoardCard.material_code.like(f"%{material_code}%"))
    if material_name:
        statement = statement.where(BoardCard.material_name.like(f"%{material_name}%"))
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(or_(BoardCard.material_code.like(like), BoardCard.material_name.like(like)))
    return statement


async def import_board_cards(
    session: AsyncSession,
    *,
    items: list[BoardCardImportItem],
    source_file_name: str | None,
    source_file_hash: str | None,
    user_id: int,
) -> dict[str, Any]:
    job = JobRunLog(job_name="board_cards_import", job_type="master_data_import", status="running", processed_count=len(items), metadata_json={})
    session.add(job)
    await session.flush()
    created = 0
    updated = 0
    for item in items:
        data = item.model_dump()
        material_code = data["material_code"].strip()
        row = await session.scalar(select(BoardCard).where(BoardCard.material_code == material_code))
        payload = {
            **data,
            "material_code": material_code,
            "source_file_name": source_file_name,
            "source_file_hash": source_file_hash,
            "imported_by_user_id": user_id,
            "imported_at": utcnow(),
        }
        if row is None:
            session.add(BoardCard(**payload))
            created += 1
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            updated += 1
    job.status = "success"
    job.finished_at = utcnow()
    job.success_count = created + updated
    job.metadata_json = {"created": created, "updated": updated, "source_file_name": source_file_name}
    await log_operation(
        session,
        user_id=user_id,
        operation_type="board_cards_imported",
        target_type="board_cards",
        target_id=None,
        after_data=job.metadata_json,
    )
    return {"job_run_id": job.id, "created": created, "updated": updated, "processed": len(items)}


def _date_or_none(value: str | None) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def _bool_value(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "是", "启用"}


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _xlsx_with_openpyxl(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes | None:
    try:
        from openpyxl import Workbook  # type: ignore
    except ModuleNotFoundError:
        return None

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "data"
    sheet.append(fieldnames)
    for row in rows:
        sheet.append([_string_value(row.get(field)) for field in fieldnames])
    for column_cells in sheet.columns:
        max_length = max(len(_string_value(cell.value)) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(max(max_length + 2, 12), 40)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_fallback(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    def cell(row_index: int, column_index: int, value: Any) -> str:
        text = html.escape(_string_value(value), quote=False)
        ref = f"{_column_name(column_index)}{row_index}"
        return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'

    sheet_rows: list[str] = []
    all_rows = [dict(zip(fieldnames, fieldnames))] + rows
    for row_index, row in enumerate(all_rows, start=1):
        cells = "".join(cell(row_index, column_index, row.get(field)) for column_index, field in enumerate(fieldnames, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="data" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def xlsx_bytes(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    return _xlsx_with_openpyxl(rows, fieldnames) or _xlsx_fallback(rows, fieldnames)


def _read_xlsx_with_openpyxl(content: bytes) -> list[dict[str, Any]] | None:
    try:
        from openpyxl import load_workbook  # type: ignore
    except ModuleNotFoundError:
        return None

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="XLSX_HEADER_REQUIRED")
    headers = [_string_value(value).strip() for value in rows[0]]
    if not any(headers):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="XLSX_HEADER_REQUIRED")
    data: list[dict[str, Any]] = []
    for values in rows[1:]:
        if not any(value not in (None, "") for value in values):
            continue
        data.append({headers[index]: values[index] if index < len(values) else None for index in range(len(headers)) if headers[index]})
    return data


def _cell_index(ref: str | None) -> int:
    if not ref:
        return 0
    match = re.match(r"([A-Z]+)", ref.upper())
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - 64
    return index - 1


def _read_xlsx_fallback(content: bytes) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        sheet_xml = archive.read("xl/worksheets/sheet1.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="XLSX_INVALID_FILE") from exc

    shared_strings: list[str] = []
    try:
        shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in shared_root.findall(".//{*}si"):
            shared_strings.append("".join(text.text or "" for text in item.findall(".//{*}t")))
    except KeyError:
        pass

    root = ElementTree.fromstring(sheet_xml)
    parsed_rows: list[list[str]] = []
    for row in root.findall(".//{*}row"):
        values: list[str] = []
        for cell in row.findall("{*}c"):
            column_index = _cell_index(cell.attrib.get("r"))
            while len(values) <= column_index:
                values.append("")
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = "".join(text.text or "" for text in cell.findall(".//{*}t"))
            else:
                raw = cell.find("{*}v")
                value = raw.text if raw is not None and raw.text is not None else ""
                if cell_type == "s" and value.isdigit() and int(value) < len(shared_strings):
                    value = shared_strings[int(value)]
            values[column_index] = value
        if any(value != "" for value in values):
            parsed_rows.append(values)
    if not parsed_rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="XLSX_HEADER_REQUIRED")
    headers = [value.strip() for value in parsed_rows[0]]
    if not any(headers):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="XLSX_HEADER_REQUIRED")
    return [
        {headers[index]: row[index] if index < len(row) else "" for index in range(len(headers)) if headers[index]}
        for row in parsed_rows[1:]
    ]


def _read_xlsx(content: bytes) -> tuple[list[dict[str, Any]], str]:
    rows = _read_xlsx_with_openpyxl(content)
    if rows is None:
        rows = _read_xlsx_fallback(content)
    return rows, hashlib.sha256(content).hexdigest()


def parse_sn_assets_xlsx(content: bytes) -> tuple[list[SnAssetImportItem], str]:
    rows, file_hash = _read_xlsx(content)
    items: list[SnAssetImportItem] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        try:
            items.append(
                SnAssetImportItem(
                    customer_code=_string_value(row.get("customer_code")).strip(),
                    customer_name=_string_value(row.get("customer_name")).strip(),
                    material_code=_string_value(row.get("material_code")).strip(),
                    material_name=_string_value(row.get("material_name")).strip() or None,
                    sn=_string_value(row.get("sn")).strip(),
                    asset_status=_string_value(row.get("asset_status") or "valid").strip(),
                    warranty_start_date=row.get("warranty_start_date")
                    if isinstance(row.get("warranty_start_date"), date)
                    else _date_or_none(_string_value(row.get("warranty_start_date"))),
                    warranty_end_date=row.get("warranty_end_date")
                    if isinstance(row.get("warranty_end_date"), date)
                    else _date_or_none(_string_value(row.get("warranty_end_date"))),
                    source_row_no=index,
                    raw_data={key: _string_value(value) for key, value in row.items()},
                )
            )
        except (ValueError, ValidationError) as exc:
            errors.append({"row": index, "error": str(exc)})
    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "XLSX_VALIDATION_FAILED", "errors": errors})
    return items, file_hash


def parse_board_cards_xlsx(content: bytes) -> tuple[list[BoardCardImportItem], str]:
    rows, file_hash = _read_xlsx(content)
    items: list[BoardCardImportItem] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        try:
            items.append(
                BoardCardImportItem(
                    material_code=_string_value(row.get("material_code")).strip(),
                    material_name=_string_value(row.get("material_name")).strip() or None,
                    need_ship_to_beijing=_bool_value(_string_value(row.get("need_ship_to_beijing"))),
                    shipping_address=_string_value(row.get("shipping_address")).strip() or None,
                    shipping_contact=_string_value(row.get("shipping_contact")).strip() or None,
                    shipping_phone=_string_value(row.get("shipping_phone")).strip() or None,
                    postal_code=_string_value(row.get("postal_code")).strip() or None,
                    status=_string_value(row.get("status") or "active").strip(),
                    source_row_no=index,
                    raw_data={key: _string_value(value) for key, value in row.items()},
                )
            )
        except (ValueError, ValidationError) as exc:
            errors.append({"row": index, "error": str(exc)})
    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "XLSX_VALIDATION_FAILED", "errors": errors})
    return items, file_hash


def _read_csv(content: bytes) -> tuple[list[dict[str, str]], str]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV_HEADER_REQUIRED")
    return [dict(row) for row in reader], hashlib.sha256(content).hexdigest()


def parse_sn_assets_csv(content: bytes) -> tuple[list[SnAssetImportItem], str]:
    rows, file_hash = _read_csv(content)
    items: list[SnAssetImportItem] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        try:
            items.append(
                SnAssetImportItem(
                    customer_code=(row.get("customer_code") or "").strip(),
                    customer_name=(row.get("customer_name") or "").strip(),
                    material_code=(row.get("material_code") or "").strip(),
                    material_name=(row.get("material_name") or "").strip() or None,
                    sn=(row.get("sn") or "").strip(),
                    asset_status=(row.get("asset_status") or "valid").strip(),
                    warranty_start_date=_date_or_none(row.get("warranty_start_date")),
                    warranty_end_date=_date_or_none(row.get("warranty_end_date")),
                    source_row_no=index,
                    raw_data=row,
                )
            )
        except (ValueError, ValidationError) as exc:
            errors.append({"row": index, "error": str(exc)})
    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "CSV_VALIDATION_FAILED", "errors": errors})
    return items, file_hash


def parse_board_cards_csv(content: bytes) -> tuple[list[BoardCardImportItem], str]:
    rows, file_hash = _read_csv(content)
    items: list[BoardCardImportItem] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        try:
            items.append(
                BoardCardImportItem(
                    material_code=(row.get("material_code") or "").strip(),
                    material_name=(row.get("material_name") or "").strip() or None,
                    need_ship_to_beijing=_bool_value(row.get("need_ship_to_beijing")),
                    shipping_address=(row.get("shipping_address") or "").strip() or None,
                    shipping_contact=(row.get("shipping_contact") or "").strip() or None,
                    shipping_phone=(row.get("shipping_phone") or "").strip() or None,
                    postal_code=(row.get("postal_code") or "").strip() or None,
                    status=(row.get("status") or "active").strip(),
                    source_row_no=index,
                    raw_data=row,
                )
            )
        except (ValueError, ValidationError) as exc:
            errors.append({"row": index, "error": str(exc)})
    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": "CSV_VALIDATION_FAILED", "errors": errors})
    return items, file_hash


def csv_bytes(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def sn_assets_template_csv() -> bytes:
    return csv_bytes(
        [
            {
                "sn": "SN202607070001",
                "customer_code": "CUST001",
                "customer_name": "示例客户",
                "material_code": "MAT001",
                "material_name": "示例物料",
                "asset_status": "valid",
                "warranty_start_date": "2026-01-01",
                "warranty_end_date": "2027-01-01",
            }
        ],
        ["sn", "customer_code", "customer_name", "material_code", "material_name", "asset_status", "warranty_start_date", "warranty_end_date"],
    )


def board_cards_template_csv() -> bytes:
    return csv_bytes(
        [
            {
                "material_code": "MAT001",
                "material_name": "示例物料",
                "need_ship_to_beijing": "true",
                "shipping_address": "北京市示例地址",
                "shipping_contact": "张三",
                "shipping_phone": "010-00000000",
                "postal_code": "100000",
                "status": "active",
            }
        ],
        ["material_code", "material_name", "need_ship_to_beijing", "shipping_address", "shipping_contact", "shipping_phone", "postal_code", "status"],
    )


def sn_assets_template_xlsx() -> bytes:
    return xlsx_bytes(
        [
            {
                "sn": "SN202607070001",
                "customer_code": "CUST001",
                "customer_name": "示例客户",
                "material_code": "MAT001",
                "material_name": "示例物料",
                "asset_status": "valid",
                "warranty_start_date": "2026-01-01",
                "warranty_end_date": "2027-01-01",
            }
        ],
        ["sn", "customer_code", "customer_name", "material_code", "material_name", "asset_status", "warranty_start_date", "warranty_end_date"],
    )


def board_cards_template_xlsx() -> bytes:
    return xlsx_bytes(
        [
            {
                "material_code": "MAT001",
                "material_name": "示例物料",
                "need_ship_to_beijing": "true",
                "shipping_address": "北京市示例地址",
                "shipping_contact": "张三",
                "shipping_phone": "010-00000000",
                "postal_code": "100000",
                "status": "active",
            }
        ],
        ["material_code", "material_name", "need_ship_to_beijing", "shipping_address", "shipping_contact", "shipping_phone", "postal_code", "status"],
    )


async def export_sn_assets(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    sn: str | None = None,
    customer: str | None = None,
    material: str | None = None,
    asset_status: str | None = None,
) -> bytes:
    statement = _sn_asset_statement(keyword=keyword, sn=sn, customer=customer, material=material, asset_status=asset_status).order_by(
        SnAsset.updated_at.desc(), SnAsset.id.desc()
    )
    rows = (await session.execute(statement)).scalars().all()
    return xlsx_bytes([model_to_dict(row, SN_ASSET_FIELDS) for row in rows], list(SN_ASSET_FIELDS))


async def export_board_cards(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    material_code: str | None = None,
    material_name: str | None = None,
    status: str | None = None,
) -> bytes:
    statement = _board_card_statement(keyword=keyword, material_code=material_code, material_name=material_name, status=status).order_by(
        BoardCard.updated_at.desc(), BoardCard.id.desc()
    )
    rows = (await session.execute(statement)).scalars().all()
    return xlsx_bytes([model_to_dict(row, BOARD_CARD_FIELDS) for row in rows], list(BOARD_CARD_FIELDS))
