from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
MODEL_FACTORY = Path("ai/models.py")
STRUCTURED_GATEWAY = Path("ai/gateway.py")
PROVIDER_FACADES = {
    Path("integrations/ai_provider.py"),
    Path("integrations/qwen_provider.py"),
}
VENDOR_SDK_ROOTS = {
    "anthropic",
    "dashscope",
    "google.generativeai",
    "langchain_anthropic",
    "langchain_google_genai",
    "langchain_openai",
    "openai",
}
MODEL_CONSTRUCTORS = {
    "Anthropic",
    "AsyncAnthropic",
    "AsyncOpenAI",
    "ChatAnthropic",
    "ChatGoogleGenerativeAI",
    "ChatOpenAI",
    "OpenAI",
}


def _python_sources() -> list[tuple[Path, str, ast.AST]]:
    sources: list[tuple[Path, str, ast.AST]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        sources.append((path.relative_to(APP_ROOT), source, ast.parse(source, filename=str(path))))
    return sources


def _module_root(module: str) -> str | None:
    for root in VENDOR_SDK_ROOTS:
        if module == root or module.startswith(f"{root}."):
            return root
    return None


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_vendor_model_sdks_are_imported_only_by_the_model_factory() -> None:
    violations: list[str] = []
    for relative, _source, tree in _python_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                root = _module_root(module)
                if root and relative != MODEL_FACTORY:
                    violations.append(f"{relative}:{node.lineno} imports {root}")
    assert violations == []


def test_vendor_model_constructors_exist_only_in_the_model_factory() -> None:
    violations: list[str] = []
    for relative, _source, tree in _python_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in MODEL_CONSTRUCTORS and relative != MODEL_FACTORY:
                violations.append(f"{relative}:{node.lineno} calls {_call_name(node)}")
    assert violations == []


def test_structured_output_invocation_is_owned_by_the_gateway() -> None:
    violations: list[str] = []
    for relative, _source, tree in _python_sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "with_structured_output"
                and relative != STRUCTURED_GATEWAY
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []


def test_provider_facades_use_gateway_without_raw_ai_http_or_json_parsing() -> None:
    for relative, source, tree in _python_sources():
        if relative not in PROVIDER_FACADES:
            continue
        assert "LangChainStructuredGateway" in source
        assert "/chat/completions" not in source
        assert "json.loads" not in source
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "post", "put", "patch", "request"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"client", "httpx", "requests"}
            for node in ast.walk(tree)
        )


def test_application_has_no_raw_ai_chat_completion_endpoint() -> None:
    violations = [
        str(relative)
        for relative, source, _tree in _python_sources()
        if "/chat/completions" in source or "/responses" in source
    ]
    assert violations == []
