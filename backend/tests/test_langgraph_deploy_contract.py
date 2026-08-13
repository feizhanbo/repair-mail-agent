from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_deploy_initializes_graph_checkpoint_before_application_start() -> None:
    script = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    evidence = script.index("--verify-local-release-evidence")
    postgres_start = script.index("docker compose --profile langgraph up -d langgraph-postgres")
    setup = script.index("python -m tools.setup_langgraph_checkpoint")
    audit = script.index("python -m tools.audit_langgraph_release", evidence + 1)
    application_start = script.index('echo "Starting application services..."')

    executable_lines = [line.strip() for line in script.splitlines() if not line.lstrip().startswith("#")]
    assert "source .env" not in executable_lines
    assert ". .env" not in executable_lines
    assert '[[ "$WORKFLOW_ENGINE_VALUE" == "langgraph" ]]' in script
    assert "docker compose --profile langgraph up -d langgraph-postgres" in script
    assert evidence < postgres_start < setup < audit < application_start
    assert "LANGGRAPH_RELEASE_EVIDENCE_FILE is required for langgraph deployment" in script
    assert "LANGGRAPH_RELEASE_EVIDENCE_FILE must be under /app/release-evidence/" in script
    assert '--expected-commit "$DEPLOY_COMMIT"' in script
    assert "--evidence-root /app/release-evidence" in script
    assert "--max-evidence-age-hours 168" in script
    assert 'export APP_RELEASE_COMMIT="$DEPLOY_COMMIT"' in script


def test_compose_keeps_checkpoint_optional_for_legacy_backend() -> None:
    import yaml

    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "langgraph-postgres" in compose["services"]
    assert compose["services"]["langgraph-postgres"]["profiles"] == ["langgraph"]
    assert compose["services"]["backend-api"]["depends_on"] == {
        "mysql": {"condition": "service_healthy"}
    }
    assert "./release-evidence:/app/release-evidence:ro" in compose["services"]["backend-api"]["volumes"]
    assert compose["services"]["backend-api"]["environment"]["APP_RELEASE_COMMIT"] == "${APP_RELEASE_COMMIT:-}"


def test_legacy_deploy_does_not_start_optional_checkpoint_database() -> None:
    script = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")
    graph_branch = script.index('if [[ "$WORKFLOW_ENGINE_VALUE" == "langgraph" ]]')
    postgres_start = script.index("docker compose --profile langgraph up -d langgraph-postgres")
    graph_branch_end = script.index("\nfi", graph_branch)

    assert graph_branch < postgres_start < graph_branch_end
