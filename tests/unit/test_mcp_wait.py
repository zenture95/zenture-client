"""Bounded same-Run observation through public MCP peers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

import pytest

from zenture.errors import ZentureMCPError, ZentureMCPProtocolError, ZenturePollingTimeoutError
from zenture.mcp import AsyncMcpClient, McpClient

RUN_ID = "run_waittest"
TERMINAL = ("completed", "succeeded", "failed", "cancelled", "expired", "budget_exhausted")


def result(status: str, run_id: str = RUN_ID) -> dict[str, object]:
    return {
        "run_id": run_id,
        "generation": 1,
        "family": "knowledge",
        "work_type": "answer",
        "profile": "standard",
        "status": status,
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:00:01Z",
        "artifact_refs": [],
    }


class Port:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_tools(self) -> object:
        return {"tools": []}

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.calls.append((name, dict(arguments)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class AsyncPort(Port):
    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        return super().call_tool(name, arguments)


@pytest.mark.parametrize(
    "status",
    [
        *TERMINAL,
        "created",
        "queued",
        "running",
        "waiting_for_dependency",
        "partially_complete",
        "cancel_requested",
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_wait_stops_with_same_id_summary_only(status: str, asynchronous: bool) -> None:
    port = AsyncPort(result(status)) if asynchronous else Port(result(status))
    client = AsyncMcpClient(cast("Any", port)) if asynchronous else McpClient(port)
    wait = cast("Any", getattr(client, "wait_run", None))
    assert callable(wait), "public bounded wait is missing"
    stop_on = () if status in TERMINAL else (status,)
    read = (
        asyncio.run(cast("AsyncMcpClient", client).wait_run(RUN_ID, 5, stop_on=stop_on))
        if asynchronous
        else cast("McpClient", client).wait_run(RUN_ID, 5, stop_on=stop_on)
    )
    assert read.run.run_id == RUN_ID
    assert read.run.status == status
    assert port.calls == [("get_run", {"run_id": RUN_ID, "view": "summary", "replay_limit": 50})]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, None])
def test_wait_rejects_invalid_budget_before_io(timeout: object) -> None:
    port = Port()
    wait = getattr(McpClient(port), "wait_run", None)
    assert callable(wait), "public bounded wait is missing"
    with pytest.raises(ValueError, match=r"timeout|stop_on"):
        wait(RUN_ID, timeout)
    assert port.calls == []


def test_wait_rejects_unknown_stop_and_wrong_identity() -> None:
    port = Port(result("completed", "run_foreign"))
    client = cast("Any", McpClient(port))
    assert callable(getattr(client, "wait_run", None)), "public bounded wait is missing"
    with pytest.raises(ValueError, match=r"timeout|stop_on"):
        client.wait_run(RUN_ID, 3, stop_on=("invented",))
    assert port.calls == []
    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        client.wait_run(RUN_ID, 3)


def test_wait_preserves_permanent_error() -> None:
    error = ZentureMCPError("denied", retryable=False)
    client = cast("Any", McpClient(Port(error)))
    assert callable(getattr(client, "wait_run", None)), "public bounded wait is missing"
    with pytest.raises(ZentureMCPError) as raised:
        client.wait_run(RUN_ID, 3)
    assert raised.value is error


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("hint", "expected"), [(None, [1.0, 2.0]), (3, [3.0, 2.0]), (20, [10.0])])
def test_wait_retry_minimum_and_deadline(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, hint: int | None, expected: list[float]
) -> None:

    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    async def async_sleep(seconds: float) -> None:
        sleep(seconds)

    monkeypatch.setattr("zenture._mcp.wait.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("zenture._mcp.wait.time.sleep", sleep)
    monkeypatch.setattr("zenture._mcp.wait.asyncio.sleep", async_sleep)
    error = ZentureMCPError(
        "busy", retryable=True, retry_after_seconds=hint, request_id="request_safe"
    )
    responses = (error, result("running"), result("expired"))
    port = AsyncPort(*responses) if asynchronous else Port(*responses)
    client = AsyncMcpClient(cast("Any", port)) if asynchronous else McpClient(port)

    def run_wait() -> Any:
        return (
            asyncio.run(cast("AsyncMcpClient", client).wait_run(RUN_ID, 10))
            if asynchronous
            else cast("McpClient", client).wait_run(RUN_ID, 10)
        )

    if hint == 20:
        with pytest.raises(ZenturePollingTimeoutError) as raised:
            run_wait()
        assert raised.value.operation_id == RUN_ID
        assert raised.value.last_request_id == "request_safe"
        assert len(port.calls) == 1
    else:
        read = (
            asyncio.run(cast("AsyncMcpClient", client).wait_run(RUN_ID, 10))
            if asynchronous
            else cast("McpClient", client).wait_run(RUN_ID, 10)
        )
        assert read.run.status == "expired"
        assert read.run.run_id == RUN_ID
        assert len(port.calls) == 3
    assert sleeps == expected
    assert all(
        name == "get_run" and args["run_id"] == RUN_ID and args["view"] == "summary"
        for name, args in port.calls
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_late_terminal_times_out_retaining_observation(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    clock = [0.0]
    monkeypatch.setattr("zenture._mcp.wait.time.monotonic", lambda: clock[0])

    class LatePort(Port):
        def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
            clock[0] = 6
            return super().call_tool(name, arguments)

    class LateAsyncPort(LatePort):
        async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
            return super().call_tool(name, arguments)

    port = LateAsyncPort(result("completed")) if asynchronous else LatePort(result("completed"))

    def run_wait() -> None:
        if asynchronous:
            asyncio.run(AsyncMcpClient(cast("Any", port)).wait_run(RUN_ID, 5))
        else:
            McpClient(port).wait_run(RUN_ID, 5)

    with pytest.raises(ZenturePollingTimeoutError) as raised:
        run_wait()
    assert raised.value.operation_id == RUN_ID
    assert raised.value.last_status == "completed"
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_cancel_wait_during_read_does_not_cancel_run() -> None:
    entered = asyncio.Event()

    class PendingPort(AsyncPort):
        async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
            self.calls.append((name, dict(arguments)))
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    port = PendingPort()
    task = asyncio.create_task(AsyncMcpClient(cast("Any", port)).wait_run(RUN_ID, 10))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [name for name, _ in port.calls] == ["get_run"]


@pytest.mark.asyncio
async def test_official_and_sync_portal_forward_read_budget() -> None:
    from anyio.from_thread import start_blocking_portal

    from zenture._mcp.client import _PortalTransport  # pyright: ignore[reportPrivateUsage]
    from zenture._mcp.transport import (
        _OfficialAsyncMcpTransport,  # pyright: ignore[reportPrivateUsage]
    )

    observed: list[float] = []

    class Session:
        async def call_tool(
            self, name: str, *, arguments: dict[str, object], read_timeout_seconds: float
        ) -> object:
            assert name == "get_run"
            assert arguments["view"] == "summary"
            observed.append(read_timeout_seconds)
            return result("completed")

    transport = _OfficialAsyncMcpTransport(Session())
    read = await AsyncMcpClient(transport).wait_run(RUN_ID, 4)
    assert read.run.run_id == RUN_ID

    def sync_wait() -> None:
        with start_blocking_portal() as portal:
            read = McpClient(_PortalTransport(portal, transport)).wait_run(RUN_ID, 3)
            assert read.run.status == "completed"

    await asyncio.to_thread(sync_wait)
    assert len(observed) == 2
    assert 0 < observed[0] <= 4
    assert 0 < observed[1] <= 3


def test_timed_transport_typeerror_is_not_redispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("zenture._mcp.wait.time.monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr("zenture._mcp.wait.time.sleep", sleep)

    class BrokenPort(Port):
        def call_tool(
            self,
            name: str,
            arguments: Mapping[str, object],
            *,
            read_timeout_seconds: float | None = None,
        ) -> object:
            self.calls.append((name, dict(arguments)))
            raise TypeError("implementation failure")

    port = BrokenPort()
    with pytest.raises(ZentureMCPError) as raised:
        McpClient(port).wait_run(RUN_ID, 3)
    assert raised.value.retryable is False
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_official_transport_uses_installed_mcp_seconds_contract() -> None:
    from zenture._mcp.transport import (
        _OfficialAsyncMcpTransport,  # pyright: ignore[reportPrivateUsage]
    )

    class Session:
        async def call_tool(
            self, name: str, *, arguments: dict[str, object], read_timeout_seconds: float
        ) -> object:
            assert isinstance(read_timeout_seconds, float), "MCP 2 requires seconds, not timedelta"
            assert read_timeout_seconds == 2.5
            return result("completed")

    await _OfficialAsyncMcpTransport(Session()).call_tool(
        "get_run", {"run_id": RUN_ID}, read_timeout_seconds=2.5
    )


def test_each_read_uses_remaining_budget_and_poll_interval_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    sleeps: list[float] = []
    budgets: list[float] = []
    monkeypatch.setattr("zenture._mcp.wait.time.monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("zenture._mcp.wait.time.sleep", sleep)

    class TimedPort(Port):
        def call_tool(
            self,
            name: str,
            arguments: Mapping[str, object],
            *,
            read_timeout_seconds: float | None = None,
        ) -> object:
            assert read_timeout_seconds is not None
            budgets.append(read_timeout_seconds)
            return super().call_tool(name, arguments)

    port = TimedPort(*[result("running") for _ in range(5)], result("completed"))
    read = McpClient(port).wait_run(RUN_ID, 30)
    assert read.run.status == "completed"
    assert sleeps == [1, 2, 4, 8, 8]
    assert budgets == [30, 29, 27, 23, 15, 7]
    assert all(name == "get_run" for name, _ in port.calls)
