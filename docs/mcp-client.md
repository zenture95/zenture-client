# MCP client

`zenture.mcp` is the client of the hosted zenture MCP endpoint. It is a peer of
the REST client: both consume the same Runs, and neither becomes a second
execution or persistence authority. There is one synchronous and one
asynchronous peer with the identical typed surface:

```python
from zenture.mcp import AsyncMcpClient, McpClient
```

The MCP endpoint uses your zenture account, not an API token. Log in first, see
[authentication](./authentication.md):

```bash
zenture auth login
```

## Connecting

`connect()` without `session` or `bearer_token` uses the stored authorization. It
never starts a login: without one it raises `AuthorizationRequired` (run
`zenture auth login`). To log in inside a program, call `zenture.auth.login()` or
`login_async()` explicitly and pass the returned session:

```python
import asyncio

from zenture.auth import login_async
from zenture.mcp import AsyncMcpClient


async def review() -> None:
    with await login_async() as session:
        async with AsyncMcpClient.connect(session=session) as client:
            await client.require_product_tools()
            run = await client.run(
                task="Review the selected answer",
                artifact={"type": "text", "value": "selected answer"},
            )
            read = await client.get_run(run.run_id, view="full")
            print(read.run.status)


asyncio.run(review())
```

```python
from zenture.mcp import McpClient

with McpClient.connect() as client:  # stored authorization, no login
    for item in client.list_runs(limit=5).runs:
        print(item.run_id)
```

`McpClient.connect()` runs the same asynchronous implementation on a dedicated
background event loop that is stopped when the `with` block ends. Calling it from
inside a running event loop raises `RuntimeError`; use `AsyncMcpClient` there.

A caller that already owns a credential can instead pass
`bearer_token=<async callable returning the token>`. The client then does not
refresh, store or log it.

## The six tools

| Tool | Method | Purpose |
|---|---|---|
| `run` | `run(task=..., artifact=..., profile="standard")` | Start a Run |
| `attach_artifact` | `attach_artifact(file_name=..., mime_type=..., byte_size=..., content_hash=...)` | Register an artifact upload |
| `list_runs` | `list_runs(status=..., limit=5, cursor=...)` | List Runs, one bounded page |
| `get_run` | `get_run(run_id, view="summary" or "full", replay_cursor=...)` | Read a Run, optionally with one replay page |
| `cancel_run` | `cancel_run(run_id)` | Cancel a Run |
| `record_run_outcome` | `record_run_outcome(run_id, outcome=...)` | Record what you did with the result |

`replay_events(...)` is bounded and never pages automatically. Artifact bytes are
not carried in MCP JSON; the server answers `artifact_unavailable` when the byte
source is not available.

## Behavior to rely on

- A request rejected as unauthenticated is retried once after one serialized
  refresh (the rejection happens before any tool runs). Tool calls are never
  retried automatically after a timeout or a server error.
- An interrupted refresh can require `zenture auth login` again; the old
  credential is never reused.
- A `403` raises `PermissionDenied`; a new login does not widen access.
- Nothing in this module logs or prints credentials.

## Run idempotency

The MCP `run` method currently takes `task`, `artifact` and `profile` only. A
caller-supplied `idempotency_key` for MCP `run` is not available; do not pass
one. Until it exists, do not blindly repeat `run` after a timeout: check
`list_runs` for the Run you started. Idempotency keys are a feature of the REST
client, see [idempotency](./idempotency.md).
