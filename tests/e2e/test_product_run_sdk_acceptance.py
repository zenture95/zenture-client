"""Opt-in local Product Run acceptance through the public SDK only."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from zenture import AsyncZentureClient
from zenture._contract import RunProfile, RunStatus
from zenture.testing.product_runs import ProductRunFixture, run_product_e2e

_ENABLEMENT = "ZENTURE_RUN_LOCAL_E2E"
_LOCAL_BASE_URL = "http://localhost:8000"
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


def _require_local_e2e_enablement() -> None:
    if os.environ.get(_ENABLEMENT) != "1":
        pytest.skip(f"set {_ENABLEMENT}=1 for the approved local Product Run check")
    if os.environ.get("ZENTURE_BASE_URL") != _LOCAL_BASE_URL:
        pytest.fail("local Product Run acceptance requires ZENTURE_BASE_URL=http://localhost:8000")


@pytest.mark.real_e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("profile", tuple(RunProfile))
async def test_product_run_sdk_acceptance(profile: RunProfile) -> None:
    """Run one complete public SDK journey for each supported profile."""

    _require_local_e2e_enablement()
    fixture = ProductRunFixture(
        fixture_id="fixture-product-run-quality-001",
        task="Bewerte die ausgewählte Aussage anhand der bereitgestellten Evidenz.",
        artifact={
            "type": "text",
            "value": "Die Erde umkreist die Sonne; die Aussage soll evidenzbezogen bewertet werden.",
        },
        profile=profile,
    )

    async with AsyncZentureClient.from_env() as client:
        report = await run_product_e2e(
            client,
            fixture,
            idempotency_key=f"run-quality-local-{profile.value}-{uuid4().hex}",
            timeout=900.0,
        )

    assert report.run_id == report.terminal.run_id == report.full.run_id
    assert report.full.profile is profile
    assert report.full.status in _TERMINAL_STATUSES
    assert report.full.safe_result_content is not None
    assert report.full.safe_result_content.status == "available"
    assert report.full.safe_result_content.content.summary
    assert report.full.safe_result_content.content.findings is not None
    assert report.full.safe_result_content.content.evidence_summaries is not None
    assert report.full.safe_result_content.content.limitations is not None
    assert report.full.safe_result_content.content.decision.next_action
    assert report.full.billing_projection is not None
    assert report.full.billing_projection.status in {"settled", "released"}
    assert report.full.billing_projection.final_credits is not None
    assert report.full.billing_projection.final_credits.status == "available"
    assert report.full.usage_summary == {}
    assert report.full.billing_summary == {}
    assert report.event_replay.events
    assert all(event.run_id == report.run_id for event in report.event_replay.events)
