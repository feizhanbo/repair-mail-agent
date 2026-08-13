from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_workflow_engine_defaults_to_legacy() -> None:
    assert Settings(_env_file=None).WORKFLOW_ENGINE == "legacy"


def test_workflow_engine_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError, match="WORKFLOW_ENGINE"):
        Settings(_env_file=None, WORKFLOW_ENGINE="experimental")


def test_langgraph_engine_requires_dedicated_checkpoint_url() -> None:
    with pytest.raises(ValidationError, match="LANGGRAPH_CHECKPOINT_DATABASE_URL"):
        Settings(
            _env_file=None,
            WORKFLOW_ENGINE="langgraph",
            LANGGRAPH_CHECKPOINT_DATABASE_URL="",
        )
    settings = Settings(
        _env_file=None,
        WORKFLOW_ENGINE="langgraph",
        LANGGRAPH_CHECKPOINT_DATABASE_URL="postgresql://checkpoint/graph",
    )
    assert settings.WORKFLOW_ENGINE == "langgraph"


def test_checkpoint_url_rejects_business_mysql() -> None:
    with pytest.raises(ValidationError, match="must be a PostgreSQL URL"):
        Settings(
            _env_file=None,
            LANGGRAPH_CHECKPOINT_DATABASE_URL="mysql+asyncmy://db/business",
        )


def test_production_graph_rejects_default_checkpoint_secret() -> None:
    with pytest.raises(ValidationError, match="LANGGRAPH_CHECKPOINT_DATABASE_URL"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            WORKFLOW_ENGINE="langgraph",
            LANGGRAPH_CHECKPOINT_DATABASE_URL=(
                "postgresql://repair_langgraph:change-me-langgraph@langgraph-postgres/repair_langgraph"
            ),
            DATABASE_URL="mysql+asyncmy://repair:strong-secret@mysql/repair_system",
            JWT_SECRET="a-secure-production-jwt-secret-that-is-long-enough",
            DEFAULT_ADMIN_PASSWORD="a-secure-admin-password",
            CORS_ALLOWED_ORIGINS=["https://repair.example.com"],
            TRUSTED_HOSTS=["repair.example.com"],
        )


def test_langgraph_rollout_percent_is_bounded() -> None:
    assert Settings(_env_file=None, LANGGRAPH_ROLLOUT_PERCENT=0).LANGGRAPH_ROLLOUT_PERCENT == 0
    assert Settings(_env_file=None, LANGGRAPH_ROLLOUT_PERCENT=100).LANGGRAPH_ROLLOUT_PERCENT == 100
    with pytest.raises(ValidationError, match="LANGGRAPH_ROLLOUT_PERCENT"):
        Settings(_env_file=None, LANGGRAPH_ROLLOUT_PERCENT=101)


def test_strict_msgpack_defaults_enabled() -> None:
    assert Settings(_env_file=None).LANGGRAPH_STRICT_MSGPACK is True


def test_async_job_stale_window_allows_heartbeat_safety_margin() -> None:
    assert Settings(_env_file=None, ASYNC_JOB_STALE_SECONDS=30).ASYNC_JOB_STALE_SECONDS == 30
    with pytest.raises(ValidationError, match="ASYNC_JOB_STALE_SECONDS"):
        Settings(_env_file=None, ASYNC_JOB_STALE_SECONDS=29)


def test_release_evidence_max_age_must_be_positive() -> None:
    assert Settings(_env_file=None).LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS == 168
    with pytest.raises(ValidationError, match="LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS"):
        Settings(_env_file=None, LANGGRAPH_RELEASE_EVIDENCE_MAX_AGE_HOURS=0)
