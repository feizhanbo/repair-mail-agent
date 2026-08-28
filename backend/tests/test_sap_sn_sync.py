from decimal import Decimal

from app.integrations.sap_middleware import ExternalSnRecord
from app.services.sap_sn_sync import assess_sn_snapshot, snapshot_count_change_percent


def _record(
    sn: str,
    customer: str = "CM1",
    material: str = "MAT1",
    warranty: str | None = None,
) -> ExternalSnRecord:
    return ExternalSnRecord(
        sn=sn,
        ins_id=1,
        customer_code=customer,
        customer_name="Customer",
        material_code=material,
        values={"warranty_end_date": warranty},
    )


def test_snapshot_rejects_duplicates_and_required_field_gaps() -> None:
    result = assess_sn_snapshot([_record("SN1"), _record("SN1"), _record("SN2", customer="")])
    # SN1 keeps its first (authoritative) row instead of being fully discarded;
    # SN2 is invalid because customer is missing.
    assert result["duplicate_sns"] == {"SN1"}
    assert result["duplicate_count"] == 1
    assert [row.sn for row in result["invalid"]] == ["SN2"]
    assert result["valid_count"] == 1
    assert [row.sn for row in result["resolved"]] == ["SN1"]


def test_snapshot_picks_latest_warranty_row() -> None:
    result = assess_sn_snapshot(
        [
            _record("SN1", warranty="2026-01-01"),
            _record("SN1", warranty="2027-06-30"),
            _record("SN1", warranty="2026-12-31"),
        ]
    )
    assert result["valid_count"] == 1
    assert result["resolved"][0].values["warranty_end_date"] == "2027-06-30"
    assert result["duplicate_sns"] == {"SN1"}
    assert result["duplicate_count"] == 2


def test_snapshot_nulls_fallback_to_first_row() -> None:
    first = _record("SN1", customer="CM_A")
    second = _record("SN1", customer="CM_B")
    result = assess_sn_snapshot([first, second])
    assert result["resolved"] == [first]
    assert result["valid_count"] == 1
    assert result["duplicate_count"] == 1


def test_snapshot_mixed_invalid_and_duplicate() -> None:
    result = assess_sn_snapshot(
        [
            _record("SN1", warranty="2026-01-01"),
            _record("SN1", warranty="2027-06-30"),
            _record("SN2", customer=""),
        ]
    )
    assert [row.sn for row in result["invalid"]] == ["SN2"]
    assert [row.sn for row in result["resolved"]] == ["SN1"]
    assert result["duplicate_sns"] == {"SN1"}
    assert result["valid_count"] == 1


def test_snapshot_duplicate_count_is_dropped_rows() -> None:
    result = assess_sn_snapshot([_record("SN1"), _record("SN1"), _record("SN1")])
    assert result["duplicate_count"] == 2
    assert result["valid_count"] == 1
    assert result["duplicate_sns"] == {"SN1"}


def test_snapshot_count_change_guard_uses_absolute_five_percent_boundary() -> None:
    assert snapshot_count_change_percent(None, 100) is None
    assert snapshot_count_change_percent(100, 105) == Decimal("5.0000")
    assert snapshot_count_change_percent(100, 94) == Decimal("6.0000")
