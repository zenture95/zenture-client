"""Scenarios 1 and 2: explicit RFC 8628 device login and the browser-only default (D-36)."""

from __future__ import annotations

import logging
import socket
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from auth_harness import Environment, FakeClock, browser_opener, runtime_for

import zenture.auth
from zenture._auth import login as login_module
from zenture._auth.errors import (
    AuthorizationRequired,
    AuthUnavailable,
    LoginCancelled,
)
from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import MemoryStore, RecordKey, RecordStore
from zenture._mcp.client import AsyncMcpClient

if TYPE_CHECKING:
    from pathlib import Path


class Shown:
    """Presenter double: records exactly what the user would be shown."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, prompt: Any) -> None:
        self.prompts.append((prompt.verification_uri, prompt.user_code))


def _device_login(
    env: Environment,
    store: RecordStore,
    lock_dir: Path,
    clock: FakeClock,
    shown: Shown,
    **kw: Any,
) -> Any:
    opener, _ = browser_opener()
    runtime = runtime_for(
        store, lock_dir, opener, presenter=shown, clock=clock, sleep=clock.sleep, **kw
    )
    return LoginFlow(runtime).login(device=True, endpoint=env.endpoint)


def _key(env: Environment) -> RecordKey:
    return RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID)


def test_device_login_polls_with_interval_and_slow_down_then_stores_like_browser_login(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    clock, shown = FakeClock(), Shown()
    env.issuer.device_script = ["authorization_pending", "slow_down", "approve"]

    session = _device_login(env, store, lock_dir, clock, shown)

    request = env.issuer.device_requests[0]
    assert request == {
        "client_id": "zenture-client",
        "scope": "openid mcp:run offline_access",
        "resource": env.mcp.resource,
    }
    # The user sees the plain verification URI and the code as XXXX-XXXX, nothing else.
    assert shown.prompts == [(f"{env.issuer.issuer}/device", "WDJB-MJHT")]
    polls = env.issuer.device_polls
    assert len(polls) == 3
    assert {p["device_code"] for p in polls} == {polls[0]["device_code"]}
    assert all(
        p["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
        and p["client_id"] == CLIENT_ID
        and p["resource"] == env.mcp.resource
        for p in polls
    )
    assert clock.sleeps == [5, 5, 10]  # slow_down adds five seconds for every later poll
    record = store.load(_key(env))
    assert record is not None
    assert record.state == "ready"
    assert record.binding == session.binding
    assert session.binding.sub == "user-1"


@pytest.mark.asyncio
async def test_device_login_session_reaches_the_mcp_server_without_another_refresh(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    clock = FakeClock()
    env.issuer.device_script = ["approve"]
    opener, _ = browser_opener()
    runtime = runtime_for(
        store, lock_dir, opener, presenter=Shown(), clock=clock, sleep=clock.sleep
    )

    session = await LoginFlow(runtime).login_async(device=True, endpoint=env.endpoint)

    async with AsyncMcpClient.connect(env.endpoint, session=session) as client:
        assert sorted(await client.list_tools()) == ["echo", "ping"]
    assert env.issuer.refresh_requests == []


def test_the_complete_verification_uri_and_device_code_never_reach_the_user_or_logs(
    env: Environment,
    store: RecordStore,
    lock_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock, shown = FakeClock(), Shown()
    env.issuer.device_script = ["approve"]

    _device_login(env, store, lock_dir, clock, shown)

    device_code = env.issuer.device_polls[0]["device_code"]
    shown_text = " ".join(part for prompt in shown.prompts for part in prompt)
    assert "user_code=" not in shown_text
    assert device_code not in shown_text
    assert "WDJBMJHT" not in caplog.text
    assert "WDJB-MJHT" not in caplog.text
    assert device_code not in caplog.text


def test_default_presenter_prints_uri_and_code_to_the_terminal_only(
    env: Environment,
    store: RecordStore,
    lock_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()
    env.issuer.device_script = ["approve"]
    opener, _ = browser_opener()
    runtime = runtime_for(store, lock_dir, opener, clock=clock, sleep=clock.sleep)

    LoginFlow(runtime).login(device=True, endpoint=env.endpoint)

    captured = capsys.readouterr()
    assert f"{env.issuer.issuer}/device" in captured.err
    assert "WDJB-MJHT" in captured.err
    assert "user_code=" not in captured.err + captured.out


def test_access_denied_is_a_cancellation_and_stores_nothing(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    env.issuer.device_script = ["authorization_pending", "access_denied"]

    with pytest.raises(LoginCancelled):
        _device_login(env, store, lock_dir, FakeClock(), Shown())

    assert store.load(_key(env)) is None
    assert len(env.issuer.device_polls) == 2


@pytest.mark.parametrize(
    ("answer", "code"),
    [("expired_token", "device_code_expired"), ("invalid_grant", "device_code_invalid")],
)
def test_terminal_poll_errors_require_starting_again(
    env: Environment, store: RecordStore, lock_dir: Path, answer: str, code: str
) -> None:
    env.issuer.device_script = [answer]

    with pytest.raises(AuthorizationRequired) as raised:
        _device_login(env, store, lock_dir, FakeClock(), Shown())

    assert raised.value.code == code
    assert store.load(_key(env)) is None
    assert len(env.issuer.device_polls) == 1


def test_polling_stops_at_the_600_second_deadline(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    clock = FakeClock()
    env.issuer.device_expires_in = 900  # a server may not stretch the client-side bound

    with pytest.raises(AuthorizationRequired) as raised:
        _device_login(env, store, lock_dir, clock, Shown())

    assert raised.value.code == "device_code_expired"
    assert sum(clock.sleeps) <= 600
    assert len(env.issuer.device_polls) == 119  # 600 s at a 5 s interval, deadline exclusive
    assert store.load(_key(env)) is None


def test_keyboard_interrupt_stops_polling_without_a_server_cancel_call(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    clock = FakeClock()

    def interrupted(seconds: float) -> None:
        clock.sleep(seconds)
        if len(env.issuer.device_polls) == 1:
            raise KeyboardInterrupt

    opener, _ = browser_opener()
    runtime = runtime_for(
        store, lock_dir, opener, presenter=Shown(), clock=clock, sleep=interrupted
    )

    with pytest.raises(LoginCancelled) as raised:
        LoginFlow(runtime).login(device=True, endpoint=env.endpoint)

    assert raised.value.code == "device_login_interrupted"
    assert len(env.issuer.device_polls) == 1  # polling stopped, nothing else was sent
    assert len(env.issuer.device_requests) == 1
    assert store.load(_key(env)) is None


@pytest.mark.parametrize("lost", ["drop", "server_error"])
def test_a_lost_token_response_reports_unknown_outcome_and_never_repolls(
    env: Environment, store: RecordStore, lock_dir: Path, lost: str
) -> None:
    env.issuer.device_script = ["authorization_pending", lost, "approve"]

    with pytest.raises(AuthorizationRequired) as raised:
        _device_login(env, store, lock_dir, FakeClock(), Shown())

    assert raised.value.code == "device_outcome_unknown"
    assert raised.value.next_action == "login"
    assert len(env.issuer.device_polls) == 2  # the consumed code is not polled again
    assert store.load(_key(env)) is None


def test_device_login_requires_an_advertised_device_endpoint(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    env.issuer.metadata_overrides = {"device_authorization_endpoint": None}

    with pytest.raises(AuthUnavailable) as raised:
        _device_login(env, store, lock_dir, FakeClock(), Shown())

    assert raised.value.code == "device_login_unsupported"
    assert env.issuer.device_requests == []


def test_public_login_accepts_device_true(
    env: Environment,
    store: RecordStore,
    lock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, shown = FakeClock(), Shown()
    env.issuer.device_script = ["approve"]
    opener, _ = browser_opener()
    runtime = runtime_for(store, lock_dir, opener, presenter=shown, clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(login_module, "default_runtime", lambda: runtime)

    session = zenture.auth.login(device=True, endpoint=env.endpoint)

    assert session.binding.sub == "user-1"
    assert shown.prompts


# -- D-36: browser is the default; failure to use it never falls back silently ---------------


def test_browser_that_cannot_open_points_to_device_login_without_starting_one(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    runtime = runtime_for(store, lock_dir, lambda _url: False)

    with pytest.raises(AuthUnavailable) as raised:
        LoginFlow(runtime).login(endpoint=env.endpoint)

    assert raised.value.code == "browser_unavailable"
    assert raised.value.next_action == "use_device_login"
    assert env.issuer.device_requests == []


def test_listener_that_cannot_bind_points_to_device_login_without_starting_one(
    env: Environment,
    store: RecordStore,
    lock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self: socket.socket, *_args: object) -> None:
        raise OSError("no bind")

    monkeypatch.setattr(socket.socket, "bind", refuse)
    opener, opened = browser_opener()
    runtime = runtime_for(store, lock_dir, opener)

    with pytest.raises(AuthUnavailable) as raised:
        LoginFlow(runtime).login(endpoint=env.endpoint)

    assert raised.value.code == "loopback_unavailable"
    assert raised.value.next_action == "use_device_login"
    assert opened == []
    assert env.issuer.device_requests == []


def test_session_only_device_login_never_touches_the_native_store(
    env: Environment, lock_dir: Path
) -> None:
    clock = FakeClock()
    env.issuer.device_script = ["approve"]

    def forbidden() -> RecordStore:
        raise AssertionError("native store used")

    opener, _ = browser_opener()
    runtime = runtime_for(
        MemoryStore(), lock_dir, opener, presenter=Shown(), clock=clock, sleep=clock.sleep
    )
    runtime = replace(runtime, native_store=forbidden)

    session = LoginFlow(runtime).login(device=True, session_only=True, endpoint=env.endpoint)

    assert session.binding.connection_id == "conn-1"
