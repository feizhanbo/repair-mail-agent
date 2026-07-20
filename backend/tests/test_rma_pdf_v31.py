from __future__ import annotations

from datetime import date
from decimal import Decimal

import fitz
import pytest

from app.services.rma_pdf import (
    TEMPLATE_SHA256,
    TEMPLATE_VERSION,
    RmaItemData,
    RmaPdfData,
    RmaPdfError,
    render_rma_pdf,
    rma_pdf_page_count,
    rma_pdf_snapshot,
    validate_rma_template_integrity,
)


_CODE39_DECODE = {
    "nnnwwnwnn": "0", "wnnwnnnnw": "1", "nnwwnnnnw": "2", "wnwwnnnnn": "3",
    "nnnwwnnnw": "4", "wnnwwnnnn": "5", "nnwwwnnnn": "6", "nnnwnnwnw": "7",
    "wnnwnnwnn": "8", "nnwwnnwnn": "9", "nwnnwnwnn": "*",
}


def _data(item_count: int, *, test_long_text: bool = False) -> RmaPdfData:
    description = "W" * 160 if test_long_text else "Synthetic part"
    return RmaPdfData(
        rma_no="2026071012",
        request_date=date(2026, 7, 10),
        currency="CNY",
        customer_code="SYNTHETIC",
        customer_name="Synthetic Customer",
        mailing_address="No real shipment - synthetic address",
        mailing_contact_person="Test Operator",
        mailing_contact_phone="000-0000",
        delivery_fee_paid_by_customer="TEST ONLY",
        repair_fee_paid_by_customer="TEST ONLY",
        total_cost=Decimal("0"),
        items=[
            RmaItemData(
                part_no=f"P{index:03}",
                part_description=description,
                part_serial_no=f"SYNTH{index:04}",
                failure_description="Synthetic failure",
            )
            for index in range(1, item_count + 1)
        ],
    )


@pytest.mark.parametrize(
    ("item_count", "page_count"),
    [(1, 2), (6, 2), (7, 3), (17, 3), (18, 4), (28, 4), (29, 5), (300, 29)],
)
def test_dynamic_page_count_and_last_details_page(item_count: int, page_count: int) -> None:
    pdf = render_rma_pdf(_data(item_count))
    with fitz.open(stream=pdf, filetype="pdf") as document:
        assert document.page_count == page_count == rma_pdf_page_count(item_count)
        assert "Customer Name" in document[-1].get_text()
        assert all("Customer Name" not in page.get_text() for page in document[1:-1])
        compact_text = "".join(page.get_text() for page in document).replace(" ", "").replace("\n", "")
        assert "SYNTH0001" in compact_text
        assert f"SYNTH{item_count:04}" in compact_text
        assert "0.00" not in compact_text


@pytest.mark.parametrize("item_count", [0, 301])
def test_item_count_outside_supported_range_is_rejected(item_count: int) -> None:
    with pytest.raises(RmaPdfError, match="RMA_ITEM_COUNT_OUT_OF_RANGE"):
        rma_pdf_page_count(item_count)


def test_duplicate_sn_is_case_insensitive_and_quantity_must_equal_one() -> None:
    values = _data(2).model_dump()
    values["items"][1]["part_serial_no"] = values["items"][0]["part_serial_no"].lower()
    with pytest.raises(ValueError, match="RMA_DUPLICATE_SN"):
        RmaPdfData.model_validate(values)

    values = _data(1).model_dump()
    values["items"][0]["quantity"] = 2
    with pytest.raises(ValueError, match="RMA_ITEM_QUANTITY_SN_CONFLICT"):
        RmaPdfData.model_validate(values)


def test_continuation_has_only_real_rows_and_no_hidden_details_labels() -> None:
    pdf = render_rma_pdf(_data(7))
    with fitz.open(stream=pdf, filetype="pdf") as document:
        continuation = document[1].get_text()
        compact = continuation.replace(" ", "").replace("\n", "")
        assert "SYNTH0007" in compact
        for forbidden in ("Customer Name", "Mailing Add", "Delivery fee", "Total cost"):
            assert forbidden not in continuation


def test_test_only_watermark_is_present_on_every_page() -> None:
    pdf = render_rma_pdf(_data(7), test_only=True)
    with fitz.open(stream=pdf, filetype="pdf") as document:
        assert all("TESTONLY" in "".join(page.get_text().split()) for page in document)


def test_fixed_box_overflow_aborts_generation() -> None:
    with pytest.raises(RmaPdfError, match="RMA_FIELD_OVERFLOW"):
        render_rma_pdf(_data(1, test_long_text=True))


def test_realistic_dotted_material_code_wraps_inside_part_number_cell() -> None:
    data = _data(1)
    data.rma_no = "20260720085727251439"
    data.items[0].part_no = "Z.SM.8123V120A"

    with fitz.open(stream=render_rma_pdf(data), filetype="pdf") as document:
        compact_text = "".join(page.get_text() for page in document).replace(" ", "").replace("\n", "")

    assert "Z.SM.8123V120A" in compact_text
    assert "20260720085727251439" in compact_text


def test_template_integrity_and_snapshot_record_hashes() -> None:
    health = validate_rma_template_integrity()
    assert health["template_version"] == TEMPLATE_VERSION
    assert health["template_sha256"] == TEMPLATE_SHA256

    snapshot = rma_pdf_snapshot(_data(1), pdf_content=b"synthetic-pdf", oss_object_id=17)
    assert snapshot["template_version"] == TEMPLATE_VERSION
    assert snapshot["template_sha256"] == TEMPLATE_SHA256
    assert snapshot["oss_object_id"] == 17
    assert len(snapshot["pdf_sha256"]) == 64


def test_independent_raster_code39_decoder_reads_document_rma_and_quiet_zones() -> None:
    data = _data(1)
    document = fitz.open(stream=render_rma_pdf(data), filetype="pdf")
    scale = 4
    pixmap = document[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    y = int(40 * scale)
    bits: list[bool] = []
    for x in range(int(362.1 * scale), int(693.876 * scale)):
        offset = (y * pixmap.width + x) * pixmap.n
        bits.append(sum(pixmap.samples[offset:offset + 3]) < 300)
    leading_quiet = bits.index(True)
    trailing_quiet = len(bits) - 1 - max(index for index, value in enumerate(bits) if value)

    runs: list[tuple[bool, int]] = []
    current = bits[0]
    length = 0
    for value in bits:
        if value == current:
            length += 1
        else:
            runs.append((current, length))
            current = value
            length = 1
    runs.append((current, length))
    while runs and not runs[0][0]:
        runs.pop(0)
    while runs and not runs[-1][0]:
        runs.pop()

    assert (len(runs) + 1) % 10 == 0
    character_count = (len(runs) + 1) // 10
    element_widths = [width for index in range(character_count) for _, width in runs[index * 10:index * 10 + 9]]
    narrow_width = sorted(element_widths)[len(element_widths) // 2]
    threshold = narrow_width * 1.5
    decoded = ""
    for index in range(character_count):
        pattern = "".join("w" if width > threshold else "n" for _, width in runs[index * 10:index * 10 + 9])
        decoded += _CODE39_DECODE[pattern]

    assert decoded == f"*{data.rma_no}*"
    assert leading_quiet >= narrow_width * 8
    assert trailing_quiet >= narrow_width * 8
