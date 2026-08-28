from __future__ import annotations

from tools.test_relay_server import RelayBatch, RelayControl, RelayRecord, TestRelayStore


def test_call_ids_are_unique_across_relay_database_instances(tmp_path) -> None:
    first = TestRelayStore(tmp_path / "batch-a.sqlite3")
    second = TestRelayStore(tmp_path / "batch-b.sqlite3")
    payload = RelayRecord(
        RequestID="11111111-1111-4111-8111-111111111111",
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
        "RequestID": "11111111-1111-4111-8111-111111111111",
        "remote_record_key": first_result["remote_record_key"],
        "idempotent_reuse": True,
    }


def test_source_request_batch_is_idempotent_and_queryable(tmp_path) -> None:
    store = TestRelayStore(tmp_path / "source-request.sqlite3")
    batch = RelayBatch(
        items=[
            RelayRecord(
                RequestID="11111111-1111-4111-8111-111111111111",
                ticket_id=43,
                ticket_item_id=85,
                sn="SN-1",
            ),
            RelayRecord(
                RequestID="22222222-2222-4222-8222-222222222222",
                ticket_id=43,
                ticket_item_id=86,
                sn="SN-2",
            ),
        ]
    )
    first = store.create_batch(batch)
    second = store.create_batch(batch)
    rows = store.query([str(item.request_id) for item in batch.items])
    assert first["status"] == "succeeded"
    assert all(item["idempotent_reuse"] is False for item in first["items"])
    assert all(item["idempotent_reuse"] is True for item in second["items"])
    assert len(rows) == 2
    assert len({row["rma_no"] for row in rows}) == 1


def test_default_control_can_lock_a_gold_rma_before_submission(tmp_path) -> None:
    store = TestRelayStore(tmp_path / "gold-fixed.sqlite3")
    store.configure(RelayControl(scenario="normal", rma_no="2026081201"))
    store.create(
        RelayRecord(
            RequestID="33333333-3333-4333-8333-333333333333",
            ticket_id=88,
            ticket_item_id=1,
            sn="SN-GOLD-1",
        )
    )
    store.create(
        RelayRecord(
            RequestID="44444444-4444-4444-8444-444444444444",
            ticket_id=88,
            ticket_item_id=2,
            sn="SN-GOLD-2",
        )
    )
    assert {
        row["rma_no"]
        for row in store.query(["33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"])
    } == {"2026081201"}
