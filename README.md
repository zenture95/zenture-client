<img src="https://ai.zenture.app/logo.svg" alt="zenture logo" width="180">

# The official zenture Python client

Official Python client for the zenture REST API and hosted MCP endpoint.
The default installation includes REST, hosted MCP, and native authentication;
no extra is required. API tokens stay bound to REST, and account OAuth stays
bound to MCP. The server retains authorization, Run execution, billing, and
tenant authority; this package provides no local MCP server.

`zenture` is for backend services, automation jobs, evaluation pipelines,
CI tasks, and controlled notebook environments. zenture API tokens are
server-side credentials. Do not put them in browsers, mobile apps, frontend
bundles, public notebooks, logs, analytics, traces, or customer-visible errors.

## Source migration

This is a hard source cutover: install `zenture` and import `ZentureClient` or
`AsyncZentureClient` from `zenture`. Replace the former `Zenture` and
`AsyncZenture` class names in application code; no legacy aliases are provided.
The repository is `zenture95/zenture-client`. API-token access and the existing
caller-provided bearer MCP adapter retain their behavior. OAuth discovery,
login, refresh and credential storage are implemented by this client for the
hosted MCP endpoint (see [MCP and native login](#mcp-and-native-login)); API
tokens stay a separate credential.
Previously published artifacts remain the return path if the source migration
cannot yet be applied; do not install both distributions into one environment.
Candidate artifact checks do not establish public PyPI availability.
See the [migration guide](./docs/migration.md) for uninstall-first upgrade
commands, source changes, the exact published return baseline, and verification
limits.

## Package Names

- Distribution: `zenture`
- Import package: `zenture`
- Sync client: `ZentureClient`
- Async client: `AsyncZentureClient`
- Supported Python: CPython 3.11, 3.12, 3.13, and 3.14 (Python 3.14 is declared; the CI matrix covers it but has not yet run; there is no upper version cap)
- Command line: `zenture`

## Installation

```bash
pip install zenture
```

`zenture` is not published to PyPI yet, so `pip install zenture` will only work
after the first public release. Until then install from a checkout of this
repository (or from a wheel the repository owner gives you):

```bash
git clone https://github.com/zenture95/zenture-client.git
cd zenture-client
python -m pip install .
```

The installation includes the `zenture` command and the `zenture.auth` and
`zenture.mcp` modules; no extra is needed.

Run `zenture --version` to print the installed package version and exit without
logging in or accessing the credential store or network.

## Authentication

Create API tokens in the existing zenture webapp with an existing account:
`https://ai.zenture.app/profile?tab=api-tokens`. The SDK does not provide an
API-token management surface.

Keep the token outside Python source code:

```bash
# .env, not committed
ZENTURE_API_KEY=your_webapp_created_api_token
```

```bash
set -a
. ./.env
set +a
```

The SDK intentionally does not parse `.env` files. Load environment variables
through your runtime, deployment platform, secrets manager, or preferred local
loader. Public SDK usage targets the production API origin:
`https://api.zenture.app`.

## MCP and native login

Besides the REST client, `zenture` talks to the hosted zenture MCP endpoint as a
peer channel. It authorizes *you* (a zenture account), not an API token: nothing
is read from `ZENTURE_API_KEY` and the REST client is unchanged.

Log in once from a terminal:

```bash
zenture auth login                 # opens your browser (PKCE, loopback redirect)
zenture auth login --device        # headless: shows a code to enter on another device
zenture auth login --session-only  # verify the login but store nothing
zenture auth status                # verify the stored authorization
zenture auth clear                 # delete the local record only
```

- **Explicit login only.** Ordinary calls such as `McpClient.connect()` never open a
  browser and never start a login. Without a usable authorization they raise
  `AuthorizationRequired`; you decide when to run `zenture auth login`.
- **One stored account.** This machine keeps one stored account per user profile
  (one stored account, one connection). Logging in again replaces it.
- **Device login** waits at most 10 minutes for approval and is never started
  automatically when the browser is unavailable.
- **`zenture auth clear` is local.** It deletes only the record on this machine.
  Revoke the connection itself in zenture under
  *Zugriff & Sicherheit -> Verbindungen*.
- **Secure storage.** The authorization is kept in the operating system's
  credential store. `--session-only` / `session_only=True` keeps it in memory for
  the current process instead.

| Platform | Credential store | Status |
|---|---|---|
| macOS | Keychain | proven |
| Windows | Credential Manager | implemented, not yet verified |
| Linux | Secret Service (for example GNOME Keyring) | implemented, not yet verified |

For deliberate Run recovery, save an explicit `idempotency_key` before calling
`run` and reuse it with the same request. The named `run.idempotency_key` receipt
is excluded from default model serialization. Cancellation requires the key to
have been saved beforehand; receipts do not prove completion or billing. See
[Run idempotency](./docs/mcp-client.md#run-idempotency).

The six MCP tools are `run`, `attach_artifact`, `list_runs`, `get_run`,
`cancel_run` and `record_run_outcome`, available on both peers.

Synchronous (from ordinary code, not from inside a running event loop):

```python
from zenture.mcp import McpClient

with McpClient.connect() as client:  # uses the stored authorization
    client.require_product_tools()
    run = client.run(
        task="Review the selected answer",
        artifact={"type": "text", "value": "selected answer"},
    )
    print(client.get_run(run.run_id).run.status)
```

Asynchronous, with an explicit login in the same program:

```python
import asyncio

from zenture.auth import login_async
from zenture.mcp import AsyncMcpClient


async def main() -> None:
    with await login_async() as session:
        async with AsyncMcpClient.connect(session=session) as client:
            page = await client.list_runs(limit=5)
            print(len(page.runs))


asyncio.run(main())
```

Runnable variants live in [`examples/`](./examples/): `mcp_async_login.py`,
`mcp_sync_stored_login.py` and `mcp_device_login.py` (each supports `--help`).

### Errors and next actions

Import these from `zenture.auth`. Messages carry a fixed code, never a credential.

| Error | Meaning | What to do |
|---|---|---|
| `AuthorizationRequired` | No stored authorization, or it can no longer be used (for example after an interrupted refresh or a revoked connection) | Run `zenture auth login` |
| `AuthUnavailable` | Authorization could not be checked now; the stored state is unchanged | Try again later |
| `SecureStoreUnavailable` | No protected credential store on this system | Use `zenture auth login --session-only` |
| `PermissionDenied` | The server refused access for this authorization (HTTP 403) | Contact the zenture account owner; logging in again does not widen access |
| `LoginCancelled` | The authorization was declined, cancelled or stopped | Nothing was connected; run the login again if you meant it |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success (status: connected) |
| `1` | status: not_logged_in |
| `2` | invalid command line |
| `3` | authorization_required: log in again |
| `4` | unavailable: nothing changed; the message says whether retrying later helps |
| `5` | store_unavailable: no protected credential store (try `--session-only`) |
| `6` | login cancelled (denied or declined) |
| `7` | permission denied by the server |
| `130` | interrupted (Ctrl+C, also while waiting for a device login) |

Details: [`docs/authentication.md`](./docs/authentication.md) and
[`docs/mcp-client.md`](./docs/mcp-client.md).

## Local Scratch Workspace

For ad-hoc local experiments, create a `scratch/` directory in the repository
root:

```bash
mkdir -p scratch
```

Use it for temporary request payloads, response captures, throwaway scripts,
and local notes while testing the SDK. The directory is ignored by Git and
must not contain real API tokens, production data, customer data, or other
secrets.

Reproducible tests belong in `tests/`; reusable examples belong in `examples/`.

## API Documentation

The public API documentation lives at
`https://www.zenture.app/developers`. Treat the committed OpenAPI
artifact in this repository as the SDK's local contract source of truth and use
the public documentation as the human-facing API reference.

For copy-paste SDK usage, routes, and example response structures for every
public call, see [`docs/sdk-call-reference.md`](./docs/sdk-call-reference.md).
For raw JSON response bodies and SDK model mapping, see
[`docs/response-shapes.md`](./docs/response-shapes.md).
The MCP peer clients are documented in
[`docs/mcp-client.md`](./docs/mcp-client.md) and native login in
[`docs/authentication.md`](./docs/authentication.md).

## Agent And Interface Governance

**Repository role:** This repository implements the server-side Python REST
API client, hosted MCP peers, and explicit native authentication, all included
in the default installation. Its committed OpenAPI artifact is
the SDK-local contract mirror, not the authority for Backend product behavior
or gateway routes.

**Canonical authorities:** Follow [`AGENTS.md`](./AGENTS.md) for SDK changes.
The public gateway OpenAPI owns REST shapes; Backend owns Run status, billing,
auth, and tenant behavior. The workspace
`governance/interface-registry/registry.yml` records interface ownership,
consumers, lifecycle, and compatibility without replacing those authorities.
For cross-repository interface changes, run
`governance/interface-registry/logic/scripts/check_interface_registry.py` with
`--mode strict-workspace` from the workspace root.

## Sync Quickstart

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    print(client.helloworld())

    models = client.models.list(mode="single")
    for model in models.models:
        print(model.id, model.display_name)
```

## Async Quickstart

```python
import asyncio

from zenture import AsyncZentureClient


async def main() -> None:
    async with AsyncZentureClient.from_env() as client:
        result = await client.chat.run(
            message="Summarize this support note.",
            mode="single",
            idempotency_key="case-123-chat-single-v1",
            timeout=120.0,
        )
        print(result.status)


if __name__ == "__main__":
    asyncio.run(main())
```

## Models

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    single_models = client.models.list(mode="single")
    multi_models = client.models.list(mode="multi")
    print(single_models.models[0].id)
    print(multi_models.models[0].id)
```

## Chat

Single-model chat:

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    available_models = [
        model.id for model in client.models.list(mode="single").models if model.is_available
    ]
    if not available_models:
        raise RuntimeError("No available single-mode model for this token.")

    result = client.chat.run(
        message="Summarize this customer update.",
        mode="single",
        model=available_models[0],
        idempotency_key="case-123-chat-single-v1",
        timeout=120.0,
    )
    print(result.operation_id, result.status)
    print(result.result.amount_billed)
    print(result.result.chat_id, result.result.turn_id, result.result.model_response_id)
```

Multi-model chat:

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    available_models = [
        model.id for model in client.models.list(mode="multi").models if model.is_available
    ]
    if len(available_models) < 2:
        raise RuntimeError("At least two available multi-mode models are required.")

    result = client.chat.run(
        message="Compare these draft answers for factual consistency.",
        mode="multi",
        models=available_models[:2],
        idempotency_key="case-123-chat-multi-v1",
        timeout=120.0,
    )
    print(result.status)
    print(result.result.amount_billed)
```

Agentic chat mode is not part of Public V1. Do not document or add public
helpers for it in this SDK.

Continue a chat with a follow-up turn:

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    first = client.chat.run(
        message="Give me a concise onboarding checklist for a new API user.",
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-1", "v1"),
        timeout=120.0,
    )
    chat_id = first.result.chat_id

    follow_up = client.chat.run(
        message="Turn that checklist into three implementation steps.",
        chat_id=chat_id,
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-2", "v1"),
        timeout=120.0,
    )
    print(follow_up.result.amount_billed)
    print(follow_up.result.turn_id, follow_up.result.model_response_id)
```

Read chat turns when you need the exact `user_message`, `model_answer`, and
`model_response_id` for evaluation:

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    messages = client.chat.messages("chat_example")
    turn = messages.turns[0]
    print(turn.user_message, turn.model_answer, turn.model_response_id)
```

## Input Wizard

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    result = client.input_wizard.run(
        prompt="Improve this onboarding prompt for a support assistant.",
        idempotency_key="case-123-input-wizard-v1",
        timeout=120.0,
    )
    print(result.operation_id, result.status)
    if result.result and result.result.optimized_prompt:
        print(result.result.optimized_prompt)
```

## Evaluations

There are two evaluation flows. Keep them separate:

- **External evaluation:** the answer came from your application or another AI
  system. Send `user_message`, `ai_answer`, optional `external_id`, and optional
  `metadata`. Do not send `chat_id`, `turn_id`, or `model_response_id`.
- **Internal zenture chat evaluation:** the answer came from a zenture chat
  turn. Send `user_message`, `ai_answer`, and the AI-answer
  `model_response_id`. `chat_id` and `turn_id` are optional correlation fields,
  but if either is sent, `model_response_id` is required and must belong to the
  same zenture user.

External answer evaluation creates an external evaluation-only record. It does
not add the submitted content to normal zenture chat history. If the answer
contains sources, include them directly in `ai_answer` as Markdown links,
footnotes, or plain URLs:

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    result = client.evaluations.run(
        user_message="What is zenture?",
        ai_answer=("zenture evaluates AI outputs. [Source](https://example.com/product-brief)"),
        external_id="support-ticket-123-answer-a",
        metadata={"source": "support_bot", "answer_format": "markdown_with_sources"},
        idempotency_key=idempotency_key("support-ticket-123-answer-a", "evaluate", "v1"),
        timeout=120.0,
    )
    print(result.operation_id, result.status)
    print(result.result.amount_billed)

    detail = client.evaluations.get(result.result.evaluation_id)
    print(detail.zenture_summary)
    print(detail.sources)
```

Internal zenture chat-answer evaluation uses the AI-answer `model_response_id`.
That id is not the user-message id. Passing only `chat_id` or `turn_id` is not
enough; the API rejects that request with `422 validation_failed` before it
creates an operation or checks credits.

The SDK also validates that shape locally. `chat_id` or `turn_id` without
`model_response_id` raises Pydantic `ValidationError` before an HTTP request is
sent. A valid-looking target that the API user does not own raises
`ZentureValidationError` from the API.

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key
from pydantic import ValidationError
from zenture.errors import ZentureValidationError

with ZentureClient.from_env() as client:
    try:
        chat = client.chat.run(
            message="Draft three customer-support next steps.",
            mode="single",
            idempotency_key=idempotency_key("case-456", "chat-turn-1", "v1"),
            timeout=120.0,
        )
        chat_id = chat.result.chat_id
        turn_id = chat.result.turn_id
        model_response_id = chat.result.model_response_id or chat.result.model_response_ids[0]

        turn = next(item for item in client.chat.messages(chat_id).turns if item.turn_id == turn_id)

        evaluation = client.evaluations.run(
            user_message=turn.user_message,
            ai_answer=turn.model_answer,
            chat_id=chat_id,
            turn_id=turn_id,
            model_response_id=model_response_id,
            idempotency_key=idempotency_key(model_response_id, "evaluate", "v1"),
            timeout=120.0,
        )
        print(evaluation.result.evaluation_id, evaluation.status)
        print(evaluation.result.amount_billed)
    except ValidationError:
        # Local SDK validation, for example chat_id/turn_id without model_response_id.
        handle_invalid_evaluation_target()
    except ZentureValidationError as exc:
        # Server-side validation, for example a model_response_id not owned by this user.
        handle_api_validation_error(exc.status_code, exc.error_code, exc.request_id)
```

Use `external_id` only as optional caller-side correlation. Use
`idempotency_key` as the required retry-safety key for each mutating request.

## End-to-End Chat Evaluation

For a complete local smoke flow, use:

```bash
python3 examples/end_to_end_chat_evaluation.py
```

The script reads configuration from environment variables, calls `wallet.get()`,
selects an available single-model chat model with a Haiku preference, runs
`input_wizard`, chats, reads the generated turn, evaluates the answer, and
prints a JSON summary with per-operation `amount_billed` plus `total_billed`.

For smaller application code, `chat.run(..., include_content=True)` attaches
the matching public chat turn as `result.chat_turn`, and
`evaluations.run(..., include_detail=True)` attaches the public evaluation
detail as `result.evaluation`. These flags are explicit so normal operation
polling does not perform extra read requests.

## Account Reads

Read wallet, usage, and route limits without creating billable work:

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    wallet = client.wallet.get()
    api_usage = client.usage.get(scope="api")
    all_usage = client.usage.get(scope="all")
    limits = client.limits.get()

    print(wallet.plan, wallet.status, wallet.credits_available.amount)
    print(api_usage.operation_count, all_usage.operation_count)
    print(limits.operation_statuses)
```

## Operation Polling

Low-level create methods return an operation immediately. Use
`client.operations.wait(...)` when you want to poll explicitly.

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    operation = client.chat.create_operation(
        message="Review this response.",
        mode="single",
        idempotency_key="case-123-chat-create-v1",
    )
    final_operation = client.operations.wait(operation.operation_id, timeout=120.0)
    print(final_operation.status)
```

Terminal statuses are `succeeded`, `failed`, `cancelled`, and `expired`.

If `.run(...)` creates an operation and local polling later times out or is
stopped, the exception exposes `operation_id` and `idempotency_key` attributes.
Use `client.operations.get(exc.operation_id)` or retry with the same
`exc.idempotency_key`. Do not retry a billable mutation with a new key.

For a successful settled Product Run, the canonical status is `completed`.
`client.runs.list(status=["completed"])` selects that success category;
`succeeded` remains a deprecated list-filter input and a historical Run response
value. Async Operations still use `succeeded` for terminal success.

For public Runs, `client.runs.wait(run_id)` is always finite. When the first
response includes `deadline_at`, an omitted timeout ends at that deadline plus
the fixed 35-second recovery allowance and one polling interval. A caller
supplied finite timeout remains authoritative. The timeout exception includes
the last status and observed deadline as safe metadata. Run event streaming
remains an optional lower-latency path; `runs.wait(...)` is the bounded polling
fallback when a stream is unavailable.

## Pagination

List-style read helpers support `limit` and `cursor`. The default page size is
`limit=50`, the maximum is `100`, and cursors are opaque strings. Responses
include `next_cursor`; `None` means there is no further page.

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    page = client.chat.list(limit=50)
    print(page.next_cursor)

    for chat in client.chat.iter(limit=50):
        print(chat.chat_id)
```

Available iterators:

- `client.chat.iter(limit=50, cursor=None)`
- `client.chat.iter_messages(chat_id, limit=50, cursor=None)`
- `client.evaluations.iter(limit=50, cursor=None)`

## Idempotency

Mutating routes require `Idempotency-Key`. Use stable caller-owned keys that do
not contain prompts, answers, API tokens, customer PII, or request bodies.

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    key = idempotency_key("case-123", "chat-turn-1", "v1")
    result = client.chat.run(
        message="Create a concise summary.",
        idempotency_key=key,
        timeout=120.0,
    )
    print(result.idempotency_key)
```

Example keys:

- `idempotency_key("case-123", "chat-turn-1", "v1")`
- `idempotency_key("case-123", "chat-turn-2", "v1")`
- `idempotency_key("support-ticket-123-answer-a", "evaluate", "v1")`
- `idempotency_key("response_abc123", "evaluate", "v1")`

## Errors

```python
from zenture import ZentureClient
from zenture.errors import (
    ZentureAPIError,
    ZentureInsufficientCreditsError,
    ZenturePollingTimeoutError,
    ZentureRateLimitError,
)

with ZentureClient.from_env() as client:
    try:
        result = client.chat.run(
            message="Summarize this incident.",
            idempotency_key="case-123-error-example-v1",
            timeout=120.0,
        )
        print(result.status)
    except ZenturePollingTimeoutError as exc:
        print(exc.operation_id)
    except ZentureInsufficientCreditsError:
        wallet = client.wallet.get()
        print(wallet.credits_available)
    except ZentureRateLimitError as exc:
        print(exc.retry_after)
    except ZentureAPIError as exc:
        print(exc.request_id)
```

SDK exceptions redact sensitive content. Request and response bodies are not
included in exception strings.

## Timeout, Retry, Polling, and Rate Limits

- SDK-owned HTTP clients use explicit timeouts: connect `5s`,
  read/write/pool `30s`.
- `operations.wait(timeout=...)` and `.run(timeout=...)` use a total operation
  polling budget, not the raw HTTP read timeout.
- Polling uses deterministic intervals: `initial_interval=1s`, doubled up to
  `max_interval=8s`, with no jitter.
- Manual `GET /v1/operations/{operation_id}` polling should use the same
  `1s -> 2s -> 4s -> 8s` cadence, should not poll faster than once per second
  per operation, and must stop at terminal status.
- Retry default is `max_retries=2`.
- Retryable HTTP responses include `429`, `500`, `502`, `503`, and `504` when
  the public error code is retryable.
- `Retry-After` is honored before deterministic exponential backoff.
- Mutating requests are retried only when an `Idempotency-Key` is present.

## Base URL Policy

- Default production API origin: `https://api.zenture.app`
- Never derive `base_url` from user input.
- URL credentials, paths, query strings, fragments, and arbitrary HTTPS origins
  are rejected.

## Public V1 Scope

- No browser, mobile, or frontend bundle usage.
- No API-token management surface.
- No public helper for agentic chat mode in Public V1.
- No top-level exports from internal `_contract` modules.

## Local Verification

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

## Security

See [`SECURITY.md`](./SECURITY.md) for vulnerability reporting and security
expectations. Do not publish API tokens in issues, logs, screenshots, or support
requests.

## License

Apache License 2.0. See [`LICENSE`](./LICENSE).
