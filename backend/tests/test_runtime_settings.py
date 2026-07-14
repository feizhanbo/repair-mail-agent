from __future__ import annotations

import json

import pytest

from app.api.v1.system import _config_payload
from app.config import settings


def test_system_payload_reports_split_ai_configuration_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret_values = {
        "qwen": "not-a-real-qwen-secret",
        "imap": "not-a-real-imap-password",
        "smtp": "not-a-real-smtp-password",
        "oss_access": "not-a-real-oss-access-key",
        "oss_secret": "not-a-real-oss-secret-key",
    }
    monkeypatch.setattr(settings, "RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    monkeypatch.setattr(settings, "AI_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "QWEN_API_KEY", secret_values["qwen"])
    monkeypatch.setattr(settings, "MULTIMODAL_PROVIDER", "qwen")
    monkeypatch.setattr(settings, "QWEN_VL_MODEL", "qwen-vl-plus")
    monkeypatch.setattr(settings, "IMAP_HOST", "imap.example.com")
    monkeypatch.setattr(settings, "IMAP_USER", "imap@example.com")
    monkeypatch.setattr(settings, "IMAP_PASSWORD", secret_values["imap"])
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USER", "smtp@example.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", secret_values["smtp"])
    monkeypatch.setattr(settings, "OSS_ENDPOINT", "oss.example.com")
    monkeypatch.setattr(settings, "OSS_BUCKET", "repair-mail")
    monkeypatch.setattr(settings, "OSS_ACCESS_KEY", secret_values["oss_access"])
    monkeypatch.setattr(settings, "OSS_SECRET_KEY", secret_values["oss_secret"])

    payload = _config_payload()

    integrations = payload["integrations"]
    assert integrations["ai_configured"] is False
    assert integrations["text_ai_configured"] is False
    assert integrations["text_ai_provider"] == "deepseek"
    assert integrations["multimodal_ai_configured"] is True
    assert integrations["multimodal_provider"] == "qwen"
    assert integrations["imap_configured"] is True
    assert integrations["smtp_configured"] is True
    assert integrations["oss_configured"] is True

    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in secret_values.values():
        assert secret not in serialized
