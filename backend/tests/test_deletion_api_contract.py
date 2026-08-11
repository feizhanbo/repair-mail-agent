from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.main import app


def _parameters(operation: dict) -> dict[str, dict]:
    return {row["name"]: row for row in operation.get("parameters", [])}


def test_delete_endpoints_are_registered_with_preview_and_admin_auth() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    expected = {
        "/api/v1/emails/attachments/{attachment_id}": "delete",
        "/api/v1/emails/{email_id}": "delete",
        "/api/v1/tickets/{ticket_id}": "delete",
        "/api/v1/emails/attachments/{attachment_id}/delete-preview": "get",
        "/api/v1/emails/{email_id}/delete-preview": "get",
        "/api/v1/tickets/{ticket_id}/delete-preview": "get",
        "/api/v1/deletion-operations/{operation_log_id}": "get",
        "/api/v1/deletion-operations/{operation_log_id}/retry": "post",
    }
    for path, method in expected.items():
        assert method in paths[path]
        assert paths[path][method].get("security") == [{"OAuth2PasswordBearer": []}]


def test_delete_mutations_require_reason_and_preview_token() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    for path in (
        "/api/v1/emails/attachments/{attachment_id}",
        "/api/v1/emails/{email_id}",
        "/api/v1/tickets/{ticket_id}",
    ):
        parameters = _parameters(paths[path]["delete"])
        assert parameters["reason"]["required"] is True
        assert parameters["reason"]["schema"]["minLength"] == 3
        assert parameters["confirmation_token"]["required"] is True
        assert parameters["confirmation_token"]["schema"]["minLength"] == 20


def test_force_local_cleanup_is_not_available_for_attachment_delete() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    attachment = _parameters(paths["/api/v1/emails/attachments/{attachment_id}"]["delete"])
    email = _parameters(paths["/api/v1/emails/{email_id}"]["delete"])
    ticket = _parameters(paths["/api/v1/tickets/{ticket_id}"]["delete"])
    assert "force_local_cleanup" not in attachment
    assert email["force_local_cleanup"]["schema"]["default"] is False
    assert ticket["force_local_cleanup"]["schema"]["default"] is False


def test_operator_cannot_access_delete_preview() -> None:
    async def fake_session() -> AsyncGenerator[object, None]:
        yield object()

    async def operator() -> CurrentUser:
        return CurrentUser(
            id=7,
            username="operator",
            real_name="Operator",
            email=None,
            phone=None,
            status="active",
            roles=["operator"],
        )

    app.dependency_overrides[get_session] = fake_session
    app.dependency_overrides[get_current_user] = operator
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/emails/1/delete-preview")
        assert response.status_code == 403
        assert response.json()["message"] == "AUTH_FORBIDDEN"
    finally:
        app.dependency_overrides.clear()
