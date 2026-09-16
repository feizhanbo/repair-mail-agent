from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.business import BoardCardImportItem, TicketItemUpsert
from app.services import tickets as ticket_service
from app.services.master_data import BOARD_CARD_FIELDS


def test_settings_require_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("AIRMA_WORKER_ENVIRONMENT", raising=False)
    with pytest.raises(ValidationError, match="APP_ENV"):
        Settings(_env_file=None)


def test_production_settings_reject_placeholder_secrets() -> None:
    with pytest.raises(ValidationError, match="insecure production settings"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            AIRMA_WORKER_ENVIRONMENT="production",
        )


@pytest.mark.parametrize("value", ["", "dev", "prod", "staging", "PRODUCTION-LIKE"])
def test_settings_reject_unknown_or_abbreviated_environment(value: str) -> None:
    with pytest.raises(ValidationError, match="environment must be one of"):
        Settings(_env_file=None, APP_ENV=value)


def test_production_settings_accept_explicit_secure_boundary_values() -> None:
    configured = Settings(
        _env_file=None,
        APP_ENV="production",
        AIRMA_WORKER_ENVIRONMENT="production",
        DATABASE_URL="mysql+asyncmy://repair:strong-password@db:3306/repair",
        JWT_SECRET="a-secure-production-secret-with-32-characters",
        DEFAULT_ADMIN_PASSWORD="a-strong-bootstrap-password",
        CORS_ALLOWED_ORIGINS=["https://repair.example.com"],
        TRUSTED_HOSTS=["repair.example.com"],
        IMAP_FETCH_ENABLED=False,
        RMA_AUTO_SEND_ENABLED=False,
    )

    assert configured.APP_ENV == "production"


def test_production_enabled_mail_ingress_requires_real_credentials() -> None:
    with pytest.raises(ValidationError, match="IMAP_PASSWORD"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            AIRMA_WORKER_ENVIRONMENT="production",
            DATABASE_URL="mysql+asyncmy://repair:strong-password@db:3306/repair",
            JWT_SECRET="a-secure-production-secret-with-32-characters",
            DEFAULT_ADMIN_PASSWORD="a-strong-bootstrap-password",
            CORS_ALLOWED_ORIGINS=["https://repair.example.com"],
            TRUSTED_HOSTS=["repair.example.com"],
            IMAP_FETCH_ENABLED=True,
            RMA_AUTO_SEND_ENABLED=False,
        )


def test_board_card_public_fields_do_not_expose_sap_material_aliases() -> None:
    assert "board_code" in BOARD_CARD_FIELDS
    assert "board_name" in BOARD_CARD_FIELDS
    assert "material_code" not in BOARD_CARD_FIELDS
    assert "material_name" not in BOARD_CARD_FIELDS


@pytest.mark.parametrize("schema", [BoardCardImportItem, TicketItemUpsert])
@pytest.mark.parametrize("field", ["material_code", "material_name"])
def test_board_inputs_reject_sap_material_aliases(schema, field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        schema.model_validate({"board_code": "BOARD-1", field: "FORBIDDEN"})


@pytest.mark.anyio
async def test_export_snapshot_invalidation_uses_workflow_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    async def fake_transition(_session, **kwargs):
        calls.append(kwargs)
        kwargs["ticket"].current_status_code = kwargs["to_status_code"]
        return kwargs["ticket"]

    monkeypatch.setattr(ticket_service, "transition_ticket", fake_transition)
    ticket = SimpleNamespace(
        id=10,
        current_status_code="ready_for_export",
        assigned_user_id=7,
        rma_required=True,
        sn_validation_status="passed",
        sn_validation_snapshot={"valid": True},
        sn_validation_hash="sn-hash",
        sn_validated_at=object(),
        safety_check_snapshot={"valid": True},
        safety_check_hash="safe-hash",
        safety_checked_at=object(),
        relay_export_status="ready",
        rma_status="ready",
    )

    await ticket_service._invalidate_export_snapshot(
        SimpleNamespace(),
        ticket=ticket,
        user_id=7,
        reason="validated fields changed",
        invalidate_sn=True,
    )

    assert ticket.current_status_code == "manual_review"
    assert ticket.sn_validation_status == "stale"
    assert ticket.safety_check_hash is None
    assert len(calls) == 1
    assert calls[0]["trigger_event"] == "validated_data_changed"
    assert calls[0]["manual_task_type"] == "validated_data_changed"
    assert calls[0]["manual_task_priority"] == "high"
