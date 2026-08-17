from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import fitz
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ExportSap, RepairTicket, RepairTicketItem, TicketRma


TEMPLATE_VERSION = "rma_authorization_v3_2_reference"
TEMPLATE_SHA256 = "8e7a2c5b7bc448a785d3698300acf0e2554a9d953e779cccafbce307e80853a0"
LEGACY_TEMPLATE_VERSIONS = {"v1", "rma_authorization_v1", "rma_authorization_zh_v1"}
CODE39_PATTERN = re.compile(r"^[0-9A-Z\-. $/+%]+$")

CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn",
    "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw",
    "8": "wnnwnnwnn", "9": "nnwwnnwnn", "A": "wnnnnwnnw", "B": "nnwnnwnnw",
    "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn", "F": "nnwnwwnnn",
    "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww",
    "O": "wnnnwnnwn", "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn",
    "S": "nnwnnnwwn", "T": "nnnnwnwwn", "U": "wwnnnnnnw", "V": "nwwnnnnnw",
    "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn", "Z": "nwwnwnnnn",
    "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "$": "nwnwnwnnn",
    "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn", "*": "nwnnwnwnn",
}

ROW_FIELD_NAMES = (
    "item_no", "item_part_no", "item_description", "item_qty", "item_rma_no",
    "item_serial_no", "item_price", "item_program", "item_advance", "item_failure",
    "item_delivery",
)


class RmaPdfError(ValueError):
    pass


class RmaItemData(BaseModel):
    """One physical SN maps to exactly one visible row."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    no: int | None = Field(default=None, ge=1, le=9999)
    part_no: str = Field(min_length=1, max_length=40)
    part_description: str = Field(min_length=1, max_length=160)
    quantity: int = Field(default=1, ge=1, le=9999)
    part_serial_no: str = Field(min_length=1, max_length=80)
    maintenance_price: Decimal | None = Field(default=None, ge=0)
    program_running: str = Field(default="", max_length=80)
    advance_replacement: str = Field(default="N", max_length=40)
    failure_description: str = Field(default="", max_length=240)
    delivery: str = Field(default="2 WEEKS", max_length=40)


class RmaPdfData(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    rma_no: str = Field(min_length=1, max_length=30)
    request_date: date
    currency: str = Field(default="", max_length=10)
    customer_code: str = Field(default="", max_length=50)
    customer_name: str = Field(min_length=1, max_length=120)
    mailing_address: str = Field(min_length=1, max_length=220)
    mailing_contact_person: str = Field(min_length=1, max_length=60)
    mailing_contact_phone: str = Field(default="", max_length=60)
    delivery_fee_paid_by_customer: str = Field(default="", max_length=100)
    repair_fee_paid_by_customer: str = Field(default="", max_length=100)
    total_cost: Decimal = Field(default=Decimal("0"), ge=0)
    items: list[RmaItemData] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def normalize_and_validate_document(self) -> "RmaPdfData":
        self.rma_no = self.rma_no.upper()
        self.currency = self.currency.upper()
        if "*" in self.rma_no or not CODE39_PATTERN.fullmatch(self.rma_no):
            raise ValueError("RMA_CODE39_VALUE_INVALID")
        normalized_sns = [item.part_serial_no.casefold() for item in self.items]
        if len(normalized_sns) != len(set(normalized_sns)):
            raise ValueError("RMA_DUPLICATE_SN")
        if any(item.quantity != 1 for item in self.items):
            raise ValueError("RMA_ITEM_QUANTITY_SN_CONFLICT")
        return self

    @property
    def mailing_contact(self) -> str:
        return f"{self.mailing_contact_person}{self.mailing_contact_phone}"


@dataclass(frozen=True)
class _FieldSpec:
    rect: tuple[float, float, float, float]
    font_size: float
    min_font_size: float
    align: str
    max_lines: int
    padding: float

    def offset_y(self, offset: float) -> "_FieldSpec":
        x0, y0, x1, y1 = self.rect
        return replace(self, rect=(x0, y0 + offset, x1, y1 + offset))


class _FixedBoxTextWriter:
    def __init__(self, font_buffer: bytes | None) -> None:
        self.font_buffer = font_buffer
        if self.font_buffer:
            self.font = fitz.Font(fontbuffer=self.font_buffer)
            self.font_name = "RMA_CJK"
        else:
            self.font = fitz.Font(fontname="china-s")
            self.font_name = "china-s"

    def register(self, page: fitz.Page) -> None:
        if self.font_buffer:
            page.insert_font(fontname=self.font_name, fontbuffer=self.font_buffer)
        else:
            page.insert_font(fontname=self.font_name)

    def write(self, page: fitz.Page, spec: _FieldSpec, value: Any) -> None:
        text = "" if value is None else str(value)
        if not text:
            return
        box = fitz.Rect(*spec.rect)
        inner = fitz.Rect(
            box.x0 + spec.padding,
            box.y0 + spec.padding,
            box.x1 - spec.padding,
            box.y1 - spec.padding,
        )
        size = spec.font_size
        while size >= spec.min_font_size - 1e-6:
            lines = self._wrap(text, inner.width, size, spec.max_lines)
            line_height = size * 1.12
            if lines and len(lines) * line_height <= inner.height:
                self._draw(page, inner, lines, size, line_height, spec.align)
                return
            size = round(size - 0.25, 2)
        raise RmaPdfError(f"RMA_FIELD_OVERFLOW:{text[:40]}")

    def _wrap(self, text: str, max_width: float, font_size: float, max_lines: int) -> list[str]:
        if self.font.text_length(text, fontsize=font_size) <= max_width:
            return [text]
        if max_lines <= 1:
            return []
        lines: list[str] = []
        current = ""
        last_break = -1
        break_chars = set(" -_/，。；：、()（）")
        for char in text:
            candidate = current + char
            if char in break_chars:
                last_break = len(candidate)
            if self.font.text_length(candidate, fontsize=font_size) <= max_width:
                current = candidate
                continue
            if not current:
                return []
            if last_break > 0:
                line = current[:last_break].rstrip()
                current = current[last_break:].lstrip() + char
            else:
                line, current = current, char
            lines.append(line)
            if len(lines) >= max_lines:
                return []
            last_break = max((index for index, existing in enumerate(current, 1) if existing in break_chars), default=-1)
        if current:
            lines.append(current)
        return lines if len(lines) <= max_lines else []

    def _draw(
        self,
        page: fitz.Page,
        rect: fitz.Rect,
        lines: list[str],
        font_size: float,
        line_height: float,
        align: str,
    ) -> None:
        total_height = len(lines) * line_height
        baseline = rect.y0 + (rect.height - total_height) / 2 + self.font.ascender * font_size
        for index, line in enumerate(lines):
            width = self.font.text_length(line, fontsize=font_size)
            if align == "center":
                x = rect.x0 + (rect.width - width) / 2
            elif align == "right":
                x = rect.x1 - width
            else:
                x = rect.x0
            page.insert_text(
                fitz.Point(x, baseline + index * line_height),
                line,
                fontname=self.font_name,
                fontsize=font_size,
                color=(0, 0, 0),
                overlay=True,
            )


def _authorization_no(ticket_no: str) -> str:
    value = ticket_no.strip().upper()
    return value[3:] if value.startswith("RMA") and len(value) > 3 else value


def _resolve_cjk_font_path() -> Path | None:
    if settings.RMA_CJK_FONT_PATH:
        configured = Path(settings.RMA_CJK_FONT_PATH)
        if not configured.is_file():
            raise RmaPdfError("RMA_CJK_FONT_UNAVAILABLE")
        return configured
    candidates = (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _subset_cjk_font(font_path: Path | None, text: str) -> bytes | None:
    if font_path is None:
        return None
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont

        kwargs = {"fontNumber": 0} if font_path.suffix.lower() in {".ttc", ".otc"} else {}
        font = TTFont(str(font_path), **kwargs)
        options = subset.Options()
        options.recalc_average_width = True
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(text=text)
        subsetter.subset(font)
        output = io.BytesIO()
        font.save(output)
        result = output.getvalue()
        if not result:
            raise ValueError("empty subset")
        return result
    except Exception as exc:
        raise RmaPdfError("RMA_FONT_SUBSET_FAILED") from exc


def _dynamic_text(data: RmaPdfData) -> str:
    values: list[str] = [
        data.rma_no,
        f"{data.request_date.year}/{data.request_date.month}/{data.request_date.day}",
        data.currency,
        data.customer_name,
        data.mailing_address,
        data.mailing_contact,
        data.delivery_fee_paid_by_customer,
        data.repair_fee_paid_by_customer,
        format(data.total_cost.normalize(), "f"),
    ]
    for row_number, item in enumerate(data.items, start=1):
        values.extend(str(value) for value in _row_values(data, item, row_number).values())
    return "\n".join(values)


def _load_layout(path: str | Path | None = None) -> dict[str, Any]:
    layout_path = Path(path or settings.RMA_PDF_LAYOUT_PATH)
    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED") from exc
    if layout.get("template_version") != TEMPLATE_VERSION or layout.get("template_sha256") != TEMPLATE_SHA256:
        raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
    return layout


def _field_specs(layout: dict[str, Any]) -> dict[str, _FieldSpec]:
    result: dict[str, _FieldSpec] = {}
    for name, raw in layout["fields"].items():
        result[name] = _FieldSpec(
            rect=tuple(float(value) for value in raw["rect"]),
            font_size=float(raw["font_size"]),
            min_font_size=float(raw["min_font_size"]),
            align=str(raw["align"]),
            max_lines=int(raw["max_lines"]),
            padding=float(raw["padding"]),
        )
    return result


def _normalized_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def validate_rma_template_integrity(
    template_path: str | Path | None = None,
    *,
    layout_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the immutable base template and return safe health metadata."""
    path = Path(template_path or settings.RMA_PDF_TEMPLATE_PATH)
    layout = _load_layout(layout_path)
    try:
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED") from exc
    if file_hash != TEMPLATE_SHA256:
        raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED") from exc
    try:
        if document.page_count != int(layout["template_page_count"]):
            raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
        for page in document:
            if abs(page.rect.width - float(layout["page_width"])) > 0.5 or abs(page.rect.height - float(layout["page_height"])) > 0.5:
                raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
        page_texts = [_normalized_text(page.get_text()) for page in document]
        if _normalized_text(str(layout["title"])) not in page_texts[0]:
            raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
        for page_index, anchors in layout["static_anchors"].items():
            if not all(_normalized_text(anchor) in page_texts[int(page_index)] for anchor in anchors):
                raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
    finally:
        document.close()
    return {
        "status": "healthy",
        "template_version": TEMPLATE_VERSION,
        "template_sha256": file_hash,
        "page_count": int(layout["template_page_count"]),
        "page_size": [float(layout["page_width"]), float(layout["page_height"])],
    }


def validate_rma_runtime_health() -> dict[str, Any]:
    report = validate_rma_template_integrity()
    font_path = _resolve_cjk_font_path()
    report["cjk_font"] = str(font_path) if font_path else "builtin-china-s"
    return report


def _validate_snapshot(
    ticket: RepairTicket,
    items: list[RepairTicketItem],
    snapshot: dict[str, Any] | None,
) -> None:
    if snapshot is None:
        return
    expected_top = {
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "customer_name": ticket.customer_name,
        "contact_person": ticket.contact_person,
        "contact_phone": ticket.contact_phone,
        "contact_email": (ticket.contact_email or "").lower(),
        "request_date": ticket.request_date.isoformat() if isinstance(ticket.request_date, date) else ticket.request_date,
        "mailing_address": ticket.mailing_address,
    }
    if any(snapshot.get(key) != value for key, value in expected_top.items()):
        raise RmaPdfError("RMA_SAFETY_SNAPSHOT_MISMATCH")
    snapshot_items = snapshot.get("items")
    if not isinstance(snapshot_items, list) or len(snapshot_items) != len(items):
        raise RmaPdfError("RMA_SAFETY_SNAPSHOT_MISMATCH")
    for item, saved in zip(items, snapshot_items, strict=True):
        expected = {
            "id": item.id,
            "line_no": item.line_no,
            "material_code": item.material_code,
            "material_name": item.material_name,
            "sn": (item.sn or "").strip().upper(),
            "quantity": item.quantity,
            "failure_description": item.failure_description,
        }
        if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in expected.items()):
            raise RmaPdfError("RMA_SAFETY_SNAPSHOT_MISMATCH")


def _latest_export_rows_by_item(rows: Iterable[ExportSap]) -> dict[int, ExportSap]:
    latest: dict[int, ExportSap] = {}
    for row in rows:
        current = latest.get(row.ticket_item_id)
        if current is None or (row.id or 0) > (current.id or 0):
            latest[row.ticket_item_id] = row
    return latest


async def build_rma_pdf_data(
    session: AsyncSession,
    *,
    ticket_id: int,
    safety_snapshot: dict[str, Any] | None = None,
    rma_no: str | None = None,
) -> RmaPdfData:
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise RmaPdfError("RMA_TICKET_NOT_FOUND")
    if ticket.current_status_code != "ready_for_export":
        raise RmaPdfError("RMA_TICKET_NOT_ELIGIBLE")
    if ticket.missing_fields or ticket.conflict_fields:
        raise RmaPdfError("RMA_TICKET_HAS_UNRESOLVED_FIELDS")
    required = {
        "customer_name": ticket.customer_name,
        "mailing_address": ticket.mailing_address,
        "contact_person": ticket.contact_person,
        "contact_phone": ticket.contact_phone,
        "contact_email": ticket.contact_email,
        "request_date": ticket.request_date,
    }
    missing = [name for name, value in required.items() if value is None or (isinstance(value, str) and not value.strip())]
    if missing:
        raise RmaPdfError("RMA_REQUIRED_FIELDS_MISSING:" + ",".join(missing))
    items = list(
        (
            await session.execute(
                select(RepairTicketItem)
                .where(RepairTicketItem.ticket_id == ticket.id)
                .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
            )
        ).scalars().all()
    )
    if not 1 <= len(items) <= 300:
        raise RmaPdfError("RMA_ITEM_COUNT_OUT_OF_RANGE")
    _validate_snapshot(ticket, items, safety_snapshot)
    rma_statement = select(TicketRma).where(TicketRma.ticket_id == ticket.id)
    if rma_no:
        rma_statement = rma_statement.where(TicketRma.rma_no == rma_no)
    rma_rows = list((await session.execute(rma_statement)).scalars().all())
    if len(rma_rows) != 1:
        raise RmaPdfError("RMA_NUMBER_NOT_UNIQUE_FOR_TICKET")
    rma_record = rma_rows[0]
    export_rows = list(
        (
            await session.execute(
                select(ExportSap).where(
                    ExportSap.ticket_id == ticket.id,
                    ExportSap.rma_no == rma_record.rma_no,
                    ExportSap.status == "rma_received",
                ).order_by(ExportSap.id)
            )
        ).scalars().all()
    )
    # A ticket may be exported again after a policy or route correction while
    # SAP legitimately keeps the same RMA number.  In that case historical
    # rma_received rows remain as immutable audit evidence.  The PDF must use
    # only the newest accepted row for each ticket item; summing every export
    # row would double-charge the customer.
    export_by_item = _latest_export_rows_by_item(export_rows)
    if len(export_by_item) != len(items):
        raise RmaPdfError("RMA_EXPORT_ITEMS_INCOMPLETE")
    selected_export_rows = list(export_by_item.values())
    currencies = {str(row.currency or "").upper() for row in selected_export_rows}
    if len(currencies) != 1:
        raise RmaPdfError("RMA_CURRENCY_CONFLICT")
    currency = next(iter(currencies))
    total_cost = sum((row.repair_fee or Decimal("0")) for row in selected_export_rows)
    normalized_sns = [(item.sn or "").strip().casefold() for item in items]
    if len(normalized_sns) != len(set(normalized_sns)):
        raise RmaPdfError("RMA_DUPLICATE_SN")
    result_items: list[RmaItemData] = []
    for index, item in enumerate(items, start=1):
        if item.validation_status != "pass":
            raise RmaPdfError("RMA_ITEM_VALIDATION_NOT_PASSED")
        if not item.material_code or not item.material_name or not item.sn:
            raise RmaPdfError("RMA_ITEM_FIELDS_MISSING")
        if item.quantity != 1:
            raise RmaPdfError("RMA_ITEM_QUANTITY_SN_CONFLICT")
        result_items.append(
            RmaItemData(
                no=index,
                part_no=item.material_code,
                part_description=item.material_name,
                quantity=1,
                part_serial_no=item.sn,
                maintenance_price=export_by_item[item.id].repair_fee,
                failure_description=item.failure_description or "",
            )
        )
    resolved_rma_no = rma_record.rma_no
    if not re.fullmatch(r"\d{10}", resolved_rma_no) or not CODE39_PATTERN.fullmatch(resolved_rma_no):
        raise RmaPdfError("RMA_CODE39_VALUE_INVALID")
    try:
        return RmaPdfData(
            rma_no=resolved_rma_no,
            request_date=ticket.request_date,
            currency=currency,
            customer_code=ticket.customer_code,
            customer_name=ticket.customer_name,
            mailing_address=ticket.mailing_address,
            mailing_contact_person=ticket.contact_person,
            mailing_contact_phone=ticket.contact_phone,
            delivery_fee_paid_by_customer=settings.RMA_PDF_DEFAULT_DELIVERY_FEE,
            repair_fee_paid_by_customer=(
                settings.RMA_PDF_DEFAULT_REPAIR_FEE
                if total_cost == 0
                else f"{total_cost:.2f} {currency}"
            ),
            total_cost=total_cost,
            items=result_items,
        )
    except ValueError as exc:
        raise RmaPdfError("RMA_DATA_VALIDATION_FAILED") from exc


def rma_pdf_page_count(item_count: int) -> int:
    if not 1 <= item_count <= 300:
        raise RmaPdfError("RMA_ITEM_COUNT_OUT_OF_RANGE")
    return 2 + math.ceil(max(item_count - 6, 0) / 11)


def _draw_code39_fallback(page: fitz.Page, value: str, rect: tuple[float, float, float, float]) -> None:
    encoded = f"*{value}*"
    wide_ratio = 2.5
    quiet_units = 10.0
    unit_count = quiet_units * 2 + sum(
        sum(wide_ratio if width == "w" else 1.0 for width in CODE39[char]) + 1.0
        for char in encoded
    )
    box = fitz.Rect(*rect)
    unit = box.width / unit_count
    x = box.x0 + quiet_units * unit
    for char in encoded:
        for index, width in enumerate(CODE39[char]):
            element_width = unit * (wide_ratio if width == "w" else 1.0)
            if index % 2 == 0:
                page.draw_rect(
                    fitz.Rect(x, box.y0, x + element_width, box.y1),
                    color=None,
                    fill=(0, 0, 0),
                    overlay=True,
                )
            x += element_width
        x += unit


def _draw_code39(page: fitz.Page, value: str, barcode: dict[str, Any]) -> None:
    if not CODE39_PATTERN.fullmatch(value):
        raise RmaPdfError("RMA_CODE39_VALUE_INVALID")
    rect = tuple(float(number) for number in barcode["rect"])
    try:
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode import createBarcodeDrawing
    except ImportError:
        _draw_code39_fallback(page, value, rect)
        return
    drawing = createBarcodeDrawing(
        "Standard39",
        value=value,
        checksum=False,
        humanReadable=False,
        barHeight=float(barcode["bar_height"]),
        barWidth=float(barcode["bar_width"]),
        quiet=True,
    )
    svg = renderSVG.drawToString(drawing)
    svg_document = fitz.open("svg", svg.encode("utf-8") if isinstance(svg, str) else svg)
    barcode_document = fitz.open("pdf", svg_document.convert_to_pdf())
    try:
        page.show_pdf_page(fitz.Rect(*rect), barcode_document, 0, keep_proportion=False, overlay=True)
    finally:
        barcode_document.close()
        svg_document.close()


def _continuation_footer_resource(template: fitz.Document, table: dict[str, Any]) -> bytes:
    clip = fitz.Rect(*table["footer_clip"])
    scale = float(table.get("footer_dpi", 300)) / 72.0
    pixmap = template[1].get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    return pixmap.tobytes("png")


def _clear_locked_blank_regions(
    document: fitz.Document, layout: dict[str, Any]
) -> None:
    """Remove template placeholder glyphs from fields that must stay empty."""
    for raw in (layout.get("blank_regions") or {}).values():
        page_index = int(raw["page"])
        if page_index < 0 or page_index >= document.page_count:
            raise RmaPdfError("RMA_TEMPLATE_INTEGRITY_FAILED")
        page = document[page_index]
        page.add_redact_annot(fitz.Rect(*raw["rect"]), fill=(1, 1, 1))
        page.apply_redactions(images=0, graphics=0)


def _draw_first_page_extension(page: fitz.Page, row_count: int, table: dict[str, Any]) -> None:
    if not 1 <= row_count <= int(table["first_max_rows"]):
        raise RmaPdfError("RMA_ITEM_COUNT_OUT_OF_RANGE")
    if row_count == 1:
        return
    final_bottom = float(table["first_data_top"]) + row_count * float(table["first_row_height"])
    shape = page.new_shape()
    for x in table["vertical_lines_x"]:
        shape.draw_line(fitz.Point(float(x), float(table["first_base_row_bottom"])), fitz.Point(float(x), final_bottom))
    for row_number in range(2, row_count + 1):
        y = float(table["first_data_top"]) + row_number * float(table["first_row_height"])
        shape.draw_line(fitz.Point(float(table["first_horizontal_left_x"]), y), fitz.Point(float(table["first_horizontal_right_x"]), y))
    shape.finish(width=float(table["line_width"]), color=(0, 0, 0))
    shape.commit(overlay=True)


def _continuation_row_bounds(local_row_index: int, table: dict[str, Any]) -> tuple[float, float]:
    if local_row_index == 0:
        return float(table["continuation_data_top"]), float(table["continuation_first_row_bottom"])
    top = float(table["continuation_first_row_bottom"]) + (local_row_index - 1) * float(table["continuation_row_height"])
    return top, top + float(table["continuation_row_height"])


def _draw_continuation_table(page: fitz.Page, row_count: int, table: dict[str, Any]) -> None:
    if not 1 <= row_count <= int(table["continuation_max_rows"]):
        raise RmaPdfError("RMA_ITEM_COUNT_OUT_OF_RANGE")
    _, final_bottom = _continuation_row_bounds(row_count - 1, table)
    shape = page.new_shape()
    for x in table["vertical_lines_x"]:
        shape.draw_line(fitz.Point(float(x), float(table["continuation_data_top"])), fitz.Point(float(x), final_bottom))
    for row_index in range(row_count):
        _, row_bottom = _continuation_row_bounds(row_index, table)
        shape.draw_line(
            fitz.Point(float(table["continuation_horizontal_left_x"]), row_bottom),
            fitz.Point(float(table["continuation_horizontal_right_x"]), row_bottom),
        )
    shape.finish(width=float(table["line_width"]), color=(0, 0, 0))
    shape.commit(overlay=True)


def _row_values(data: RmaPdfData, item: RmaItemData, row_number: int) -> dict[str, Any]:
    return {
        "item_no": row_number,
        "item_part_no": item.part_no,
        "item_description": item.part_description,
        "item_qty": 1,
        "item_rma_no": data.rma_no,
        "item_serial_no": item.part_serial_no,
        "item_price": format(item.maintenance_price, ".2f") if item.maintenance_price is not None else "",
        "item_program": item.program_running,
        "item_advance": item.advance_replacement,
        "item_failure": item.failure_description,
        "item_delivery": item.delivery,
    }


def _write_row(
    page: fitz.Page,
    writer: _FixedBoxTextWriter,
    specs: dict[str, _FieldSpec],
    values: dict[str, Any],
    *,
    offset: float | None = None,
    continuation_bounds: tuple[float, float] | None = None,
) -> None:
    for field_name in ROW_FIELD_NAMES:
        spec = specs[field_name]
        if offset is not None:
            spec = spec.offset_y(offset)
        elif continuation_bounds is not None:
            x0, _, x1, _ = spec.rect
            spec = replace(spec, rect=(x0, continuation_bounds[0], x1, continuation_bounds[1]))
        writer.write(page, spec, values[field_name])


def render_rma_pdf(
    data: RmaPdfData,
    *,
    template_path: str | Path | None = None,
    layout_path: str | Path | None = None,
    test_only: bool = False,
) -> bytes:
    rma_pdf_page_count(len(data.items))
    validate_rma_template_integrity(template_path, layout_path=layout_path)
    layout = _load_layout(layout_path)
    template = fitz.open(Path(template_path or settings.RMA_PDF_TEMPLATE_PATH))
    document = fitz.open()
    try:
        table = layout["table"]
        first_items = data.items[: int(table["first_max_rows"])]
        remaining = data.items[int(table["first_max_rows"]):]
        continuation_size = int(table["continuation_max_rows"])
        continuation_chunks = [remaining[index:index + continuation_size] for index in range(0, len(remaining), continuation_size)]
        document.insert_pdf(template, from_page=0, to_page=0)
        footer_resource = _continuation_footer_resource(template, table) if continuation_chunks else None
        for _ in continuation_chunks:
            page = document.new_page(width=float(layout["page_width"]), height=float(layout["page_height"]))
            page.insert_image(fitz.Rect(*table["footer_clip"]), stream=footer_resource, keep_proportion=False, overlay=True)
        document.insert_pdf(template, from_page=1, to_page=1)
        if document.page_count != rma_pdf_page_count(len(data.items)):
            raise RmaPdfError("RMA_PAGINATION_FAILED")
        _clear_locked_blank_regions(document, layout)

        writer = _FixedBoxTextWriter(_subset_cjk_font(_resolve_cjk_font_path(), _dynamic_text(data)))
        for page in document:
            writer.register(page)
        specs = _field_specs(layout)
        first_page = document[0]
        _draw_code39(first_page, data.rma_no, layout["barcode"])
        writer.write(first_page, specs["request_date"], f"{data.request_date.year}/{data.request_date.month}/{data.request_date.day}")
        writer.write(first_page, specs["currency"], data.currency)
        _draw_first_page_extension(first_page, len(first_items), table)
        row_number = 1
        for local_index, item in enumerate(first_items):
            _write_row(
                first_page,
                writer,
                specs,
                _row_values(data, item, row_number),
                offset=local_index * float(table["first_row_height"]),
            )
            row_number += 1
        for page_index, chunk in enumerate(continuation_chunks, start=1):
            page = document[page_index]
            _draw_continuation_table(page, len(chunk), table)
            for local_index, item in enumerate(chunk):
                _write_row(
                    page,
                    writer,
                    specs,
                    _row_values(data, item, row_number),
                    continuation_bounds=_continuation_row_bounds(local_index, table),
                )
                row_number += 1

        details_page = document[-1]
        detail_values = {
            "customer_name": data.customer_name,
            "mailing_address": data.mailing_address,
            "mailing_contact": data.mailing_contact,
            "delivery_fee": data.delivery_fee_paid_by_customer,
            "repair_fee": data.repair_fee_paid_by_customer,
            "total_cost": format(data.total_cost.normalize(), "f"),
        }
        for field_name, value in detail_values.items():
            writer.write(details_page, specs[field_name], value)
        # RMA artifacts must match the business template in every environment.
        # Test safety is enforced by the mail envelope/subject gate, never by
        # modifying the customer-facing PDF.
        del test_only
        result = document.tobytes(garbage=4, deflate=True, clean=True)
        if len(result) > settings.RMA_PDF_MAX_BYTES:
            raise RmaPdfError("RMA_PDF_TOO_LARGE")
        return result
    except RmaPdfError:
        raise
    except Exception as exc:
        raise RmaPdfError("RMA_PDF_RENDER_FAILED") from exc
    finally:
        document.close()
        template.close()


def rma_pdf_file_name(data: RmaPdfData) -> str:
    customer = re.sub(r"[\\/:*?\"<>|]+", "_", data.customer_name).strip(" ._") or "customer"
    return f"RMA{data.rma_no}{customer[:80]}.pdf"


def rma_pdf_snapshot(
    data: RmaPdfData,
    *,
    pdf_content: bytes | None = None,
    oss_object_id: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "template_version": TEMPLATE_VERSION,
        "template_sha256": TEMPLATE_SHA256,
        "data": data.model_dump(mode="json"),
    }
    if pdf_content is not None:
        result["pdf_sha256"] = hashlib.sha256(pdf_content).hexdigest()
    if oss_object_id is not None:
        result["oss_object_id"] = oss_object_id
    return result


def normalize_rma_template_version(value: str | None) -> str | None:
    if value in LEGACY_TEMPLATE_VERSIONS:
        return TEMPLATE_VERSION
    return value
