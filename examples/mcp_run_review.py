"""Review selected text through MCP without implicit login or automatic restart.

Input: connected MCP client, selected task/text, persisted operation key.
Output: typed full Run response or error retaining the original identity.
Dependencies: time and zenture. Importing this file makes no service calls.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from zenture.errors import ZenturePollingTimeoutError

if TYPE_CHECKING:
    from zenture.mcp import McpClient
    from zenture.types import PublicRunResponse

TERMINAL_STATUSES = {"completed", "succeeded", "failed", "cancelled", "expired", "budget_exhausted"}


def review_selected_text(
    client: McpClient,
    *,
    task: str,
    text: str,
    saved_key: str,
    timeout: float = 120.0,
) -> PublicRunResponse:
    """Start once, poll summaries with backoff, then unwrap the full Run.

    Persist saved_key and the exact task/text before dispatch; starting may
    consume Credits. timeout bounds the polling loop; individual in-flight
    calls also use the connection's transport timeout. No failure triggers a
    replacement start, cancellation or login. Inspect acceptance_decision and
    safe_result_content availability rather than assuming completion is ready.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    client.require_product_tools()
    run = client.run(
        task=task,
        artifact={"type": "text", "value": text},
        idempotency_key=saved_key,
    )
    deadline = time.monotonic() + timeout
    interval = 1.0
    while run.status not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            raise ZenturePollingTimeoutError(operation_id=run.run_id)
        run = client.get_run(run.run_id, view="summary").run
        if run.status not in TERMINAL_STATUSES:
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 2, 8.0)
    return client.get_run(run.run_id, view="full").run
