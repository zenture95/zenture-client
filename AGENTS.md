# AGENTS.md

Purpose: onboard contributors and coding agents to the public Python SDK for
the zenture Public API.

## Repository Purpose

`zenture-client` is the official server-side Python SDK and the implementation
home for the opt-in client-side MCP peer adapter. It is intended for backend
services, workers, automation jobs, CI, evaluation pipelines, controlled
notebooks and governed MCP-consuming clients. It is not for browsers, mobile
apps, or frontend bundles.

Package names:

- Distribution: `zenture`
- Import package: `zenture`
- Sync client: `ZentureClient`
- Async client: `AsyncZentureClient`

Public product references:

- Users create API tokens at `https://ai.zenture.app/profile?tab=api-tokens`.
- Human-facing API documentation lives at
  `https://www.zenture.app/developers`.
- The committed `openapi/zenture-public-api-v1.openapi.json` file remains the
  SDK-local contract source of truth for implementation and drift tests.

## Architecture Map

- `src/zenture/client.py` and `src/zenture/async_client.py`: public clients.
- `src/zenture/_resources/`: private resource implementation classes backing
  public `client.<resource>` attributes for operations, chat, input wizard,
  evaluations, models, billing, usage, limits, and helloworld.
- `src/zenture/_transport/`: private sync/async `httpx` transport layer.
- `src/zenture/_mcp/`: opt-in MCP client transport ports, official Streamable
  HTTP binding and typed peer Run adapter; it owns no OAuth, Backend, Engine or
  persistence authority.
- `src/zenture/_contract/`: internal OpenAPI-derived Pydantic models.
- `src/zenture/errors.py`: typed SDK exceptions.
- `src/zenture/polling.py`: polling policy primitives.
- `src/zenture/retries.py`: retry decision policy.
- `src/zenture/idempotency.py`: idempotency-key helpers.
- `src/zenture/redaction.py`: secret redaction helpers.

## Public vs Internal Surface

Only `ZentureClient`, `AsyncZentureClient`, and `__version__` are top-level
public exports.
Do not export internal `_contract` models from `zenture.__init__`.

The `_mcp` namespace remains an opt-in implementation surface. Its bearer input
is caller-provided;
the SDK does not issue, refresh, revoke, persist or log MCP credentials.

Resource methods such as `client.chat.run(...)`, `client.models.list(...)`, and
`client.operations.wait(...)` are the ergonomic public API. `_transport`,
`_resources`, and `_contract` are implementation namespaces even when tests
import them.

## Contract drift

The OpenAPI artifact in `openapi/zenture-public-api-v1.openapi.json` is the
local contract source of truth. Contract tests under
`tests/integration/contract/` verify operation statuses, error codes, bounded
operation result shapes, idempotency header constraints, and excluded
user-session routes.

Use `https://www.zenture.app/developers` for human-facing API context,
but do not implement SDK behavior from website prose unless the local OpenAPI
artifact and drift tests agree.

When adding or changing a resource:

- Confirm the route exists in OpenAPI.
- Add or update internal Pydantic contract models.
- Add contract drift tests where the schema meaning matters.
- Add sync and async resource tests using `httpx.MockTransport`.
- Keep examples and docs aligned with the public surface.

When a new Engine capability is intended for client consumption, extend this
repository's canonical contract models and both peer surfaces: the API
resource and the opt-in MCP adapter. Add sync/async parity and cross-channel
acceptance tests here before release promotion; do not create a workspace-only client duplicate or move Engine/business authority into the
SDK.

## Required Commands

```bash
python3 -m ruff format --check .
python3 -m ruff check .
python3 -m mypy
python3 -m pyright
python3 -m pytest
python3 -m coverage run -m pytest
python3 -m coverage report
python3 -m build
python3 -m twine check dist/*
```

Remove local artifacts after verification: `.coverage`, `.pytest_cache`,
`.ruff_cache`, `.mypy_cache`, `dist/`, and `__pycache__/`.

## Security Red Lines

- no token logging
- no token literals in examples, docs, tests, or issue text
- no arbitrary base URL origins
- no API-token-management SDK resource
- no public helper for agentic chat mode in Public V1
- no internal `_contract` top-level exports
- no request or response body logging by default
- no private infrastructure details in public docs

## Contribution Expectations

New resources must include typed sync and async behavior, idempotency safety for
mutating calls, no real network tests, and public-safe docs/examples when the
usage surface changes.

Examples must use `ZentureClient.from_env()` or `AsyncZentureClient.from_env()`.
Mutating examples must pass explicit stable idempotency keys, preferably built with the
SDK helper. Do not create tracked `.env` files.

## Pull Request Rules

Use one primary PR type: `type: fix`, `type: feat`, `type: docs`,
`type: test`, `type: chore`, `type: refactor`, or `type: security`.

Every PR must keep public docs production-relevant, avoid internal routes, avoid
real token patterns, and preserve the private/public package boundary. Public
API changes require matching tests, README/docs updates, and changelog coverage.

Required review posture: small focused PRs, `Development CI` green, CODEOWNERS
review once branch protection is available, and squash merge only.
