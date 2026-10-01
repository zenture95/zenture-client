"""Documentation, examples and CI stay consistent with the implemented public auth surface."""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from zenture import auth, cli

ROOT = Path(__file__).resolve().parents[2]
USER_DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "authentication.md",
    ROOT / "docs" / "mcp-client.md",
]
ALL_DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]
EXAMPLES = [
    ROOT / "examples" / "mcp_async_login.py",
    ROOT / "examples" / "mcp_sync_stored_login.py",
    ROOT / "examples" / "mcp_device_login.py",
]
TEXTS = [*ALL_DOCS, *EXAMPLES]
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
COMMAND = re.compile(r"zenture auth (login|status|clear)((?: --[a-z][a-z-]*)*)")
FLAG = re.compile(r"--[a-z][a-z-]*")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@cache
def _help(*words: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "zenture.cli", *words, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    return result.stdout


def _python_blocks(text: str) -> list[str]:
    return [body for lang, body in FENCE.findall(text) if lang in {"python", "py"}]


def test_documented_cli_commands_and_flags_exist_in_the_help() -> None:
    documented: set[tuple[str, str]] = set()
    for path in TEXTS:
        for line in _read(path).splitlines():
            for command, tail in COMMAND.findall(line):
                documented.add((command, ""))
                documented.update((command, flag) for flag in FLAG.findall(tail))
    commands = {command for command, _ in documented}
    assert commands == {"login", "status", "clear"}
    assert {("login", "--device"), ("login", "--session-only")} <= documented
    assert "login|status|clear" in _help("auth")
    for command, flag in documented:
        assert command in _help("auth")
        if flag:
            assert flag in _help("auth", command), (command, flag)


def test_user_docs_document_every_exit_code_and_error_with_its_next_action() -> None:
    codes = {
        cli.EXIT_OK,
        cli.EXIT_NOT_LOGGED_IN,
        cli.EXIT_USAGE,
        cli.EXIT_AUTHORIZATION_REQUIRED,
        cli.EXIT_UNAVAILABLE,
        cli.EXIT_STORE_UNAVAILABLE,
        cli.EXIT_CANCELLED,
        cli.EXIT_PERMISSION_DENIED,
        cli.EXIT_INTERRUPTED,
    }
    readme = _read(ROOT / "README.md")
    for code in codes:
        assert re.search(rf"^\| `{code}` \|", readme, re.MULTILINE), code
    for name in (
        "AuthorizationRequired",
        "AuthUnavailable",
        "SecureStoreUnavailable",
        "PermissionDenied",
        "LoginCancelled",
    ):
        assert hasattr(auth, name)
        assert re.search(rf"^\| `{name}` \|", readme, re.MULTILINE), name


def test_documented_behavior_matches_the_implemented_surface() -> None:
    readme = _read(ROOT / "README.md")
    for claim in (
        "pip install zenture",
        "Keychain",
        "not yet verified",
        "one stored account",
        "zenture auth login --device",
        "zenture auth login --session-only",
        "zenture auth status",
        "zenture auth clear",
        "Zugriff & Sicherheit",
        "Verbindungen",
        "Python 3.14",
        "record_run_outcome",
    ):
        assert claim in readme, claim
    assert "attach_artifact" in readme
    flows = _read(ROOT / "docs" / "authentication.md")
    for claim in ("10 minutes", "log in again", "loopback", "PKCE", "refresh"):
        assert claim in flows, claim
    mcp = _read(ROOT / "docs" / "mcp-client.md")
    assert "idempotency_key" in mcp
    assert re.search(r"idempotency_key.{0,200}not available", mcp, re.DOTALL)
    assert "OAuth implementation" not in mcp
    assert "zenture._mcp" not in mcp
    assert (
        "OAuth discovery,\nlogin, refresh and credential storage are not implemented" not in readme
    )


@pytest.mark.parametrize("path", TEXTS, ids=lambda p: p.name)
def test_documents_and_examples_use_only_public_modules(path: Path) -> None:
    text = _read(path)
    assert re.search(r"zenture\._|zenture/_", text) is None or path.name == "AGENTS.md"
    if path.name == "AGENTS.md":
        assert re.search(r"(from|import) zenture\._", text) is None
    blocks = [text] if path.suffix == ".py" else _python_blocks(text)
    for block in blocks:
        for node in ast.walk(ast.parse(block)):
            modules: list[tuple[str, list[str]]] = []
            if isinstance(node, ast.Import):
                modules = [(alias.name, []) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [(node.module, [alias.name for alias in node.names])]
            for module, names in modules:
                if module.split(".")[0] != "zenture":
                    continue
                assert not any(part.startswith("_") for part in module.split(".")), module
                imported = importlib.import_module(module)
                for name in names:
                    assert hasattr(imported, name), f"{module}.{name}"


def test_mcp_docs_name_the_six_tools_and_both_peers() -> None:
    from zenture.mcp import AsyncMcpClient, McpClient

    tools = ("run", "attach_artifact", "list_runs", "get_run", "cancel_run", "record_run_outcome")
    mcp = _read(ROOT / "docs" / "mcp-client.md")
    for tool in tools:
        assert f"`{tool}`" in mcp
        assert hasattr(McpClient, tool)
        assert hasattr(AsyncMcpClient, tool)
    for peer in ("McpClient", "AsyncMcpClient", "login_async"):
        assert peer in mcp or peer in _read(ROOT / "README.md")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_import_quietly_and_print_help_without_network(path: Path) -> None:
    source = _read(path)
    assert 'if __name__ == "__main__":' in source
    guard = (
        "import socket, webbrowser, sys, runpy\n"
        "def deny(*a, **k):\n    raise AssertionError('side effect at import')\n"
        "socket.socket.connect = deny\nwebbrowser.open = deny\n"
        f"sys.argv = [{path.name!r}, '--help']\n"
        f"runpy.run_path({str(path)!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", guard],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
    )
    assert result.returncode == 0, result.stderr[-300:]
    assert "usage:" in result.stdout.lower()
    assert re.search(r"(access|bearer|api)_?token\s*=\s*['\"]", source) is None


def test_agent_entry_states_safe_usage_rules_and_contribution_commands() -> None:
    agents = _read(ROOT / "AGENTS.md")
    for rule in (
        "zenture.auth",
        "zenture.mcp",
        "zenture auth status",
        "never print",
        "no implicit login",
        "no private imports",
        "python3 -m pytest",
    ):
        assert rule in agents, rule


def test_pyproject_supports_python_3_14_without_an_upper_cap() -> None:
    import tomllib

    project = tomllib.loads(_read(ROOT / "pyproject.toml"))["project"]
    assert project["requires-python"] == ">=3.11"
    for minor in ("11", "12", "13", "14"):
        assert f"Programming Language :: Python :: 3.{minor}" in project["classifiers"]


def test_development_workflow_tests_the_full_platform_matrix_and_native_keyring_is_macos_only_and_opt_in() -> (
    None
):
    text = _read(ROOT / ".github" / "workflows" / "development.yml")
    assert "\t" not in text
    testing = text.split("\n  testing:\n", 1)[1].split("\n  native-keyring:\n", 1)[0]
    assert re.search(r'python-version: \["3\.11", "3\.12", "3\.13", "3\.14"\]', testing)
    assert re.search(r"os: \[ubuntu-latest, macos-latest, windows-latest\]", testing)
    assert "runs-on: ${{ matrix.os }}" in testing
    assert "native_keyring" not in testing
    native = text.split("\n  native-keyring:\n", 1)[1].split("\n  governance:\n", 1)[0]
    assert "if: github.event_name == 'workflow_dispatch'" in native
    assert "-m native_keyring" in native
    assert "continue-on-error: false" in native
    # Only a macOS native-keychain test exists; no Windows/Linux native evidence is claimed.
    assert "os: [" not in native
    assert "matrix" not in native
    assert "runs-on: macos-latest" in native
    assert "ubuntu-latest" not in native
    assert "windows-latest" not in native
    assert "Windows and Linux native tests do not exist yet" in text


def test_ci_and_python_support_wording_claims_nothing_that_was_not_run() -> None:
    readme = _read(ROOT / "README.md")
    changelog = _read(ROOT / "CHANGELOG.md")
    assert "declared and tested in CI" not in readme
    assert "Python 3.14 is declared; the CI matrix covers it but has not yet run" in readme
    assert "run the CI matrix" not in changelog
    assert "configured" in changelog


def test_authentication_guide_exit_codes_match_the_readme() -> None:
    guide = _read(ROOT / "docs" / "authentication.md")
    readme = _read(ROOT / "README.md")
    for line in (
        "`6` cancelled or denied, `7` permission denied",
        "`130` interrupted (Ctrl+C, also during a device login)",
    ):
        assert line.replace("\n", " ") in " ".join(guide.split())
    assert "| `6` | login cancelled (denied or declined) |" in readme


def test_refresh_safety_docs_describe_mark_then_delete_on_the_next_attempt() -> None:
    text = _read(ROOT / "docs" / "authentication.md")
    section = text.split("### Refresh safety", 1)[1].split("###", 1)[0]
    assert "rotation_outcome_unknown" in section
    assert "marks" in section
    assert "next attempt" in section


def test_agent_contribution_rule_allows_the_documented_auth_and_mcp_apis() -> None:
    agents = _read(ROOT / "AGENTS.md")
    assert "documented auth/MCP public APIs" in agents
