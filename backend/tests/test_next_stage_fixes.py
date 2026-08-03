from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import tickets as ticket_service
from app.services.master_data import BOARD_CARD_FIELDS


def test_production_settings_reject_placeholder_secrets() -> None:
    with pytest.raises(ValidationError, match="insecure production settings"):
        Settings(_env_file=None, APP_ENV="production")


def test_production_settings_accept_explicit_secure_boundary_values() -> None:
    configured = Settings(
        _env_file=None,
        APP_ENV="production",
        DATABASE_URL="mysql+asyncmy://repair:strong-password@db:3306/repair",
        JWT_SECRET="a-secure-production-secret-with-32-characters",
        DEFAULT_ADMIN_PASSWORD="a-strong-bootstrap-password",
        CORS_ALLOWED_ORIGINS=["https://repair.example.com"],
        TRUSTED_HOSTS=["repair.example.com"],
    )

    assert configured.APP_ENV == "production"


def test_board_card_public_fields_do_not_expose_sap_material_aliases() -> None:
    assert "board_code" in BOARD_CARD_FIELDS
    assert "board_name" in BOARD_CARD_FIELDS
    assert "material_code" not in BOARD_CARD_FIELDS
    assert "material_name" not in BOARD_CARD_FIELDS


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
