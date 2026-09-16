"""Contract tests for the SDK-only Product Run E2E adapter."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zenture._contract import (
    ListRunEventsResponse,
    PublicRunResponse,
    RunProfile,
    RunStatus,
)
from zenture.testing.product_runs import (
    ProductRunFixture,
    ProductRunReport,
    run_product_e2e,
)

RUN_ID = "run_33333333333343338333333333333333"


def _run(status: str) -> PublicRunResponse:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    return PublicRunResponse.model_validate(
        {
            "run_id": RUN_ID,
            "generation": 1,
            "family": "knowledge",
            "work_type": "answer",
            "profile": "standard",
            "status": status,
            "created_at": now,
            "updated_at": now,
            "completed_at": now if status == "completed" else None,
            "queue": {"queue_reason": "queue:admitted", "jobs_ahead": 0},
            "cancellation_requested": False,
        }
    )


def _replay() -> ListRunEventsResponse:
    return ListRunEventsResponse.model_validate(
        {
            "events": [
                {
                    "type": "run.event",
                    "event_id": "event_aaa",
                    "run_id": RUN_ID,
                    "sequence": 1,
                    "status": "completed",
                    "message_key": "run.status.completed",
                    "event_cursor": "cursor_aaa",
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }
    )


class RecordingRuns:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.started = _run("queued")
        self.terminal = _run("completed")
        self.full = _run("completed")
        self.replay = _replay()

    async def run(self, **kwargs: object) -> PublicRunResponse:
        self.calls.append(("run", (), kwargs))
        return self.started

    async def wait(self, run_id: str, **kwargs: object) -> PublicRunResponse:
        self.calls.append(("wait", (run_id,), kwargs))
        return self.terminal

    async def get(self, run_id: str, **kwargs: object) -> PublicRunResponse:
        self.calls.append(("get", (run_id,), kwargs))
        return self.full

    async def list_events(self, run_id: str, **kwargs: object) -> ListRunEventsResponse:
        self.calls.append(("list_events", (run_id,), kwargs))
        return self.replay


class RecordingClient:
    def __init__(self) -> None:
        self.runs = RecordingRuns()


@pytest.mark.asyncio
async def test_product_run_e2e_uses_one_sdk_run_identity_and_maps_the_report() -> None:
    client = RecordingClient()
    fixture = ProductRunFixture(
        fixture_id="fixture-answer-001",
        task="Review the selected answer.",
        artifact={"type": "text", "value": "Approved synthetic answer."},
        profile=RunProfile.STANDARD,
    )

    report = await run_product_e2e(
        client,  # type: ignore[arg-type]
        fixture,
        idempotency_key="product-e2e-fixture-answer-001",
        timeout=31.0,
        initial_interval=0.25,
        max_interval=2.0,
    )

    assert isinstance(report, ProductRunReport)
    assert report.run_id == RUN_ID
    assert report.full.status is RunStatus.COMPLETED
    assert report.event_replay.events[0].run_id == RUN_ID
    assert report.as_dict()["fixture"] == {
        "fixture_id": "fixture-answer-001",
        "task": "Review the selected answer.",
        "artifact": {"type": "text", "value": "Approved synthetic answer."},
        "profile": "standard",
    }
    assert [name for name, _, _ in client.runs.calls] == [
        "run",
        "wait",
        "get",
        "list_events",
    ]
    assert client.runs.calls[0][2] == {
        "task": fixture.task,
        "artifact": dict(fixture.artifact),
        "profile": RunProfile.STANDARD,
        "idempotency_key": "product-e2e-fixture-answer-001",
        "wait": 0,
    }
    assert client.runs.calls[1] == (
        "wait",
        (RUN_ID,),
        {"timeout": 31.0, "initial_interval": 0.25, "max_interval": 2.0},
    )
    assert client.runs.calls[2] == ("get", (RUN_ID,), {"view": "full"})
    assert client.runs.calls[3] == ("list_events", (RUN_ID,), {"limit": 50})


@pytest.mark.asyncio
async def test_product_run_e2e_rejects_a_foreign_terminal_response() -> None:
    client = RecordingClient()
    client.runs.terminal = _run("completed").model_copy(update={"run_id": "run_aaaaaaaaaaaaaaaa"})
    fixture = ProductRunFixture(
        "fixture-answer-002", "Review.", {"type": "text", "value": "Answer"}
    )

    with pytest.raises(ValueError, match="changed the bound run_id"):
        await run_product_e2e(client, fixture, idempotency_key="product-e2e-fixture-answer-002")  # type: ignore[arg-type]

    assert [name for name, _, _ in client.runs.calls] == ["run", "wait"]


def test_product_run_adapter_has_no_non_sdk_transport_or_service_imports() -> None:
    path = Path(__file__).parents[2] / "src" / "zenture" / "testing" / "product_runs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    forbidden = ("httpx", "requests", "zenture._mcp", "backend", "database", "redis", "engine")

    assert not any(
        module.casefold().startswith(prefix) for module in imports for prefix in forbidden
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getenv", "environ"}
        for node in ast.walk(tree)
    )
