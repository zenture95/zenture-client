"""SDK-only Product Run E2E fixture and report adapter.

Filepath: src/zenture/testing/product_runs.py
Purpose: Bind one approved fixture to one public SDK Run observation.
Input: A caller-owned AsyncZenture client and a synthetic fixture.
Output: A bounded report containing the SDK responses and event replay.
Critical Logic: Every observation stays bound to the Run created by the SDK.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from zenture._contract import (
    ListRunEventsResponse,
    PublicRunResponse,
    RunProfile,
    RunStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from zenture import AsyncZenture


@dataclass(frozen=True, slots=True)
class ProductRunFixture:
    """One caller-approved synthetic task and artifact."""

    fixture_id: str
    task: str
    artifact: Mapping[str, object]
    profile: RunProfile = RunProfile.STANDARD


@dataclass(frozen=True, slots=True)
class ProductRunReport:
    """The public SDK observations for one fixture-bound Run."""

    fixture: ProductRunFixture
    started: PublicRunResponse
    terminal: PublicRunResponse
    full: PublicRunResponse
    event_replay: ListRunEventsResponse

    @property
    def run_id(self) -> str:
        return self.started.run_id

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready fixture/report mapping for test tooling."""

        return {
            "fixture": asdict(self.fixture),
            "started": self.started.model_dump(mode="json"),
            "terminal": self.terminal.model_dump(mode="json"),
            "full": self.full.model_dump(mode="json"),
            "event_replay": self.event_replay.model_dump(mode="json"),
        }


def _require_same_run(response: PublicRunResponse, *, run_id: str, stage: str) -> None:
    if response.run_id != run_id:
        raise ValueError(f"SDK Run response at {stage} changed the bound run_id")


def _require_replay_identity(replay: ListRunEventsResponse, *, run_id: str) -> None:
    if any(event.run_id != run_id for event in replay.events):
        raise ValueError("SDK Run event replay changed the bound run_id")


_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.EXPIRED,
        RunStatus.BUDGET_EXHAUSTED,
    }
)


async def _cleanup_run_after_error(
    client: AsyncZenture,
    run_id: str,
    *,
    idempotency_key: str,
) -> None:
    """Cancel the same Run when observation fails before terminal state."""

    try:
        current = await client.runs.get(run_id)
        if current.status not in _TERMINAL_STATUSES:
            await client.runs.cancel(
                run_id,
                idempotency_key=f"{idempotency_key}-cleanup",
            )
    except Exception:
        # Preserve the original observation failure; cleanup status remains
        # visible to the owning caller through the same public Run boundary.
        return


async def run_product_e2e(
    client: AsyncZenture,
    fixture: ProductRunFixture,
    *,
    idempotency_key: str,
    timeout: float = 120.0,
    initial_interval: float = 1.0,
    max_interval: float = 8.0,
) -> ProductRunReport:
    """Observe one Product Run through the official async SDK resource."""

    started = await client.runs.run(
        task=fixture.task,
        artifact=dict(fixture.artifact),
        profile=fixture.profile,
        idempotency_key=idempotency_key,
        wait=0,
    )
    run_id = started.run_id

    try:
        terminal = await client.runs.wait(
            run_id,
            timeout=timeout,
            initial_interval=initial_interval,
            max_interval=max_interval,
        )
        _require_same_run(terminal, run_id=run_id, stage="wait")

        full = await client.runs.get(run_id, view="full")
        _require_same_run(full, run_id=run_id, stage="full")

        event_replay = await client.runs.list_events(run_id, limit=50)
        _require_replay_identity(event_replay, run_id=run_id)
    except Exception:
        await _cleanup_run_after_error(
            client,
            run_id,
            idempotency_key=idempotency_key,
        )
        raise

    return ProductRunReport(
        fixture=fixture,
        started=started,
        terminal=terminal,
        full=full,
        event_replay=event_replay,
    )


__all__ = ("ProductRunFixture", "ProductRunReport", "run_product_e2e")
