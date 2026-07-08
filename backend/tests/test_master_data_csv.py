from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services import master_data as master_data_service


def test_parse_sn_assets_csv_accepts_utf8_bom() -> None:
    content = (
        "\ufeffsn,customer_code,customer_name,material_code,material_name,asset_status,warranty_start_date,warranty_end_date\n"
        "SN001,C001,Acme,MAT001,Main board,valid,2026-01-01,2027-01-01\n"
    ).encode("utf-8")

    items, file_hash = master_data_service.parse_sn_assets_csv(content)

    assert len(items) == 1
    assert items[0].sn == "SN001"
    assert items[0].customer_name == "Acme"
    assert items[0].warranty_start_date.isoformat() == "2026-01-01"
    assert len(file_hash) == 64


def test_parse_sn_assets_csv_reports_invalid_rows() -> None:
    content = (
        "sn,customer_code,customer_name,material_code,material_name,asset_status,warranty_start_date,warranty_end_date\n"
        "SN001,C001,Acme,MAT001,Main board,valid,not-a-date,2027-01-01\n"
    ).encode("utf-8")

    with pytest.raises(HTTPException) as exc_info:
        master_data_service.parse_sn_assets_csv(content)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "CSV_VALIDATION_FAILED"
    assert exc_info.value.detail["errors"][0]["row"] == 2


def test_parse_board_cards_csv_parses_boolean_values() -> None:
    content = (
        "material_code,material_name,need_ship_to_beijing,shipping_address,shipping_contact,shipping_phone,postal_code,status\n"
        "MAT001,Main board,true,Beijing,Alice,010-00000000,100000,active\n"
    ).encode("utf-8")

    items, file_hash = master_data_service.parse_board_cards_csv(content)

    assert len(items) == 1
    assert items[0].material_code == "MAT001"
    assert items[0].need_ship_to_beijing is True
    assert len(file_hash) == 64


def test_csv_template_downloads_include_bom() -> None:
    assert master_data_service.sn_assets_template_csv().startswith(b"\xef\xbb\xbf")
    assert master_data_service.board_cards_template_csv().startswith(b"\xef\xbb\xbf")


def test_xlsx_templates_are_parseable() -> None:
    sn_items, sn_hash = master_data_service.parse_sn_assets_xlsx(master_data_service.sn_assets_template_xlsx())
    board_items, board_hash = master_data_service.parse_board_cards_xlsx(master_data_service.board_cards_template_xlsx())

    assert sn_items[0].sn == "SN202607070001"
    assert board_items[0].material_code == "MAT001"
    assert len(sn_hash) == 64
    assert len(board_hash) == 64
