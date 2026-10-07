"""Review selected text through REST using a key saved by the caller.

Input: connected client, selected task/text, persisted operation key.
Output: typed full Run response; no payload printing or implicit login.
Dependencies: zenture. Importing this file makes no service calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zenture import ZentureClient
    from zenture.types import PublicRunResponse


def review_selected_text(
    client: ZentureClient,
    *,
    task: str,
    text: str,
    saved_key: str,
    timeout: float = 120.0,
) -> PublicRunResponse:
    """Start once, wait within timeout seconds, then read available full content.

    The caller must persist saved_key and the exact task/text before dispatch.
    Starting may consume Credits. Timeout/stop exceptions retain run_id in
    operation_id; read that Run later without starting or cancelling work.
    A returned completed status does not imply acceptance_decision == ready.
    """
    run = client.runs.run(
        task=task,
        artifact={"type": "text", "value": text},
        idempotency_key=saved_key,
    )
    client.runs.wait(run.run_id, timeout=timeout)
    return client.runs.get(run.run_id, view="full")
