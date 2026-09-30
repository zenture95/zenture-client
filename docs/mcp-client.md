# MCP client peer surface

The SDK contains an opt-in client-side MCP adapter beside the API resources.
It consumes the hosted Streamable HTTP MCP contract and maps the six Run tools
to the SDK's existing typed Run, artifact, event and outcome models. API and
MCP are peer consumption channels; neither channel becomes a second execution
or persistence authority.

The adapter is intentionally not an OAuth implementation. The application
that owns the user interaction supplies an already-issued MCP bearer through a
callable provider. The SDK does not discover, refresh, revoke, persist or log
that credential. The API-token client and MCP adapter therefore have separate
credential boundaries.

Install the optional integration dependency only for callers that need the
official MCP transport:

```bash
python -m pip install 'zenture[mcp]'
```

The API-only installation remains independent of the optional dependency. The
adapter validates the hosted zenture MCP origin, initializes the official
Streamable HTTP session, bounds tool input and output, and exposes only safe
typed projections:

```python
from zenture._mcp import AsyncMcpClient


async def review(mcp_access_token_provider):
    async with AsyncMcpClient.connect(
        "https://mcp.zenture.app",
        bearer_token=mcp_access_token_provider,
    ) as client:
        await client.require_product_tools()
        run = await client.run(
            task="Review the selected answer",
            artifact={"type": "text", "value": "selected answer"},
        )
        return await client.get_run(run.run_id, view="full")
```

`AsyncMcpClient.connect(...)` is the network-ready path because the official
Python MCP transport is asynchronous. `McpClient` provides the identical typed
surface over a caller-owned synchronous transport port for controlled
integrations and deterministic tests; it does not introduce a second HTTP
implementation.

`get_run(..., replay_cursor=...)` returns an `McpRunRead` containing the
canonical Run and an explicit typed replay page. `replay_events(...)` is
bounded and never auto-pages. Artifact bytes are not encoded into MCP JSON;
the active host transport must provide the separately governed byte-source
capability or the server returns `artifact_unavailable`.

The `_mcp` namespace remains an opt-in implementation surface. The package
exports `ZentureClient` and `AsyncZentureClient` for API-token access; these
classes do not perform OAuth or manage MCP credentials.
