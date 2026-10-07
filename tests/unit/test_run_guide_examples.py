"""Offline contract checks for the documented REST and MCP Run journeys.

Input: synthetic requests/responses. Output: observed public client behavior.
Dependencies: pytest and httpx. No credentials, login or external service use.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from zenture import ZentureClient
from zenture.errors import ZentureMCPError, ZenturePollingTimeoutError
from zenture.mcp import McpClient

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "run_guide_example"


def example(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_payload(
    status: str = "completed", *, unavailable_reason: str = "projection_incomplete"
) -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "generation": 1,
        "work_type": "answer",
        "profile": "standard",
        "status": status,
        "created_at": "2026-10-07T09:00:00Z",
        "updated_at": "2026-10-07T09:00:00Z",
        "acceptance_decision": "revise" if status == "completed" else None,
        "safe_result_content": {"status": "unavailable", "reason_code": unavailable_reason},
    }


def test_rest_example_starts_once_waits_and_reads_full_result() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/prepare"):
            return httpx.Response(
                200,
                json={
                    "proposal_id": "33333333-3333-4333-8333-333333333333",
                    "proposal_version": 1,
                    "expires_at": "2026-10-07T10:00:00Z",
                    "inferred_work_type": "answer",
                    "task_contract_summary": {
                        "work_type": "answer",
                        "summary_ref": "snapshot:guide",
                        "requirement_count": 1,
                    },
                    "expected_duration_seconds": 30,
                    "start_admissible": True,
                    "proposal_hash": "a" * 64,
                },
            )
        return httpx.Response(
            200, json=run_payload("queued" if request.method == "POST" else "completed")
        )

    with ZentureClient(
        api_key="zt_" + "live_" + "00000000000000000000000000000000",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        result = example("run_review").review_selected_text(
            client, task="Name two colours.", text="Blue.", saved_key="review_123"
        )
    assert result.acceptance_decision == "revise"
    assert result.safe_result_content.status == "unavailable"
    assert [r.url.path for r in requests if r.method == "POST"] == ["/v1/runs/prepare", "/v1/runs"]
    assert requests[0].headers["Idempotency-Key"] == "review_123:prepare"
    assert requests[1].headers["Idempotency-Key"] == "review_123:create"
    assert requests[-1].url.params["view"] == "full"


class McpPort:
    def __init__(
        self,
        *,
        ongoing: bool = False,
        fail_start: bool = False,
        unavailable_reason: str = "projection_incomplete",
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.ongoing = ongoing
        self.fail_start = fail_start
        self.unavailable_reason = unavailable_reason

    def list_tools(self) -> object:
        return {
            "tools": [
                {"name": name}
                for name in ("run", "list_runs", "get_run", "cancel_run", "record_run_outcome")
            ]
        }

    def call_tool(self, name: str, arguments: Any) -> object:
        self.calls.append((name, dict(arguments)))
        if name == "run" and self.fail_start:
            raise TimeoutError("synthetic transport timeout")
        payload = run_payload(
            "queued" if name == "run" or self.ongoing else "completed",
            unavailable_reason=self.unavailable_reason,
        )
        if name == "run":
            payload["idempotency_key"] = arguments["idempotency_key"]
        return {"structuredContent": payload}


@pytest.mark.parametrize("reason", ["projection_incomplete", "result_content_expired"])
def test_mcp_example_unwraps_result_and_preserves_non_ready_decision(reason: str) -> None:
    port = McpPort(unavailable_reason=reason)
    result = example("mcp_run_review").review_selected_text(
        McpClient(port), task="Name two colours.", text="Blue.", saved_key="review_123"
    )
    assert result.run_id == RUN_ID
    assert result.acceptance_decision == "revise"
    assert result.safe_result_content.status == "unavailable"
    assert result.safe_result_content.reason_code == reason
    assert [name for name, _ in port.calls] == ["run", "get_run", "get_run"]
    assert port.calls[0][1]["idempotency_key"] == "review_123"
    assert port.calls[-1][1]["view"] == "full"


def test_mcp_example_timeout_retains_run_id_without_restart_or_cancel(monkeypatch: Any) -> None:
    module = example("mcp_run_review")
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    port = McpPort(ongoing=True)
    with pytest.raises(ZenturePollingTimeoutError) as exc:
        module.review_selected_text(
            McpClient(port),
            task="Review.",
            text="Selected text.",
            saved_key="review_123",
            timeout=1.0,
        )
    assert exc.value.operation_id == RUN_ID
    assert [name for name, _ in port.calls] == ["run"]


def test_mcp_example_uncertain_start_propagates_original_key_without_retry() -> None:
    port = McpPort(fail_start=True)
    with pytest.raises(ZentureMCPError) as exc:
        example("mcp_run_review").review_selected_text(
            McpClient(port), task="Review.", text="Selected text.", saved_key="review_123"
        )
    assert exc.value.idempotency_key == "review_123"
    assert [name for name, _ in port.calls] == ["run"]
