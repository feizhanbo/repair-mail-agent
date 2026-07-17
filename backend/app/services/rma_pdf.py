from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import fitz
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import RepairTicket, RepairTicketItem


TEMPLATE_VERSION = "rma_authorization_v1"
LEGACY_TEMPLATE_VERSIONS = {"v1", "rma_authorization_zh_v1"}
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


class RmaPdfError(ValueError):
    pass


class RmaItemData(BaseModel):
    no: int = 1
    part_no: str
    part_description: str
    quantity: int = 1
    part_serial_no: str
    maintenance_price: Decimal | None = None
    program_running: str = ""
    advance_replacement: str = "N"
    failure_description: str = ""
    delivery: str = "2 WEEKS"


class RmaPdfData(BaseModel):
    rma_no: str = Field(min_length=1, max_length=30)
    request_date: date
    currency: str = ""
    customer_code: str
    customer_name: str
    mailing_address: str
    mailing_contact_person: str
    mailing_contact_phone: str
    delivery_fee_paid_by_customer: str
    repair_fee_paid_by_customer: str
    total_cost: Decimal = Decimal("0")
    items: list[RmaItemData]


FIELD_RECTS = {
    "request_date": (552.0, 241.0, 654.0, 259.0),
    "currency": (552.0, 279.0, 654.0, 298.0),
    "item.no": (18.8, 360.2, 42.0, 389.5),
    "item.part_no": (43.0, 360.2, 86.0, 389.5),
    "item.description": (87.0, 360.2, 239.0, 389.5),
    "item.qty": (240.0, 360.2, 270.0, 389.5),
    "item.rma_no": (271.0, 360.2, 321.0, 389.5),
    "item.serial_no": (322.0, 360.2, 398.0, 389.5),
    "item.price": (399.0, 360.2, 489.0, 389.5),
    "item.program": (490.0, 360.2, 577.0, 389.5),
    "item.advance": (578.0, 360.2, 688.0, 389.5),
    "item.failure": (689.0, 360.2, 783.0, 389.5),
    "item.delivery": (784.0, 360.2, 832.0, 389.5),
    "customer_name": (181.0, 17.0, 433.0, 45.0),
    "mailing_address": (181.0, 77.0, 433.0, 106.0),
    "mailing_contact": (615.0, 77.0, 811.0, 106.0),
    "delivery_fee": (205.0, 180.0, 376.0, 207.0),
    "repair_fee": (205.0, 216.0, 376.0, 243.0),
    "total_cost": (205.0, 252.0, 376.0, 279.0),
}


def _authorization_no(ticket_no: str) -> str:
    value = ticket_no.strip().upper()
    return value[3:] if value.startswith("RMA") and len(value) > 3 else value


async def build_rma_pdf_data(session: AsyncSession, *, ticket_id: int) -> RmaPdfData:
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise RmaPdfError("RMA_TICKET_NOT_FOUND")
    if ticket.current_status_code != "ready_for_export":
        raise RmaPdfError("RMA_TICKET_NOT_ELIGIBLE")
    if ticket.missing_fields or ticket.conflict_fields:
        raise RmaPdfError("RMA_TICKET_HAS_UNRESOLVED_FIELDS")
    required = {
        "customer_code": ticket.customer_code, "customer_name": ticket.customer_name,
        "mailing_address": ticket.mailing_address, "contact_person": ticket.contact_person,
        "contact_phone": ticket.contact_phone, "contact_email": ticket.contact_email,
        "request_date": ticket.request_date,
    }
    missing = [name for name, value in required.items() if value is None or (isinstance(value, str) and not value.strip())]
    if missing:
        raise RmaPdfError("RMA_REQUIRED_FIELDS_MISSING:" + ",".join(missing))
    items = (
        await session.execute(
            select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id).order_by(RepairTicketItem.line_no)
        )
    ).scalars().all()
    if len(items) != 1:
        raise RmaPdfError("RMA_TEMPLATE_V1_REQUIRES_ONE_ITEM")
    item = items[0]
    if item.validation_status != "pass":
        raise RmaPdfError("RMA_ITEM_VALIDATION_NOT_PASSED")
    if not item.material_code or not item.material_name or not item.sn:
        raise RmaPdfError("RMA_ITEM_FIELDS_MISSING")
    rma_no = _authorization_no(ticket.ticket_no)
    if not CODE39_PATTERN.fullmatch(rma_no):
        raise RmaPdfError("RMA_CODE39_VALUE_INVALID")
    return RmaPdfData(
        rma_no=rma_no, request_date=ticket.request_date, currency=settings.RMA_PDF_DEFAULT_CURRENCY,
        customer_code=ticket.customer_code, customer_name=ticket.customer_name,
        mailing_address=ticket.mailing_address, mailing_contact_person=ticket.contact_person,
        mailing_contact_phone=ticket.contact_phone,
        delivery_fee_paid_by_customer=settings.RMA_PDF_DEFAULT_DELIVERY_FEE,
        repair_fee_paid_by_customer=settings.RMA_PDF_DEFAULT_REPAIR_FEE,
        total_cost=Decimal(settings.RMA_PDF_DEFAULT_TOTAL_COST),
        items=[RmaItemData(
            no=item.line_no, part_no=item.material_code, part_description=item.material_name,
            quantity=item.quantity, part_serial_no=item.sn,
            failure_description=item.failure_description or "",
        )],
    )


def _write_text(page: fitz.Page, rect: tuple[float, float, float, float], value: Any, *, font_size: float, min_font_size: float = 5.5, align: int = fitz.TEXT_ALIGN_CENTER) -> None:
    text = str(value or "")
    size = font_size
    while size >= min_font_size:
        result = page.insert_textbox(fitz.Rect(*rect), text, fontname="china-s", fontsize=size, align=align, color=(0, 0, 0), overlay=True)
        if result >= 0:
            return
        size -= 0.5
    raise RmaPdfError(f"RMA_FIELD_OVERFLOW:{text[:40]}")


def _draw_code39(page: fitz.Page, value: str, rect: tuple[float, float, float, float]) -> None:
    encoded = f"*{value}*"
    wide_ratio = 2.5
    unit_count = sum(sum(wide_ratio if width == "w" else 1.0 for width in CODE39[char]) + 1.0 for char in encoded)
    box = fitz.Rect(*rect)
    unit = box.width / unit_count
    x = box.x0
    for char in encoded:
        for index, width in enumerate(CODE39[char]):
            element_width = unit * (wide_ratio if width == "w" else 1.0)
            if index % 2 == 0:
                page.draw_rect(fitz.Rect(x, box.y0, x + element_width, box.y1), color=None, fill=(0, 0, 0), overlay=True)
            x += element_width
        x += unit


def render_rma_pdf(
    data: RmaPdfData,
    *,
    template_path: str | Path | None = None,
    test_only: bool = False,
) -> bytes:
    if len(data.items) != 1:
        raise RmaPdfError("RMA_TEMPLATE_V1_REQUIRES_ONE_ITEM")
    path = Path(template_path or settings.RMA_PDF_TEMPLATE_PATH)
    if not path.is_file():
        raise RmaPdfError("RMA_TEMPLATE_NOT_FOUND")
    document = fitz.open(path)
    try:
        if document.page_count != 2 or tuple(document[0].rect)[2:] != (842.0, 595.0):
            raise RmaPdfError("RMA_TEMPLATE_LAYOUT_INVALID")
        page1, page2 = document[0], document[1]
        _draw_code39(page1, data.rma_no, (362.0, 23.0, 694.0, 59.0))
        _write_text(page1, FIELD_RECTS["request_date"], f"{data.request_date.year}/{data.request_date.month}/{data.request_date.day}", font_size=10.5)
        _write_text(page1, FIELD_RECTS["currency"], data.currency, font_size=10.5)
        item = data.items[0]
        values = {
            "item.no": item.no, "item.part_no": item.part_no, "item.description": item.part_description,
            "item.qty": item.quantity, "item.rma_no": data.rma_no, "item.serial_no": item.part_serial_no,
            "item.price": format(item.maintenance_price, "f") if item.maintenance_price is not None else "", "item.program": item.program_running,
            "item.advance": item.advance_replacement, "item.failure": item.failure_description,
            "item.delivery": item.delivery,
        }
        for name, value in values.items():
            _write_text(page1, FIELD_RECTS[name], value, font_size=7.5, min_font_size=4.5)
        _write_text(page2, FIELD_RECTS["customer_name"], data.customer_name, font_size=10.0)
        _write_text(page2, FIELD_RECTS["mailing_address"], data.mailing_address, font_size=9.5, min_font_size=6.0)
        _write_text(page2, FIELD_RECTS["mailing_contact"], f"{data.mailing_contact_person} / {data.mailing_contact_phone}", font_size=9.5, min_font_size=6.0)
        _write_text(page2, FIELD_RECTS["delivery_fee"], data.delivery_fee_paid_by_customer, font_size=10.5, align=fitz.TEXT_ALIGN_LEFT)
        _write_text(page2, FIELD_RECTS["repair_fee"], data.repair_fee_paid_by_customer, font_size=10.5, align=fitz.TEXT_ALIGN_LEFT)
        _write_text(page2, FIELD_RECTS["total_cost"], format(data.total_cost, "f"), font_size=10.5, align=fitz.TEXT_ALIGN_LEFT)
        if test_only:
            for page in (page1, page2):
                page.insert_textbox(
                    page.rect,
                    "TEST ONLY / 测试数据",
                    fontname="china-s",
                    fontsize=42,
                    color=(0.85, 0.15, 0.15),
                    align=fitz.TEXT_ALIGN_CENTER,
                    rotate=0,
                    fill_opacity=0.22,
                    overlay=True,
                )
        return document.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        document.close()


def rma_pdf_file_name(data: RmaPdfData) -> str:
    customer = re.sub(r"[\\/:*?\"<>|]+", "_", data.customer_name).strip(" ._") or "customer"
    return f"RMA{data.rma_no}_{customer[:80]}.pdf"


def rma_pdf_snapshot(data: RmaPdfData) -> dict[str, Any]:
    return {"template_version": TEMPLATE_VERSION, "data": data.model_dump(mode="json")}


def normalize_rma_template_version(value: str | None) -> str | None:
    if value in LEGACY_TEMPLATE_VERSIONS:
        return TEMPLATE_VERSION
    return value
