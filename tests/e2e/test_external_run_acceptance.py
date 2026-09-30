"""Opt-in real MCP/API cross-channel acceptance against the INT deployment."""

from __future__ import annotations

import os

import pytest

from zenture import AsyncZentureClient
from zenture._contract import PublicRunEvent, PublicRunStreamMessage, RunStatus
from zenture._mcp import AsyncMcpClient

MCP_INT_ENDPOINT = "https://mcp-int.zenture.app"
_REAL_E2E_ENABLEMENT = "ZENTURE_RUN_EXTERNAL_E2E"
_MCP_ACCESS_TOKEN = "ZENTURE_MCP_ACCESS_TOKEN"

_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.EXPIRED,
        RunStatus.SUCCEEDED,
        RunStatus.BUDGET_EXHAUSTED,
    }
)


def _require_real_e2e_enablement() -> None:
    if os.environ.get(_REAL_E2E_ENABLEMENT) != "1":
        pytest.fail(
            "real acceptance is opt-in; set ZENTURE_RUN_EXTERNAL_E2E=1 for the approved window"
        )


def _mcp_access_token() -> str:
    value = os.environ.get(_MCP_ACCESS_TOKEN)
    if not value:
        pytest.fail("operator-injected MCP access credential is required")
    return value


async def _cancel_if_active(client: AsyncMcpClient, run_id: str) -> None:
    try:
        read = await client.get_run(run_id)
    except Exception:
        pytest.fail("cleanup could not read a tracked Run")
    if read.run.status not in _TERMINAL_STATUSES:
        try:
            await client.cancel_run(run_id)
        except Exception:
            pytest.fail("cleanup could not cancel a tracked Run")


async def _stream_run_events(
    client: AsyncZentureClient, run_id: str
) -> list[PublicRunStreamMessage]:
    return [
        message
        async for message in client.runs.iter_events(
            run_id,
            timeout=180.0,
            initial_interval=0.5,
            max_interval=4.0,
        )
    ]


@pytest.mark.real_e2e
@pytest.mark.asyncio
async def test_real_mcp_and_api_clients_complete_both_cross_channel_journeys() -> None:
    """Exercise the real INT MCP/API path after Engine Maintenance approval."""

    _require_real_e2e_enablement()
    run_ids: list[str] = []

    async with (
        AsyncZentureClient.from_env() as api,
        AsyncMcpClient.connect(
            MCP_INT_ENDPOINT,
            bearer_token=_mcp_access_token,
            timeout=30.0,
        ) as mcp,
    ):
        await mcp.require_product_tools()

        try:
            created_via_mcp = await mcp.run(
                task="Review the selected acceptance fixture before reliance.",
                artifact={
                    "type": "text",
                    "value": "A bounded acceptance fixture with one explicit claim.",
                },
                profile="standard",
            )
            run_ids.append(created_via_mcp.run_id)

            completed_via_api = await api.runs.wait(
                created_via_mcp.run_id,
                timeout=180.0,
                initial_interval=0.5,
                max_interval=4.0,
            )
            read_via_api = await api.runs.get(created_via_mcp.run_id, view="full")
            streamed_events = await _stream_run_events(api, created_via_mcp.run_id)
            replayed_events = await api.runs.list_events(
                created_via_mcp.run_id,
                limit=50,
            )
            final_read_via_mcp = await mcp.get_run(created_via_mcp.run_id, view="full")
            outcome_via_api = await api.runs.record_outcome(
                created_via_mcp.run_id,
                outcome="used",
                idempotency_key=f"external-acceptance-outcome-{created_via_mcp.run_id}",
            )

            assert completed_via_api.run_id == created_via_mcp.run_id
            assert read_via_api.run_id == created_via_mcp.run_id
            assert read_via_api.status in _TERMINAL_STATUSES
            assert final_read_via_mcp.run.run_id == read_via_api.run_id
            assert final_read_via_mcp.run.status == read_via_api.status
            assert final_read_via_mcp.run.acceptance_decision == read_via_api.acceptance_decision
            assert final_read_via_mcp.run.artifact_refs == read_via_api.artifact_refs
            assert outcome_via_api.run_id == created_via_mcp.run_id
            assert replayed_events.events
            assert any(isinstance(message, PublicRunEvent) for message in streamed_events)
            assert {message.event_id for message in streamed_events}.issuperset(
                {event.event_id for event in replayed_events.events}
            )

            created_via_api = await api.runs.run(
                task="Cancel the second selected acceptance fixture immediately.",
                artifact={"type": "text", "value": "A second bounded cancellation fixture."},
                profile="fast",
                idempotency_key=f"external-acceptance-run-{created_via_mcp.run_id}",
                wait=0,
            )
            run_ids.append(created_via_api.run_id)

            cancellation = await mcp.cancel_run(created_via_api.run_id)
            read_via_mcp = await mcp.get_run(created_via_api.run_id, view="summary")
            terminal_after_cancel = await api.runs.wait(
                created_via_api.run_id,
                timeout=180.0,
                initial_interval=0.5,
                max_interval=4.0,
            )
            final_read_via_mcp = await mcp.get_run(created_via_api.run_id, view="summary")

            assert cancellation.run_id == created_via_api.run_id
            assert cancellation.cancellation_requested or cancellation.status in {
                RunStatus.CANCEL_REQUESTED,
                RunStatus.CANCELLED,
            }
            assert read_via_mcp.run.run_id == created_via_api.run_id
            assert terminal_after_cancel.status == RunStatus.CANCELLED
            assert final_read_via_mcp.run.status == RunStatus.CANCELLED
        finally:
            for run_id in run_ids:
                await _cancel_if_active(mcp, run_id)
