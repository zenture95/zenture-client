"""Scenario 4: the ``zenture`` console script (login, status, clear) over real loopback peers."""

from __future__ import annotations

import io
import socket
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from auth_harness import Environment, FakeClock, browser_opener, runtime_for

from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import RecordKey, StoreError
from zenture.cli import main
from zenture.errors import ZentureMCPDependencyError, ZentureMCPError, ZentureMCPProtocolError

if TYPE_CHECKING:
    from file_store import FileStore

    from zenture._auth.login import Runtime
    from zenture._auth.store import StoredRecord

ROOT = Path(__file__).resolve().parents[3]


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def text(self) -> str:
        return self.out + self.err


def run(argv: list[str], runtime: Runtime, env: Environment | None = None) -> Result:
    out, err = io.StringIO(), io.StringIO()
    endpoint = None if env is None else env.endpoint
    code = main(argv, runtime=runtime, endpoint=endpoint, stdout=out, stderr=err)
    return Result(code, out.getvalue(), err.getvalue())


def _key(env: Environment) -> RecordKey:
    return RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID)


def _runtime(file_store: FileStore, lock_dir: Path, opener: Any = None, **kw: Any) -> Runtime:
    clock = FakeClock()
    chosen = opener or browser_opener()[0]
    return runtime_for(file_store, lock_dir, chosen, clock=clock, sleep=clock.sleep, **kw)


def _stored(file_store: FileStore, env: Environment) -> StoredRecord | None:
    return file_store.load(_key(env))


def _assert_no_credentials(env: Environment, file_store: FileStore, result: Result) -> None:
    record = _stored(file_store, env)
    assert record is not None
    assert record.refresh_token not in result.text
    for token in env.issuer._access:
        assert token not in result.text


# -- auth login ------------------------------------------------------------------------------


def test_login_uses_the_browser_by_default_and_stores_the_authorization(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    opener, opened = browser_opener()

    result = run(["auth", "login"], _runtime(file_store, lock_dir, opener), env)

    assert result.code == 0
    assert len(opened) == 1
    assert env.issuer.device_requests == []
    record = _stored(file_store, env)
    assert record is not None
    assert record.state == "ready"
    assert "stored" in result.out
    _assert_no_credentials(env, file_store, result)


def test_device_login_prints_the_verification_uri_and_formatted_code_to_the_terminal(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    env.issuer.device_script = ["authorization_pending", "approve"]
    opener, opened = browser_opener()

    result = run(["auth", "login", "--device"], _runtime(file_store, lock_dir, opener), env)

    assert result.code == 0
    assert opened == []  # no browser in device mode
    assert f"{env.issuer.issuer}/device" in result.out
    assert "WDJB-MJHT" in result.out
    assert "user_code=" not in result.text
    assert env.issuer.device_polls[0]["device_code"] not in result.text
    assert _stored(file_store, env) is not None
    _assert_no_credentials(env, file_store, result)


def test_session_only_login_verifies_but_persists_nothing_and_says_so(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    opener, opened = browser_opener()

    result = run(["auth", "login", "--session-only"], _runtime(file_store, lock_dir, opener), env)

    assert result.code == 0
    assert len(opened) == 1
    assert _stored(file_store, env) is None
    assert "cannot outlive" in result.out
    assert file_store.attempts == 0


def test_browser_that_cannot_open_suggests_device_login_and_does_not_start_one(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    result = run(["auth", "login"], _runtime(file_store, lock_dir, lambda _url: False), env)

    assert result.code == 4
    assert "zenture auth login --device" in result.err
    assert env.issuer.device_requests == []
    assert _stored(file_store, env) is None


def test_ctrl_c_during_device_login_exits_interrupted_and_states_the_request_simply_expires(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    clock = FakeClock()

    def interrupted(seconds: float) -> None:
        clock.sleep(seconds)
        raise KeyboardInterrupt

    opener, _ = browser_opener()
    runtime = runtime_for(file_store, lock_dir, opener, clock=clock, sleep=interrupted)

    result = run(["auth", "login", "--device"], runtime, env)

    assert result.code == 130  # Ctrl+C exits like browser login and status
    assert "simply expires" in result.err
    assert env.issuer.device_polls == []


def test_denied_device_login_exits_cancelled(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    env.issuer.device_script = ["access_denied"]

    result = run(["auth", "login", "--device"], _runtime(file_store, lock_dir), env)

    assert result.code == 6  # an explicit denial stays "cancelled"
    assert _stored(file_store, env) is None


def test_lost_device_outcome_asks_for_a_new_explicit_login(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    env.issuer.device_script = ["drop"]

    result = run(["auth", "login", "--device"], _runtime(file_store, lock_dir), env)

    assert result.code == 3
    assert "zenture auth login --device" in result.err
    assert len(env.issuer.device_polls) == 1


# -- auth status -----------------------------------------------------------------------------


def test_status_without_a_record_is_not_logged_in(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    result = run(["auth", "status"], _runtime(file_store, lock_dir), env)

    assert result.code == 1
    assert "status: not_logged_in" in result.out
    assert f"issuer: {env.issuer.issuer}" in result.out
    assert env.issuer.refresh_requests == []


def test_status_connected_verifies_by_refresh_and_a_non_activity_probe_without_tokens(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    env.mcp.guard.requests.clear()

    result = run(["auth", "status"], _runtime(file_store, lock_dir), env)

    assert result.code == 0
    assert "status: connected" in result.out
    assert f"issuer: {env.issuer.issuer}" in result.out
    assert f"resource: {env.mcp.resource}" in result.out
    assert "connection: conn-1" in result.out
    assert "owner_epoch=3" in result.out
    assert len(env.issuer.refresh_requests) == 1
    rpc = {entry["rpc"] for entry in env.mcp.guard.requests}
    assert rpc <= {"initialize", "notifications/initialized", "tools/list", ""}
    assert "tools/call" not in rpc
    _assert_no_credentials(env, file_store, result)


def test_status_authorization_required_when_the_authority_is_rejected(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    env.issuer.refresh_behaviors = ["invalid_grant"]

    result = run(["auth", "status"], _runtime(file_store, lock_dir), env)

    assert result.code == 3
    assert "status: authorization_required" in result.out
    assert "zenture auth login" in result.out


def test_status_unavailable_when_the_issuer_cannot_be_reached(
    file_store: FileStore, lock_dir: Path
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    out, err = io.StringIO(), io.StringIO()

    code = main(
        ["auth", "status"],
        runtime=_runtime(file_store, lock_dir),
        endpoint=f"http://127.0.0.1:{port}/",
        stdout=out,
        stderr=err,
    )

    assert code == 4
    assert "status: unavailable" in out.getvalue()


def test_status_unavailable_when_the_mcp_probe_is_refused_with_a_server_error(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    env.mcp.guard.forced = [503]

    result = run(["auth", "status"], _runtime(file_store, lock_dir), env)

    assert result.code == 4
    assert "status: unavailable" in result.out


@pytest.mark.parametrize("mode", ["server_error", "refused"])
def test_login_with_a_stored_record_and_an_mcp_outage_exits_unavailable(
    env: Environment,
    file_store: FileStore,
    lock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    if mode == "server_error":
        env.mcp.guard.forced = [503] * 20
    else:

        class RefusedAfterDiscovery(LoginFlow):
            def prepare(self, *, session_only: bool, endpoint: str | None) -> Any:
                prepared = super().prepare(session_only=session_only, endpoint=endpoint)
                env.mcp.stop()
                return prepared

        monkeypatch.setattr("zenture.cli.LoginFlow", RefusedAfterDiscovery)
    opener, opened = browser_opener()

    result = run(["auth", "login"], _runtime(file_store, lock_dir, opener), env)

    assert result.code == 4
    assert "later" in result.err
    assert "Traceback" not in result.text
    assert opened == []
    assert len(env.issuer.auth_requests) == 1
    assert _stored(file_store, env) is not None


class _ProbeFails(LoginFlow):
    def __init__(self, runtime: Any, error: Exception) -> None:
        super().__init__(runtime)
        self._error = error

    async def probe(self, prepared: Any) -> None:
        raise self._error


_NON_OUTAGE_ERRORS = [ZentureMCPDependencyError(), ZentureMCPProtocolError("invalid_tool_catalog")]


@pytest.mark.parametrize("error", _NON_OUTAGE_ERRORS, ids=["dependency", "protocol"])
def test_login_probe_dependency_or_protocol_error_is_not_reported_as_try_later(
    env: Environment,
    file_store: FileStore,
    lock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: ZentureMCPError,
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    monkeypatch.setattr("zenture.cli.LoginFlow", lambda runtime: _ProbeFails(runtime, error))
    opener, opened = browser_opener()

    result = run(["auth", "login"], _runtime(file_store, lock_dir, opener), env)

    assert result.code == 4
    assert "later" not in result.err
    assert error.code in result.err
    assert "Traceback" not in result.text
    assert opened == []
    assert len(env.issuer.auth_requests) == 1
    assert _stored(file_store, env) is not None


@pytest.mark.parametrize("error", _NON_OUTAGE_ERRORS, ids=["dependency", "protocol"])
def test_status_probe_dependency_or_protocol_error_reports_its_own_reason(
    env: Environment,
    file_store: FileStore,
    lock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: ZentureMCPError,
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)

    async def failing(_self: Any, _prepared: Any) -> None:
        raise error

    monkeypatch.setattr(LoginFlow, "probe", failing)

    result = run(["auth", "status"], _runtime(file_store, lock_dir), env)

    assert result.code == 4
    assert f"reason: {error.code}" in result.out
    assert "mcp_unavailable" not in result.text
    assert "Traceback" not in result.text
    assert _stored(file_store, env) is not None


def test_status_store_unavailable_when_the_credential_store_cannot_be_read(
    env: Environment, lock_dir: Path
) -> None:
    class Broken:
        def load(self, _identity: RecordKey) -> None:
            raise StoreError

        def save(self, _record: object) -> None:
            raise StoreError

        def delete(self, _identity: RecordKey) -> bool:
            raise StoreError

    opener, _ = browser_opener()
    runtime = runtime_for(Broken(), lock_dir, opener)

    result = run(["auth", "status"], runtime, env)

    assert result.code == 5
    assert "status: store_unavailable" in result.out


# -- auth clear ------------------------------------------------------------------------------


def test_clear_deletes_only_the_local_record_and_points_to_remote_revocation(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    requests_before = (len(env.issuer.token_requests), len(env.mcp.guard.requests))

    result = run(["auth", "clear"], _runtime(file_store, lock_dir), env)

    assert result.code == 0
    assert _stored(file_store, env) is None
    assert "Zugriff & Sicherheit" in result.out
    assert "Verbindungen" in result.out
    assert requests_before == (len(env.issuer.token_requests), len(env.mcp.guard.requests))
    assert not env.issuer.grant_revoked()


def test_clear_works_offline_and_deletes_without_any_network_request(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    run(["auth", "login"], _runtime(file_store, lock_dir), env)
    env.stop()  # issuer and MCP server are gone: discovery is impossible

    result = run(["auth", "clear"], _runtime(file_store, lock_dir), env)

    assert result.code == 0
    assert _stored(file_store, env) is None
    assert "No stored authorization" not in result.out


def test_clear_without_a_record_is_a_successful_no_op(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    result = run(["auth", "clear"], _runtime(file_store, lock_dir), env)

    assert result.code == 0
    assert "No stored authorization" in result.out


# -- surface ---------------------------------------------------------------------------------


def test_help_documents_the_commands_flags_and_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["--help"], ["auth", "--help"], ["auth", "login", "--help"]):
        with pytest.raises(SystemExit) as exited:
            main(argv)
        assert exited.value.code == 0
    text = capsys.readouterr().out
    for expected in (
        "login",
        "status",
        "clear",
        "--device",
        "--session-only",
        "not_logged_in",
        "store_unavailable",
        "exit codes",
    ):
        assert expected in text


@pytest.mark.parametrize("argv", [["run"], ["server"], ["auth", "revoke"], []])
def test_no_run_server_or_revoke_commands_exist(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(argv)

    assert exited.value.code == 2


def test_console_script_points_to_the_cli_entry_point() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["scripts"] == {"zenture": "zenture.cli:main"}
