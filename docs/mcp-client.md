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
                idempotency_key="review_case_123",  # save before dispatch
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
| `run` | `run(task=..., artifact=..., profile="standard", idempotency_key=...)` | Start a Run |
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

Each deliberate new `run` allocates a random identity once before dispatch when
`idempotency_key` is omitted. To recover after interruption or a process restart,
save an explicit key in your application **before** calling `run`, then reuse it
verbatim with the same task, artifact and profile. Explicit keys must be 1–128
ASCII letters, digits, underscores or hyphens (`[A-Za-z0-9_-]`); they are
case-sensitive. Null, non-string values and whitespace are rejected before send.
Keep keys free of credentials, task content and personal data; do not log them.

A successful Run remains a `PublicRunResponse` instance with its existing
attributes. The MCP-only result also exposes `run.idempotency_key`. This named
attribute is excluded from `model_dump()` and `model_dump_json()`; save it
separately. `ZentureMCPError.idempotency_key` retains the locally attempted key
on safe transport, tool or decoding errors. A missing or foreign receipt raises
`ZentureMCPProtocolError` with the local key. Other tools and REST models keep
their existing shapes.

There is no automatic timeout/server-error retry or stored last-operation key.
Cancellation still propagates, so a generated key cannot be recovered from a
cancelled call: save an explicit key beforehand. A receipt identifies the
attempt; it does not prove acceptance, completion or a debit. Reuse never bypasses
current authorization or admission. Accepted recovery depends on retained
server authority; an expired prepared-only Run is not made admissible. A changed
request under the same key can conflict. Never use a replacement key as a fallback
after an uncertain outcome. See [idempotency](./idempotency.md) for the distinct
REST client contract.
