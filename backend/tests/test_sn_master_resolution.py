from datetime import date

from app.models import SnAsset
from app.services.sn_master_resolution import (
    LATEST_DATE,
    LATEST_DATE_AND_A_PREFERRED,
    MASTER_DATA_AMBIGUOUS,
    MASTER_DATA_UNRESOLVED,
    RESOLVED,
    resolve_assets,
)


def _asset(
    row_id: int,
    ins_id: int,
    sn: str,
    material: str,
    expiry: str | None,
    *,
    customer: str = "CM00178",
    status: str = "valid",
) -> SnAsset:
    return SnAsset(
        id=row_id,
        ins_id=ins_id,
        sn=sn,
        customer_code=customer,
        customer_name="Customer",
        material_code=material,
        material_name=material,
        warranty_end_date=date.fromisoformat(expiry) if expiry else None,
        asset_status=status,
        source_system="sqlserver",
    )


def test_real_sample_m81222005100008_selects_latest_a_board() -> None:
    sn = "M81222005100008"
    rows = [
        _asset(1, 135078, sn, "Z.SM.8122V310AB", "2022-07-06"),
        _asset(2, 135079, sn, "Z.SM.8122V310A", "2022-07-06"),
        _asset(3, 135080, sn, "B.SM.8122V310", "2022-07-06"),
        _asset(4, 160265, sn, "Z.SM.8122V400AB", "2021-12-14", customer="CM00366"),
        _asset(5, 160266, sn, "B.SM.8122V400", "2021-12-14", customer="CM00366"),
    ]

    result = resolve_assets(sn, reversed(rows))

    assert result.status == RESOLVED
    assert result.method == LATEST_DATE_AND_A_PREFERRED
    assert result.asset is not None
    assert result.asset.ins_id == 135079
    assert result.asset.customer_code == "CM00178"
    assert result.asset.material_code == "Z.SM.8122V310A"


def test_known_single_a_samples_resolve() -> None:
    cases = [
        ("M81222005100001", 132540, "CM01290", "2022-06-16"),
        ("M81222005100002", 129980, "CM00131", "2022-06-07"),
    ]
    for sn, ins_id, customer, expiry in cases:
        rows = [
            _asset(1, ins_id, sn, "Z.SM.8122V310A", expiry, customer=customer),
            _asset(2, ins_id + 1, sn, "B.SM.8122V310", expiry, customer=customer),
        ]
        result = resolve_assets(sn, rows)
        assert result.status == RESOLVED
        assert result.asset is not None and result.asset.ins_id == ins_id


def test_null_dates_and_latest_only_semi_finished_are_unresolved() -> None:
    sn = "SN0001"
    nulls = [_asset(1, 1, sn, "Z.SM.XA", None)]
    semi_latest = [
        _asset(1, 1, sn, "Z.SM.XA", "2025-01-01"),
        _asset(2, 2, sn, "B.SM.X", "2026-01-01"),
    ]
    assert resolve_assets(sn, nulls).status == MASTER_DATA_UNRESOLVED
    result = resolve_assets(sn, semi_latest)
    assert result.status == MASTER_DATA_UNRESOLVED
    assert result.reason == "LATEST_DATE_ONLY_SEMI_FINISHED"


def test_multiple_a_and_multiple_ab_are_ambiguous() -> None:
    sn = "SN0002"
    multiple_a = [
        _asset(1, 1, sn, "Z.SM.XA", "2026-01-01"),
        _asset(2, 2, sn, "Z.SM.YA", "2026-01-01"),
    ]
    multiple_ab = [
        _asset(3, 3, sn, "Z.SM.XAB", "2026-01-01"),
        _asset(4, 4, sn, "Z.SM.YAB", "2026-01-01"),
    ]
    assert resolve_assets(sn, multiple_a).status == MASTER_DATA_AMBIGUOUS
    assert resolve_assets(sn, multiple_ab).status == MASTER_DATA_AMBIGUOUS


def test_single_ab_resolves_and_invalid_rows_do_not_participate() -> None:
    sn = "SN0003"
    rows = [
        _asset(1, 1, sn, "Z.SM.XAB", "2026-01-01"),
        _asset(2, 2, sn, "Z.SM.YA", "2027-01-01", status="invalid"),
    ]
    result = resolve_assets(sn, rows)
    assert result.status == RESOLVED
    assert result.method == LATEST_DATE
    assert result.asset is not None and result.asset.ins_id == 1


def test_only_invalid_rows_keep_sn_known_but_resolution_unresolved() -> None:
    sn = "SN0004"
    result = resolve_assets(
        sn, [_asset(1, 1, sn, "Z.SM.XA", "2026-01-01", status="invalid")]
    )
    assert result.status == MASTER_DATA_UNRESOLVED
    assert len(result.all_records) == 1
