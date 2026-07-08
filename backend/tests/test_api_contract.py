from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_current_user
from app.config import settings
from app.core.database import get_session
from app.main import app
from app.services import manual_review as manual_review_service
from app.services import master_data as master_data_service
from app.services import replies as reply_service
from app.services import tickets as ticket_service
from app.services import users as user_service


class FakeScalarResult:
    def all(self) -> list:
        return []


class FakeExecuteResult:
    def all(self) -> list:
        return []

    def scalars(self) -> FakeScalarResult:
        return FakeScalarResult()


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.executed = []
        self.scalar_statements = []

    async def commit(self) -> None:
        self.committed = True

    async def execute(self, statement) -> FakeExecuteResult:
        self.executed.append(statement)
        return FakeExecuteResult()

    async def scalar(self, statement) -> int:
        self.scalar_statements.append(statement)
        return 0


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
    assert "GET /api/v1/system/config" in routes
    assert "PATCH /api/v1/system/config" in routes
    assert "GET /api/v1/system/reply-templates" in routes
    assert "PATCH /api/v1/system/reply-templates/{template_id}" in routes
    assert "GET /api/v1/statistics/summary" in routes
    assert "GET /api/v1/users" in routes
    assert "DELETE /api/v1/users/{user_id}" in routes
    assert "PATCH /api/v1/auth/me/profile" in routes
    assert "GET /api/v1/db-browser/tables" in routes
    assert "GET /api/v1/master-data/sn-assets/template" in routes
    assert "GET /api/v1/master-data/sn-assets/export" in routes
    assert "POST /api/v1/master-data/sn-assets/import-file" in routes
    assert "GET /api/v1/master-data/board-cards/template" in routes
    assert "GET /api/v1/master-data/board-cards/export" in routes
    assert "POST /api/v1/master-data/board-cards/import-file" in routes
    assert "GET /api/v1/tickets/export" in routes


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


def test_ticket_structured_filters_are_forwarded(monkeypatch) -> None:
    seen = {}

    async def fake_list(_session, **kwargs):
        seen.update(kwargs)
        return ([], 0)

    monkeypatch.setattr(ticket_service, "list_tickets", fake_list)

    with make_client() as client:
        response = client.get(
            "/api/v1/tickets",
            params={
                "ticket_no": "RMA",
                "customer": "Acme",
                "contact": "alice@example.local",
                "sn": "SN001",
                "assigned_user_id": 9,
                "status_code": "manual_review",
                "request_date_start": "2026-07-01",
                "request_date_end": "2026-07-07",
                "keyword": "compat",
            },
        )

    assert response.status_code == 200
    assert seen["ticket_no"] == "RMA"
    assert seen["customer"] == "Acme"
    assert seen["contact"] == "alice@example.local"
    assert seen["sn"] == "SN001"
    assert seen["assigned_user_id"] == 9
    assert seen["status_code"] == "manual_review"
    assert seen["keyword"] == "compat"
    assert seen["request_date_start"] == date(2026, 7, 1)
    assert seen["request_date_end"] == date(2026, 7, 7)


def test_ticket_export_returns_xlsx_blob(monkeypatch) -> None:
    seen = {}

    async def fake_export(_session, **kwargs):
        seen.update(kwargs)
        return [{"ticket_no": "RMA001", "current_status_code": "ready_for_export"}]

    monkeypatch.setattr(ticket_service, "export_tickets", fake_export)

    with make_client() as client:
        response = client.get("/api/v1/tickets/export", params={"status_code": "ready_for_export", "customer": "Acme"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert response.content.startswith(b"PK")
    assert seen["status_code"] == "ready_for_export"
    assert seen["customer"] == "Acme"


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


def test_manual_task_structured_filters_are_forwarded(monkeypatch) -> None:
    seen = {}

    async def fake_list(_session, **kwargs):
        seen.update(kwargs)
        return ([], 0)

    monkeypatch.setattr(manual_review_service, "list_tasks", fake_list)

    with make_client(roles=["supervisor"]) as client:
        response = client.get(
            "/api/v1/manual-review/tasks",
            params={
                "status": "pending",
                "task_type": "missing_fields",
                "priority": "high",
                "assigned_user_id": 8,
                "scope": "all",
                "created_start": "2026-07-01",
                "created_end": "2026-07-07",
            },
        )

    assert response.status_code == 200
    assert seen["task_status"] == "pending"
    assert seen["task_type"] == "missing_fields"
    assert seen["priority"] == "high"
    assert seen["assigned_user_id"] == 8
    assert seen["scope"] == "all"
    assert seen["created_start"] == date(2026, 7, 1)
    assert seen["created_end"] == date(2026, 7, 7)


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


def test_operator_can_query_all_manual_tasks() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.get("/api/v1/manual-review/tasks?scope=all")

    payload = response.json()
    assert response.status_code == 200
    assert payload["success"] is True


def test_operator_cannot_patch_system_config() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.patch("/api/v1/system/config", json={"auto_send_enabled": True})

    payload = response.json()
    assert response.status_code == 403
    assert payload["message"] == "AUTH_FORBIDDEN"


def test_supervisor_can_patch_system_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "RUNTIME_CONFIG_PATH", str(tmp_path / "runtime_config.json"))
    monkeypatch.setattr(settings, "AUTO_SEND_ENABLED", False)
    monkeypatch.setattr(settings, "CONFIDENCE_THRESHOLD", 0.8)
    monkeypatch.setattr(settings, "MAX_FOLLOW_UP", 2)

    with make_client(roles=["supervisor"]) as client:
        response = client.patch(
            "/api/v1/system/config",
            json={"auto_send_enabled": True, "reply_send_mode": "auto_send", "auto_send_min_confidence": 0.88, "confidence_threshold": 0.91, "max_follow_up": 3},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["data"]["auto_send_enabled"] is True
    assert payload["data"]["reply_send_mode"] == "auto_send"
    assert payload["data"]["auto_send_min_confidence"] == 0.88
    assert payload["data"]["confidence_threshold"] == 0.91
    assert payload["data"]["max_follow_up"] == 3


def test_operator_cannot_query_statistics_summary() -> None:
    with make_client(roles=["operator"]) as client:
        response = client.get("/api/v1/statistics/summary")

    payload = response.json()
    assert response.status_code == 403
    assert payload["message"] == "AUTH_FORBIDDEN"


def test_supervisor_statistics_summary_empty_contract() -> None:
    with make_client(roles=["supervisor"]) as client:
        response = client.get("/api/v1/statistics/summary", params={"period": "week", "start_date": "2026-07-01", "end_date": "2026-07-07"})

    payload = response.json()
    assert response.status_code == 200
    assert payload["data"]["period"] == "week"
    assert payload["data"]["start_date"] == "2026-07-01"
    assert payload["data"]["end_date"] == "2026-07-07"
    assert payload["data"]["email_count"] == 0
    assert payload["data"]["ticket_count"] == 0
    assert payload["data"]["ai_success_rate"] == 0
    assert len(payload["data"]["trend"]) == 7
    assert payload["data"]["trend"][0]["date"] == "2026-07-01"
    assert payload["data"]["manual_intervention_rate"] == 0


def test_master_data_filter_params_are_forwarded(monkeypatch) -> None:
    seen = {}

    async def fake_list(_session, **kwargs):
        seen.update(kwargs)
        return ([], 0)

    monkeypatch.setattr(master_data_service, "list_sn_assets", fake_list)

    with make_client(roles=["supervisor"]) as client:
        response = client.get(
            "/api/v1/master-data/sn-assets",
            params={"sn": "SN", "customer": "Acme", "material": "MAT", "asset_status": "valid", "keyword": "compat"},
        )

    assert response.status_code == 200
    assert seen["sn"] == "SN"
    assert seen["customer"] == "Acme"
    assert seen["material"] == "MAT"
    assert seen["asset_status"] == "valid"
    assert seen["keyword"] == "compat"
