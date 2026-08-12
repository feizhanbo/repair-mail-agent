from decimal import Decimal

from app.integrations.sap_middleware import ExternalSnRecord
from app.services.sap_sn_sync import assess_sn_snapshot, snapshot_count_change_percent


def _record(sn: str, customer: str = "CM1", material: str = "MAT1") -> ExternalSnRecord:
    return ExternalSnRecord(
        sn=sn,
        customer_code=customer,
        customer_name="Customer",
        material_code=material,
    )


def test_snapshot_rejects_duplicates_and_required_field_gaps() -> None:
    result = assess_sn_snapshot([_record("SN1"), _record("SN1"), _record("SN2", customer="")])
    assert result["duplicate_sns"] == {"SN1"}
    assert result["duplicate_count"] == 1
    assert [row.sn for row in result["invalid"]] == ["SN2"]
    assert result["valid_count"] == 1


def test_snapshot_count_change_guard_uses_absolute_five_percent_boundary() -> None:
    assert snapshot_count_change_percent(None, 100) is None
    assert snapshot_count_change_percent(100, 105) == Decimal("5.0000")
    assert snapshot_count_change_percent(100, 94) == Decimal("6.0000")
