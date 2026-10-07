"""Local same-Run wait policy; no execution or lifecycle authority.

Owns deadline, polling and read retry decisions. Imports only SDK contracts
and policies; callers supply the parsed summary-read boundary and clock seam.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

from zenture._contract import RunStatus
from zenture._mcp.contracts import McpGetRunRequest, McpRunRead
from zenture.errors import ZentureMCPError, ZenturePollingTimeoutError
from zenture.polling import next_poll_interval, remaining_timeout, validate_wait_parameters

TERMINAL = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "expired", "budget_exhausted"}
)


def supports_timeout(call: Callable[..., object]) -> bool:
    """Detect the additive keyword once, without retrying a dispatched call."""
    try:
        parameter = inspect.signature(call).parameters.get("read_timeout_seconds")
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def timed_call(
    call: Callable[..., Any],
    name: str,
    arguments: Mapping[str, object],
    remaining: float | None,
    timed: bool,
) -> Any:
    if remaining is not None and timed:
        return call(name, arguments, read_timeout_seconds=remaining)
    return call(name, arguments)


class WaitBudget:
    """One monotonic deadline and last safe observation for one Run."""

    def __init__(self, run_id: str, timeout: float | None, stop_on: Sequence[str]) -> None:
        if timeout is None:
            raise ValueError("timeout must be finite and positive")
        validate_wait_parameters(timeout=timeout, initial_interval=1, max_interval=8)
        self.arguments = McpGetRunRequest(run_id=run_id).model_dump(mode="json", exclude_none=True)
        self.arguments.pop("include_event_replay", None)
        if isinstance(stop_on, str) or any(
            status not in {item.value for item in RunStatus} for status in stop_on
        ):
            raise ValueError("stop_on must contain existing Run statuses")
        self.run_id = run_id
        self.stops = TERMINAL.union(stop_on)
        self.deadline = time.monotonic() + timeout
        self.interval = 1.0
        self.last_status: str | None = None
        self.last_request_id: str | None = None

    def remaining(self) -> float:
        seconds = remaining_timeout(deadline=self.deadline, now=time.monotonic())
        if not seconds:
            raise ZenturePollingTimeoutError(
                operation_id=self.run_id,
                last_status=self.last_status,
                last_request_id=self.last_request_id,
            )
        return seconds

    def observed(self, read: McpRunRead) -> bool:
        self.last_status = read.run.status
        self.remaining()
        return read.run.status in self.stops

    def delay(self, error: ZentureMCPError | None = None) -> float:
        delay = self.interval
        if error is not None:
            if not error.retryable:
                raise error
            self.last_request_id = error.request_id
            hint = error.retry_after_seconds
            if isinstance(hint, int) and not isinstance(hint, bool) and 0 <= hint <= 3600:
                delay = max(delay, hint)
        self.interval = next_poll_interval(current=self.interval, max_interval=8)
        return min(delay, self.remaining())


def wait_sync(
    run_id: str,
    timeout: float,
    stop_on: Sequence[str],
    read: Callable[[Mapping[str, object], float], McpRunRead],
) -> McpRunRead:
    budget = WaitBudget(run_id, timeout, stop_on)
    while True:
        error = None
        try:
            response = read(budget.arguments, budget.remaining())
        except ZentureMCPError as exc:
            error = exc
        else:
            if budget.observed(response):
                return response
        time.sleep(budget.delay(error))


async def wait_async(
    run_id: str,
    timeout: float,
    stop_on: Sequence[str],
    read: Callable[[Mapping[str, object], float], Awaitable[McpRunRead]],
) -> McpRunRead:
    budget = WaitBudget(run_id, timeout, stop_on)
    while True:
        error = None
        try:
            response = await read(budget.arguments, budget.remaining())
        except ZentureMCPError as exc:
            error = exc
        else:
            if budget.observed(response):
                return response
        await asyncio.sleep(budget.delay(error))
