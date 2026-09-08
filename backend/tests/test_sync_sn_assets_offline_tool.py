from __future__ import annotations

from tools import sync_sn_assets_offline as tool


def test_row_identity_is_ins_id_and_sn_can_repeat():
    convert = tool._make_row_to_write("run-1")
    first_key, first_values = convert(
        (1, "SN001", "C1", "Customer", "Z.SM.XA", "A", None, None, None, None, "2026-01-01")
    )
    second_key, second_values = convert(
        (2, "SN001", "C1", "Customer", "Z.SM.XAB", "AB", None, None, None, None, "2026-01-01")
    )

    assert tool.SN_ASSETS_UNIQUE_COL == "ins_id"
    assert first_key == 1
    assert second_key == 2
    sn_index = tool.SN_ASSETS_PARAM_COLUMNS.index("sn")
    assert first_values[sn_index] == second_values[sn_index] == "SN001"


def test_row_without_sn_or_ins_id_is_quarantined():
    convert = tool._make_row_to_write("run-1")
    assert convert((None, "SN001", "", None, "", None, None, None, None, None, None)) is None
    assert convert((1, "  ", "", None, "", None, None, None, None, None, None)) is None


def test_upsert_preserves_composite_row_identity():
    sql = tool._build_sn_assets_upsert_sql()
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]

    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "`ins_id` = VALUES(`ins_id`)" not in update_clause
    assert "`source_system` = VALUES(`source_system`)" not in update_clause
    assert "`sn` = VALUES(`sn`)" in update_clause


def test_password_masking_covers_plain_and_url_encoded_values():
    text = "source=p@ss target=p%40ss"
    assert tool._mask_all(text, "p@ss", "p@ss") == "source=*** target=***"


def test_allowed_source_failure_returns_not_run_without_mysql(monkeypatch, capsys):
    values = {
        "MSSQL_HOST": "sql.example.test",
        "MSSQL_PORT": "1433",
        "MSSQL_USER": "source-user",
        "MSSQL_PASSWORD": "source-secret",
        "MSSQL_DATABASE": "RMA_MS",
        "DATABASE_URL": "mysql+asyncmy://root:target-secret@127.0.0.1:13307/AIRMA_test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        tool,
        "_connect_mssql",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("source-secret unavailable")),
    )
    mysql_called = False

    def reject_mysql(*args, **kwargs):
        nonlocal mysql_called
        mysql_called = True
        raise AssertionError("MySQL must not be connected")

    monkeypatch.setattr(tool, "_connect_mysql", reject_mysql)

    assert tool.main(["--dry-run", "--allow-source-unavailable"]) == 0
    output = capsys.readouterr().out
    assert "OVERALL: NOT RUN" in output
    assert "source-secret" not in output
    assert mysql_called is False
