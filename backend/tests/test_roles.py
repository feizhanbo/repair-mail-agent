from app.seed import ROLES


def test_role_seed_matches_current_role_design() -> None:
    roles = {item["role_code"]: item for item in ROLES}
    assert set(roles) == {"admin", "operator"}
    assert roles["admin"]["role_name"] == "系统管理员"
    assert roles["operator"]["role_name"] == "一般操作员"
