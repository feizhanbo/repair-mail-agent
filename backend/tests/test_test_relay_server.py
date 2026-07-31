from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.services.external_relay import relay_configuration_status
from tools.test_relay_server import create_app


TOKEN = "test-token-that-is-long-enough"


def test_test_http_adapter_requires_every_safety_gate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_ADAPTER", "test_http")
    monkeypatch.setattr(settings, "APP_ENV", "test")
    monkeypatch.setattr(settings, "RUN_REAL_MAIL_INTEGRATION_TESTS", True)
    monkeypatch.setattr(settings, "TEST_RELAY_BASE_URL", "http://127.0.0.1:18765")
    monkeypatch.setattr(settings, "TEST_RELAY_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "IMAP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_USER", "rmatest1@accotest.com")
    monkeypatch.setattr(settings, "SMTP_RECIPIENT_WHITELIST", ["rmatest2@accotest.com"])
    monkeypatch.setattr(settings, "IMAP_HOST", "imap.example.test")
    monkeypatch.setattr(settings, "IMAP_PASSWORD", "configured")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "configured")
    monkeypatch.setattr(settings, "SMTP_PORT", 465)
    assert relay_configuration_status()["configured"] is True

    monkeypatch.setattr(settings, "TEST_RELAY_BASE_URL", "https://relay.example.com")
    status = relay_configuration_status()
    assert status["configured"] is False
    assert "TEST_RELAY_LOOPBACK_URL_REQUIRED" in status["missing"]


def test_test_http_adapter_is_forbidden_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RELAY_SQLSERVER_ENABLED", True)
    monkeypatch.setattr(settings, "RELAY_ADAPTER", "test_http")
    monkeypatch.setattr(settings, "APP_ENV", "prod")
    monkeypatch.setattr(settings, "RUN_REAL_MAIL_INTEGRATION_TESTS", True)
    monkeypatch.setattr(settings, "TEST_RELAY_BASE_URL", "http://127.0.0.1:18765")
    monkeypatch.setattr(settings, "TEST_RELAY_TOKEN", TOKEN)
    status = relay_configuration_status()
    assert status["configured"] is False
    assert "TEST_RELAY_ENV_NOT_ALLOWED" in status["missing"]


def test_relay_is_authenticated_and_idempotent(tmp_path: Path) -> None:
    client = TestClient(create_app(database=tmp_path / "relay.sqlite3", token=TOKEN))
    payload = {
        "submission_key": "submission-key-0001",
        "ticket_id": 42,
        "ticket_item_id": 10,
        "sn": "SN000001",
    }
    unauthorized = client.post("/records", json=payload)
    assert unauthorized.status_code == 401

    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = client.post("/records", json=payload, headers=headers)
    second = client.post("/records", json=payload, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["remote_record_key"] == second.json()["remote_record_key"]
    assert first.json()["idempotent_reuse"] is False
    assert second.json()["idempotent_reuse"] is True

    polled = client.get(
        f"/records/{first.json()['remote_record_key']}", headers=headers
    ).json()
    assert polled["status"] == "rma_received"
    assert len(polled["rma_no"]) == 10
    assert polled["rma_no"].isdigit()


def test_same_ticket_shares_rma_and_multi_rma_scenario_splits_it(tmp_path: Path) -> None:
    client = TestClient(create_app(database=tmp_path / "relay.sqlite3", token=TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = client.post(
        "/records",
        json={"submission_key": "submission-key-0001", "ticket_id": 7, "ticket_item_id": 1, "sn": "SN1"},
        headers=headers,
    ).json()
    second = client.post(
        "/records",
        json={"submission_key": "submission-key-0002", "ticket_id": 7, "ticket_item_id": 2, "sn": "SN2"},
        headers=headers,
    ).json()
    first_rma = client.get(f"/records/{first['remote_record_key']}", headers=headers).json()["rma_no"]
    second_rma = client.get(f"/records/{second['remote_record_key']}", headers=headers).json()["rma_no"]
    assert first_rma == second_rma

    configured = client.put(
        "/control/default", json={"scenario": "multi_rma", "delay_seconds": 0}, headers=headers
    )
    assert configured.status_code == 200
    third = client.post(
        "/records",
        json={"submission_key": "submission-key-0003", "ticket_id": 8, "ticket_item_id": 1, "sn": "SN3"},
        headers=headers,
    ).json()
    fourth = client.post(
        "/records",
        json={"submission_key": "submission-key-0004", "ticket_id": 8, "ticket_item_id": 2, "sn": "SN4"},
        headers=headers,
    ).json()
    assert (
        client.get(f"/records/{third['remote_record_key']}", headers=headers).json()["rma_no"]
        != client.get(f"/records/{fourth['remote_record_key']}", headers=headers).json()["rma_no"]
    )
