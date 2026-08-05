from __future__ import annotations

from tools.test_relay_server import RelayRecord, TestRelayStore


def test_call_ids_are_unique_across_relay_database_instances(tmp_path) -> None:
    first = TestRelayStore(tmp_path / "batch-a.sqlite3")
    second = TestRelayStore(tmp_path / "batch-b.sqlite3")
    payload = RelayRecord(
        submission_key="submission-key-0001",
        ticket_id=43,
        ticket_item_id=85,
        sn="M81072420200031",
    )

    first_result = first.create(payload)
    second_result = second.create(payload)

    assert first_result["remote_record_key"].startswith("TESTCALL-")
    assert second_result["remote_record_key"].startswith("TESTCALL-")
    assert first_result["remote_record_key"] != second_result["remote_record_key"]
    assert first.create(payload) == {
        "status": "succeeded",
        "remote_record_key": first_result["remote_record_key"],
        "idempotent_reuse": True,
    }
