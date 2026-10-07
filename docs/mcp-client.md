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

## Payloads, results and complete workflows

Start with the [Run guide](run-guide.md): it documents every tool argument,
nested artifact/feedback fields, defaults, omitted versus null values, response
interpretation, and the matching REST methods. `get_run()` returns a Python
wrapper with `.run` and optional `.event_replay`; raw MCP structured content is
the Run itself. `run()` starts work but does not poll to completion.

Use the [complete MCP example](../examples/mcp_run_review.py) with stored
authorization and a saved caller key. It waits within a budget and reads full
content. No login or product call occurs merely by importing the example.

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

## Supported tools

Run execution currently supports inline text. The schema retains `zenture_ref`,
but Run preparation rejects registered-file inputs; registration does not enable
file evaluation. Raw MCP `run` also supports `predecessor_run_id`; these Python
MCP wrappers do not expose it yet. Use REST `runs.create()` / `runs.run()` for
linked follow-ups from Python, preserving the predecessor on idempotent recovery.

| Tool | Method | Purpose |
|---|---|---|
| `run` | `run(task=..., artifact=..., profile="standard", idempotency_key=...)` | Start a Run |
| `attach_artifact` | `attach_artifact(file_name=..., mime_type=..., byte_size=..., content_hash=...)` | Register an artifact upload when the host provides an approved artifact resolver |
| `list_runs` | `list_runs(status=..., limit=5, cursor=...)` | List Runs, one bounded page |
| `get_run` | `get_run(run_id, view="summary" or "full", replay_cursor=...)` | Read a Run, optionally with one replay page |
| `cancel_run` | `cancel_run(run_id)` | Cancel a Run |
| `record_run_outcome` | `record_run_outcome(run_id, outcome=...)` | Record what you did with the result |

The five core tools are `run`, `list_runs`, `get_run`, `cancel_run` and
`record_run_outcome`. The host may also advertise `attach_artifact` when it has
an approved resolver for the artifact bytes. `require_product_tools()` checks
only the five core tools, so a host without that resolver is still ready for
Runs. Artifact bytes come from the host-selected resolver and are not carried
in MCP JSON; the server answers `artifact_unavailable` when that source is not
available. `replay_events(...)` is bounded and never pages automatically.

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

## Complete tool discovery

`client.get_tool_catalog()` returns a tuple of SDK-owned `McpToolDefinition`
objects. Await the method on `AsyncMcpClient`. Import the definition type from
`zenture.mcp`; callers need no imports from the MCP transport package.

```python
from zenture.mcp import McpClient, McpToolDefinition

with McpClient.connect() as client:
    catalog: tuple[McpToolDefinition, ...] = client.get_tool_catalog()
    run_tool = next(tool for tool in catalog if tool.name == "run")
    task_schema = run_tool.input_schema["properties"]["task"]
```

Each definition preserves the registered `name`, `title`, `description`,
`input_schema`, `output_schema`, and `annotations`, including schema constraints,
references and annotation extensions. Optional metadata is `None` when absent.
Schemas and annotations are detached JSON objects; changing them does not change
transport data. Definitions reflect the current server registration: discover
`attach_artifact` by membership instead of assuming availability. Annotation
hints describe tools and do not grant authorization.

Discovery makes no product-tool calls and has no result cache. It follows
pagination through the official initialized MCP transport, with limits of 128
tools, 128 pages and 256 KiB of catalog JSON. Invalid metadata, duplicate names,
repeated cursors, exceeded limits, and unsupported continuation raise
`ZentureMCPProtocolError` without returning a partial catalog. Transport failures
use `ZentureMCPError`.

Custom transports can support continuation with
`list_tools(*, cursor: str | None = None)`. An older no-argument transport still
works for a complete single page; returning a cursor without support raises an
explicit protocol error. `list_tools()` retains its existing tuple of names and
accepts older name-only catalogs.

### Waiting for one Run

Both peers provide `wait_run(run_id, timeout, *, stop_on=())`. Supply a finite,
positive timeout in seconds on an already connected client:

```python
read = client.wait_run(run_id, 60, stop_on=("waiting_for_dependency",))
# Async peer: read = await client.wait_run(run_id, 60)
print(read.run.status)
```

The helper reads the same Run with summary view only. It returns `McpRunRead`
for completed, historical succeeded, failed, cancelled, expired, and
budget_exhausted states, plus any existing statuses supplied in `stop_on`.
Unknown statuses are rejected before I/O. Technical completion still differs
from acceptance. Read full content explicitly with `get_run` when needed.

Polling starts at one second and doubles up to eight seconds. Only retryable
read failures are retried; `retry_after_seconds` is a minimum delay. When that
delay cannot fit, waiting expires locally without an early retry. Permanent
errors propagate. Waiting never starts, cancels, or records an outcome for a Run.
Async task cancellation stops local observation and propagates to the caller.

`ZenturePollingTimeoutError` from `zenture.errors` retains the Run ID in
`operation_id`, the last observed status in `last_status`, and the last safe
error request ID in `last_request_id`. Keep the ID to resume observation later.
A terminal read arriving after the deadline still raises this timeout.

The official MCP transport and sync portal forward the remaining read budget
per call. Custom transports may add a keyword `read_timeout_seconds` accepting
numeric seconds. Existing two-argument `call_tool(name, arguments)` transports
continue to work: their dispatches and sleeps respect the deadline, but an
in-flight read can overrun it. The late response still produces a local timeout.
An internal `TypeError` is surfaced without redispatching that read.
