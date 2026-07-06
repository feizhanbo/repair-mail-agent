from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_current_user
from app.core.database import get_session
from app.main import app
from app.services import manual_review as manual_review_service
from app.services import replies as reply_service
from app.services import tickets as ticket_service
from app.services import users as user_service


class FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


def make_current_user(*, roles: list[str] | None = None, user_id: int = 7) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        username="tester",
        real_name="Tester",
        email="tester@example.local",
        phone=None,
        department="Test",
        status="active",
        roles=roles or ["admin"],
    )


def make_client(session: FakeSession | None = None, *, roles: list[str] | None = None, user_id: int = 7) -> TestClient:
    fake_session = session or FakeSession()

    async def fake_get_session() -> AsyncGenerator[FakeSession, None]:
        yield fake_session

    async def fake_current_user() -> CurrentUser:
        return make_current_user(roles=roles, user_id=user_id)

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_session] = fake_get_session
    return TestClient(app)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def test_expected_business_routes_are_registered() -> None:
    routes = {f"{','.join(sorted(route.methods or []))} {route.path}" for route in app.routes}
    assert "POST /api/v1/auth/login" in routes
    assert "POST /api/v1/emails/{email_id}/reparse" in routes
    assert "PATCH /api/v1/tickets/{ticket_id}/fields" in routes
    assert "POST /api/v1/manual-review/tasks/{task_id}/reparse" in routes
    assert "POST /api/v1/replies/{ticket_id}/draft" in routes
    assert "GET /api/v1/ai-logs" in routes
    assert "GET /api/v1/system/info" in routes
    assert "GET /api/v1/users" in routes
    assert "DELETE /api/v1/users/{user_id}" in routes
    assert "PATCH /api/v1/auth/me/profile" in routes
    assert "GET /api/v1/db-browser/tables" in routes


def test_ticket_detail_success_contract(monkeypatch) -> None:
    async def fake_detail(_session, ticket_id: int):
        return {"ticket": {"id": ticket_id, "ticket_no": "RMA001"}, "items": []}

    monkeypatch.setattr(ticket_service, "get_ticket_detail", fake_detail)

    with make_client() as client:
        response = client.get("/api/v1/tickets/42")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["ticket"]["id"] == 42
    assert payload["message"] == "ok"
    assert payload["request_id"].startswith("req_")


def test_ticket_list_page_contract(monkeypatch) -> None:
    async def fake_list(_session, **kwargs):
        assert kwargs["page"] == 2
        assert kwargs["page_size"] == 5
        return ([{"id": 1, "ticket_no": "RMA001"}], 11)

    monkeypatch.setattr(ticket_service, "list_tickets", fake_list)

    with make_client() as client:
        response = client.get("/api/v1/tickets?page=2&page_size=5")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"] == {"items": [{"id": 1, "ticket_no": "RMA001"}], "total": 11, "page": 2, "page_size": 5}


def test_http_exception_uses_unified_error_contract(monkeypatch) -> None:
    async def fake_detail(_session, _ticket_id: int):
        raise HTTPException(status_code=404, detail="TICKET_NOT_FOUND")

    monkeypatch.setattr(ticket_service, "get_ticket_detail", fake_detail)

    with make_client() as client:
        response = client.get("/api/v1/tickets/404")

    payload = response.json()
    assert response.status_code == 404
    assert payload["success"] is False
    assert payload["message"] == "TICKET_NOT_FOUND"
    assert payload["request_id"].startswith("req_")


def test_validation_error_uses_unified_error_contract() -> None:
    with make_client() as client:
        response = client.post("/api/v1/auth/login", json={})

    payload = response.json()
    assert response.status_code == 422
    assert payload["success"] is False
    assert payload["message"] == "REQUEST_VALIDATION_ERROR"
    assert payload["data"]["errors"]


def test_mutating_endpoint_commits_after_service_success(monkeypatch) -> None:
    session = FakeSession()

    async def fake_claim(_session, *, task_id: int, user_id: int):
        return {"id": task_id, "claimed_by_user_id": user_id, "status": "claimed"}

    monkeypatch.setattr(manual_review_service, "claim_task", fake_claim)

    with make_client(session) as client:
        response = client.post("/api/v1/manual-review/tasks/9/claim")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["claimed_by_user_id"] == 7
    assert session.committed is True


def test_delete_user_rejects_current_user() -> None:
    with make_client() as client:
        response = client.delete("/api/v1/users/7")

    payload = response.json()
    assert response.status_code == 400
    assert payload["success"] is False
    assert payload["message"] == "USER_CANNOT_DELETE_SELF"


def test_delete_user_success_contract(monkeypatch) -> None:
    async def fake_delete(_session, *, user_id: int, operator_user_id: int):
        assert user_id == 12
        assert operator_user_id == 7
        return {"deleted": True, "user": {"id": user_id, "username": "codex_operator_validation"}}

    monkeypatch.setattr(user_service, "delete_user", fake_delete)

    with make_client() as client:
        response = client.delete("/api/v1/users/12")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["deleted"] is True
    assert payload["data"]["user"]["username"] == "codex_operator_validation"


def test_delete_user_reference_conflict_contract(monkeypatch) -> None:
    async def fake_delete(_session, *, user_id: int, operator_user_id: int):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "USER_HAS_REFERENCES",
                "data": {"references": [{"table": "operation_logs", "field": "user_id", "count": 1}]},
            },
        )

    monkeypatch.setattr(user_service, "delete_user", fake_delete)

    with make_client() as client:
        response = client.delete("/api/v1/users/12")

    payload = response.json()
    assert response.status_code == 409
    assert payload["success"] is False
    assert payload["message"] == "USER_HAS_REFERENCES"
    assert payload["data"]["references"][0]["table"] == "operation_logs"


def test_admin_can_list_users(monkeypatch) -> None:
    async def fake_list(_session, **kwargs):
        return ([{"id": 1, "username": "admin", "roles": ["admin"]}], 1)

    monkeypatch.setattr(user_service, "list_users", fake_list)

    with make_client(roles=["admin"]) as client:
        response = client.get("/api/v1/users")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["items"][0]["roles"] == ["admin"]


def test_operator_cannot_list_users() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.get("/api/v1/users")

    payload = response.json()
    assert response.status_code == 403
    assert payload["success"] is False
    assert payload["message"] == "AUTH_FORBIDDEN"


def test_operator_cannot_approve_reply() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.post("/api/v1/replies/5/approve-send")

    payload = response.json()
    assert response.status_code == 403
    assert payload["message"] == "AUTH_FORBIDDEN"


def test_supervisor_can_approve_reply(monkeypatch) -> None:
    session = FakeSession()

    async def fake_approve(_session, *, reply_id: int, user_id: int):
        assert reply_id == 5
        assert user_id == 7
        return {"id": reply_id, "review_status": "approved"}

    monkeypatch.setattr(reply_service, "approve_reply", fake_approve)

    with make_client(session, roles=["supervisor"]) as client:
        response = client.post("/api/v1/replies/5/approve-send")

    payload = response.json()
    assert response.status_code == 200
    assert payload["data"]["review_status"] == "approved"
    assert session.committed is True


def test_operator_cannot_assign_manual_task() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.post("/api/v1/manual-review/tasks/9/assign", json={"assigned_user_id": 8})

    payload = response.json()
    assert response.status_code == 403
    assert payload["message"] == "AUTH_FORBIDDEN"


def test_supervisor_can_assign_manual_task(monkeypatch) -> None:
    session = FakeSession()

    async def fake_assign(_session, *, task_id: int, assigned_user_id: int | None, operator_user_id: int, reason: str | None):
        assert task_id == 9
        assert assigned_user_id == 8
        assert operator_user_id == 7
        return {"id": task_id, "assigned_user_id": assigned_user_id, "status": "assigned"}

    monkeypatch.setattr(manual_review_service, "assign_task", fake_assign)

    with make_client(session, roles=["supervisor"]) as client:
        response = client.post("/api/v1/manual-review/tasks/9/assign", json={"assigned_user_id": 8, "reason": "测试分配"})

    payload = response.json()
    assert response.status_code == 200
    assert payload["data"]["assigned_user_id"] == 8
    assert session.committed is True


def test_operator_cannot_query_all_manual_tasks() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.get("/api/v1/manual-review/tasks?scope=all")

    payload = response.json()
    assert response.status_code == 403
    assert payload["message"] == "AUTH_FORBIDDEN"
