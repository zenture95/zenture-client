"""Public documentation, example, and onboarding hygiene tests."""

from __future__ import annotations

import ast
import importlib.util
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]

DOC_FILES = [
    ROOT / "docs" / "authentication.md",
    ROOT / "docs" / "mcp-client.md",
    ROOT / "docs" / "idempotency.md",
    ROOT / "docs" / "async-operations.md",
    ROOT / "docs" / "errors.md",
    ROOT / "docs" / "rate-limits.md",
    ROOT / "docs" / "pagination.md",
    ROOT / "docs" / "account-reads.md",
    ROOT / "docs" / "security.md",
    ROOT / "docs" / "api-reference.md",
    ROOT / "docs" / "sdk-call-reference.md",
    ROOT / "docs" / "response-shapes.md",
    ROOT / "docs" / "release.md",
]
EXAMPLE_FILES = [
    ROOT / "examples" / "helloworld.py",
    ROOT / "examples" / "list_models.py",
    ROOT / "examples" / "chat_single.py",
    ROOT / "examples" / "chat_multi.py",
    ROOT / "examples" / "input_wizard.py",
    ROOT / "examples" / "evaluate.py",
    ROOT / "examples" / "operation_polling.py",
    ROOT / "examples" / "pagination.py",
    ROOT / "examples" / "idempotency.py",
    ROOT / "examples" / "error_handling.py",
    ROOT / "examples" / "async_chat.py",
    ROOT / "examples" / "async_evaluate.py",
    ROOT / "examples" / "account_status.py",
    ROOT / "examples" / "end_to_end_chat_evaluation.py",
]
MCP_EXAMPLE_FILES = [
    ROOT / "examples" / "mcp_async_login.py",
    ROOT / "examples" / "mcp_sync_stored_login.py",
    ROOT / "examples" / "mcp_device_login.py",
]
PUBLIC_TEXT_FILES = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    *DOC_FILES,
    *EXAMPLE_FILES,
    *MCP_EXAMPLE_FILES,
]
FORBIDDEN_PATTERNS = [
    re.compile("zt_" + "live_"),
    re.compile("zt_" + "test_"),
    re.compile(r"\blocalhost\b", re.IGNORECASE),
    re.compile(r"\b127\.0\.0\.1\b"),
    re.compile(r"\[::1\]"),
    re.compile(r"\bapi-int\.zenture\.app\b", re.IGNORECASE),
    re.compile(r"\bapi-[a-z0-9-]+\.zenture\.app\b", re.IGNORECASE),
    re.compile(r"api_key\s*=\s*['\"]"),
    re.compile(r"base_url\s*=\s*input\s*\("),
    re.compile(r"/Users/[^\s)]*zenture", re.IGNORECASE),
    re.compile(r'["\']public-model-[^"\']+["\']'),
    re.compile(r'mode\s*=\s*["\']agentic["\']', re.IGNORECASE),
    re.compile(r"\.agentic\b", re.IGNORECASE),
    re.compile(r"\bagentic\.run\b", re.IGNORECASE),
]
TEST_DOCS_EXAMPLES_KEY = "zt_" + "test_" + "docs_examples"
NON_PROD_OVERRIDE_ENV = "ZENTURE_SDK_ALLOW_NON_PROD_BASE_URL"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_example(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_zenture_example_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operation_payload() -> dict[str, str]:
    return {"operation_id": "op_example", "status": "succeeded"}


def _model_payload(mode: str | None) -> dict[str, object]:
    if mode == "multi":
        return {
            "models": [
                {
                    "id": "model_public_multi_a",
                    "display_name": "Public Multi A",
                    "modes": ["multi"],
                    "capabilities": [],
                    "is_default": False,
                    "is_available": True,
                    "cost_class": "standard",
                },
                {
                    "id": "model_public_multi_b",
                    "display_name": "Public Multi B",
                    "modes": ["multi"],
                    "capabilities": [],
                    "is_default": False,
                    "is_available": True,
                    "cost_class": "standard",
                },
            ]
        }
    return {
        "models": [
            {
                "id": "model_public_single",
                "display_name": "Public Single",
                "modes": ["single"],
                "capabilities": [],
                "is_default": True,
                "is_available": True,
                "cost_class": "standard",
            }
        ]
    }


def _example_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "GET" and path == "/v1/helloworld":
        return httpx.Response(200, text="# Hello from zenture")
    if request.method == "GET" and path == "/v1/models":
        return httpx.Response(200, json=_model_payload(request.url.params.get("mode")))
    if request.method == "GET" and path == "/v1/chats":
        return httpx.Response(
            200,
            json={
                "chats": [
                    {
                        "chat_id": "chat_example",
                        "created_at": "2026-06-15T10:00:00Z",
                        "title": "Example chat",
                        "updated_at": None,
                    }
                ],
                "next_cursor": None,
            },
        )
    if request.method == "GET" and path == "/v1/wallet":
        return httpx.Response(
            200,
            json={
                "plan": "free",
                "status": "active",
                "credits_available": {"amount": "123.45", "unit": "credits"},
                "current_period_end": None,
            },
        )
    if request.method == "GET" and path == "/v1/usage":
        return httpx.Response(
            200,
            json={"scope": request.url.params.get("scope", "api"), "operation_count": 3},
        )
    if request.method == "GET" and path == "/v1/limits":
        return httpx.Response(
            200,
            json={
                "routes": {
                    "POST /v1/evaluate": {
                        "auth_mode": "api_token",
                        "scopes": ["evaluation:run"],
                        "cost_class": "evaluation_write",
                        "idempotency_required": True,
                        "cors_policy": "server_only",
                        "max_body_bytes": 100000,
                        "rate_limit_per_minute": 30,
                    }
                },
                "operation_statuses": [
                    "queued",
                    "running",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                ],
            },
        )
    if request.method == "POST" and path in {
        "/v1/chat",
        "/v1/input-wizard",
        "/v1/evaluate",
    }:
        return httpx.Response(200, json={"operation_id": "op_example", "status": "queued"})
    if request.method == "GET" and path == "/v1/operations/op_example":
        return httpx.Response(200, json=_operation_payload())
    raise AssertionError(f"Unexpected example request: {request.method} {request.url}")


def test_readme_is_beta_ready_and_public_safe() -> None:
    text = _read(ROOT / "README.md")
    assert '<img src="https://ai.zenture.app/logo.svg"' in text
    assert "docs/assets/zenture-logo.svg" not in text
    required = [
        "pip install zenture",
        "Python 3.11",
        "server-side only",
        "ZENTURE_API_KEY",
        "https://ai.zenture.app/profile?tab=api-tokens",
        "https://www.zenture.app/developers",
        "docs/sdk-call-reference.md",
        "ZentureClient.from_env()",
        "AsyncZentureClient.from_env()",
        "helloworld",
        'models.list(mode="single")',
        'models.list(mode="multi")',
        'mode="single"',
        'mode="multi"',
        "input_wizard.run",
        "evaluations.run",
        "external_id",
        "model_response_id",
        "operations.wait",
        "wallet.get",
        'usage.get(scope="api")',
        "limits.get",
        "chat.iter",
        "limit=50",
        "cursor",
        "next_cursor",
        "idempotency_key",
        "ZentureAPIError",
        "Retry-After",
        "https://api.zenture.app",
        "API-token management surface",
        "python3 -m pytest",
        "SECURITY.md",
        "Apache License 2.0",
    ]

    for item in required:
        assert item in text


def test_docs_and_agents_exist_with_required_public_onboarding_content() -> None:
    for path in DOC_FILES:
        assert path.exists(), path
        assert len(_read(path).strip()) > 400

    agents = ROOT / "AGENTS.md"
    assert agents.exists()
    agents_text = _read(agents)
    required = [
        "public clients",
        "private resource implementation classes",
        "_transport",
        "_contract",
        "Contract drift",
        "https://ai.zenture.app/profile?tab=api-tokens",
        "https://www.zenture.app/developers",
        "python3 -m ruff format --check .",
        "python3 -m twine check dist/*",
        "no token logging",
        "no API-token-management SDK resource",
        "no internal `_contract` top-level exports",
    ]
    for item in required:
        assert item in agents_text

    combined_docs = "\n".join(_read(path) for path in [ROOT / "README.md", *DOC_FILES])
    assert "server-side only" in combined_docs
    assert "webapp" in combined_docs.lower()
    assert "API-token management surface" in combined_docs
    assert "api-" + "int" not in combined_docs
    assert "api-example" not in combined_docs
    assert "https://ai.zenture.app/profile?tab=api-tokens" in combined_docs
    assert "https://www.zenture.app/developers" in combined_docs
    assert "https://www.zenture.app/api-documentation" not in combined_docs


def test_api_reference_covers_current_public_resource_surface() -> None:
    text = "\n".join(
        [
            _read(ROOT / "docs" / "api-reference.md"),
            _read(ROOT / "docs" / "sdk-call-reference.md"),
        ]
    )
    required = [
        "client.helloworld()",
        "client.models.list",
        "client.chat.create",
        "client.chat.create_operation",
        "client.chat.run",
        "client.chat.list",
        "client.chat.iter",
        "client.chat.get",
        "client.chat.messages",
        "client.chat.iter_messages",
        "client.input_wizard.create",
        "client.input_wizard.run",
        "client.evaluations.create",
        "client.evaluations.run",
        "client.evaluations.list",
        "client.evaluations.iter",
        "client.evaluations.get",
        "client.operations.get",
        "client.operations.wait",
        "client.wallet.get",
        'client.usage.get(scope="api")',
        'client.usage.get(scope="all")',
        "client.limits.get",
    ]

    for item in required:
        assert item in text


def test_response_shapes_document_current_public_endpoint_surface() -> None:
    response_shapes = _read(ROOT / "docs" / "response-shapes.md")
    linked_docs = "\n".join(
        _read(path)
        for path in [
            ROOT / "README.md",
            ROOT / "docs" / "api-reference.md",
            ROOT / "docs" / "sdk-call-reference.md",
        ]
    )

    required_routes = [
        "/v1/helloworld",
        "/v1/models",
        "/v1/input-wizard",
        "/v1/chat",
        "/v1/evaluate",
        "/v1/operations/{operation_id}",
        "/v1/chats",
        "/v1/chats/{chat_id}",
        "/v1/chats/{chat_id}/messages",
        "/v1/evaluations",
        "/v1/evaluations/{evaluation_id}",
        "/v1/wallet",
        "/v1/usage",
        "/v1/limits",
    ]
    required_models = [
        "PublicOperationResponse",
        "OperationRunResult",
        "PublicOperationResult",
        "PublicOperationError",
        "PublicModelListResponse",
        "PublicChatCollectionResponse",
        "PublicChatResponse",
        "PublicChatMessagesResponse",
        "PublicEvaluationCollectionResponse",
        "PublicEvaluationResponse",
        "PublicWalletResponse",
        "PublicUsageResponse",
        "LimitsResponse",
    ]

    for route in required_routes:
        assert route in response_shapes
    for model in required_models:
        assert model in response_shapes

    assert "/v1/api-tokens" not in response_shapes
    assert "user_session" not in response_shapes
    assert "Not exposed by this SDK" not in response_shapes
    assert "agentic" in response_shapes.lower()
    assert "response-shapes.md" in linked_docs


def test_examples_exist_compile_and_use_env_clients() -> None:
    for path in EXAMPLE_FILES:
        assert path.exists(), path
        source = _read(path)
        ast.parse(source, filename=str(path))
        assert ".from_env()" in source
        assert 'if __name__ == "__main__":' in source

    async_source = _read(ROOT / "examples" / "async_chat.py")
    assert "asyncio.run(main())" in async_source
    assert "AsyncZentureClient.from_env()" in async_source

    multi_source = _read(ROOT / "examples" / "chat_multi.py")
    assert 'models.list(mode="multi")' in multi_source
    assert "available_models" in multi_source


@pytest.mark.parametrize(
    "example_name",
    [
        "helloworld.py",
        "list_models.py",
        "chat_single.py",
        "chat_multi.py",
        "input_wizard.py",
        "evaluate.py",
        "operation_polling.py",
        "idempotency.py",
        "error_handling.py",
        "pagination.py",
        "account_status.py",
    ],
)
def test_sync_examples_execute_with_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    example_name: str,
) -> None:
    real_client = httpx.Client

    def client_factory(*args: object, **kwargs: Any) -> httpx.Client:
        return real_client(
            *args,
            transport=httpx.MockTransport(_example_response),
            **kwargs,
        )

    monkeypatch.setenv("ZENTURE_API_KEY", TEST_DOCS_EXAMPLES_KEY)
    monkeypatch.setenv("ZENTURE_BASE_URL", "http://localhost")
    monkeypatch.setenv(NON_PROD_OVERRIDE_ENV, "1")
    monkeypatch.setattr(httpx, "Client", client_factory)

    module = _load_example(ROOT / "examples" / example_name)
    main = cast("Callable[[], None]", module.__dict__["main"])
    main()
    assert capsys.readouterr().out


@pytest.mark.parametrize("example_name", ["async_chat.py", "async_evaluate.py"])
@pytest.mark.asyncio
async def test_async_examples_execute_with_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    example_name: str,
) -> None:
    real_client = httpx.AsyncClient

    def client_factory(*args: object, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(
            *args,
            transport=httpx.MockTransport(_example_response),
            **kwargs,
        )

    monkeypatch.setenv("ZENTURE_API_KEY", TEST_DOCS_EXAMPLES_KEY)
    monkeypatch.setenv("ZENTURE_BASE_URL", "http://localhost")
    monkeypatch.setenv(NON_PROD_OVERRIDE_ENV, "1")
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    module = _load_example(ROOT / "examples" / example_name)
    main = cast("Callable[[], Awaitable[None]]", module.__dict__["main"])
    await main()
    assert capsys.readouterr().out


def test_public_docs_examples_and_agents_have_no_forbidden_patterns() -> None:
    for path in PUBLIC_TEXT_FILES:
        assert path.exists(), path
        text = _read(path)
        for pattern in FORBIDDEN_PATTERNS:
            assert pattern.search(text) is None, f"{path} matched {pattern.pattern}"


def test_openapi_artifact_exposes_only_production_server() -> None:
    openapi = _read(ROOT / "openapi" / "zenture-public-api-v1.openapi.json")

    assert "https://api.zenture.app" in openapi
    assert "https://api-" + "int.zenture.app" not in openapi


def test_release_and_development_workflows_run_release_safety_scan() -> None:
    development = _read(ROOT / ".github" / "workflows" / "development.yml")
    release = _read(ROOT / ".github" / "workflows" / "release.yml")

    assert "python scripts/check_release_safety.py" in development
    assert "python scripts/check_release_safety.py" in release


def test_contributing_and_security_are_public_beta_current() -> None:
    contributing = _read(ROOT / "CONTRIBUTING.md")
    security = _read(ROOT / "SECURITY.md")

    assert "planning/bootstrap" not in contributing
    assert "planning/bootstrap" not in security
    assert "private/bootstrap" not in security
    assert "Runtime SDK implementation starts only after" not in contributing
    assert "python3 -m ruff format --check ." in contributing
    assert "python3 -m pyright" in contributing
    assert "python3 -m pytest" in contributing
    assert "python3 -m twine check dist/*" in contributing
    assert "GitHub Security Advisory" in security


def test_sdist_includes_public_docs_examples_and_excludes_private_plan() -> None:
    config = tomllib.loads(_read(ROOT / "pyproject.toml"))
    include = set(config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])

    assert "/docs" in include
    assert "/examples" in include
    assert "/AGENTS.md" in include
    assert "/PLAN_ZENTURE_SDK.md" not in include
