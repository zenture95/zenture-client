# SDK Call Reference

This page is the copy-paste oriented reference for every public SDK call. Each
section shows the SDK method, the underlying public API route, and the typical
response object shape.

For raw JSON response bodies and SDK model mapping, see
[`response-shapes.md`](./response-shapes.md).

All mutating routes require an `Idempotency-Key`. The SDK methods expose this as
`idempotency_key`. Use stable keys from your own system for retries of the same
request body. Do not include prompts, answers, API tokens, or customer PII in
idempotency keys.

High-level `.run(...)` helpers create work and wait for a terminal operation.
Low-level `.create(...)` or `.create_operation(...)` helpers return the initial
`PublicOperationResponse` immediately.

## Response Wrappers

`PublicOperationResponse` is returned by low-level mutating calls:

```python
{
    "operation_id": "op_example",
    "status": "queued",  # queued, running, succeeded, failed, cancelled, expired
    "result": None,
    "error": None,
}
```

`OperationRunResult` is returned by `.run(...)` helpers:

```python
{
    "operation_id": "op_example",
    "status": "succeeded",
    "result": {"result_type": "chat"},
    "error": None,
    "idempotency_key": "case-123-chat-turn-1-v1",
    "last_request_id": None,
}
```

Failed terminal operations keep the operation response shape:

```python
{
    "operation_id": "op_example",
    "status": "failed",
    "result": None,
    "error": {
        "code": "chat_failed",
        "message": "Chat execution failed.",
    },
}
```

## Connectivity

### `client.helloworld()`

Route: `GET /v1/helloworld`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    response = client.helloworld()
    print(response)
```

Example response:

```python
"# Hello from zenture"
```

Use this only as a cheap connectivity check. It does not create billable work.

## Models

### `client.models.list(...)`

Route: `GET /v1/models`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    response = client.models.list(mode="single")
    for model in response.models:
        print(model.id, model.display_name, model.is_available)
```

Example response:

```python
{
    "models": [
        {
            "id": "model_public_single",
            "display_name": "Public Single",
            "modes": ("single",),
            "capabilities": ("chat",),
            "is_default": True,
            "is_available": True,
            "cost_class": "standard",
            "provider_display_name": "Example Provider",
            "max_input_tokens": 8192,
        }
    ]
}
```

`wallet.get()` returns the current public plan/status projection and available
credits. It does not expose Stripe, invoice, pricing, ledger, or payment-method
details. Per-operation costs are exposed as `amount_billed` on completed chat
and evaluation results. `usage.get()` returns operation counts, not a credit
ledger.

Use `mode="single"` for single-model chat and `mode="multi"` for multi-model
chat. Omit `mode` to list all visible public models.

## Chat

### `client.chat.create(...)`

Route: `POST /v1/chat`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    operation = client.chat.create(
        message="Draft three support next steps.",
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-1", "v1"),
    )
    print(operation.operation_id, operation.status)
```

Example response:

```python
{
    "operation_id": "op_chat123",
    "status": "queued",
    "result": None,
    "error": None,
}
```

Use `client.operations.wait(operation.operation_id)` to poll this operation.

### `client.chat.create_operation(...)`

Route: `POST /v1/chat`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    operation = client.chat.create_operation(
        message="Compare these draft answers.",
        mode="multi",
        models=["model_public_multi_a", "model_public_multi_b"],
        idempotency_key=idempotency_key("case-123", "chat-multi-turn-1", "v1"),
    )
    print(operation.operation_id)
```

Example response:

```python
{
    "operation_id": "op_chat_multi123",
    "status": "queued",
    "result": None,
    "error": None,
}
```

`create(...)` and `create_operation(...)` are equivalent for chat.

### `client.chat.run(...)`

Route: `POST /v1/chat`, then `GET /v1/operations/{operation_id}`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    result = client.chat.run(
        message="Draft three support next steps.",
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-1", "v1"),
        timeout=120.0,
        include_content=True,
    )
    print(result.result.amount_billed)
    print(result.result.chat_id, result.result.turn_id, result.result.model_response_id)
    print(result.chat_turn.user_message, result.chat_turn.model_answer)
```

Example response:

```python
{
    "operation_id": "op_chat123",
    "status": "succeeded",
    "result": {
        "result_type": "chat",
        "chat_id": "chat_example",
        "turn_id": "turn_example",
        "model_response_id": "resp_example",
        "model_response_ids": ("resp_example",),
        "status": "completed",
        "completed_at": "2026-06-22T10:00:00Z",
        "amount_billed": {"amount": "4.41", "unit": "credits"},
    },
    "error": None,
    "idempotency_key": "case-123-chat-turn-1-v1",
    "last_request_id": None,
}
```

Use `chat_id` for follow-up turns. Use `model_response_id` as the internal
AI-answer id when evaluating this zenture-generated answer.

Set `include_content=True` when the caller needs the matching public chat turn
in the same SDK call. The SDK then performs one additional
`GET /v1/chats/{chat_id}/messages` request and attaches the matching turn as
`result.chat_turn`. Leave it unset for lower-latency operation polling.

The SDK exposes chat operation results as flat fields on `result`. If a public
operation read contains the same ids nested under a known result wrapper, the
SDK normalizes it before returning the typed object.

Follow-up turn:

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    first = client.chat.run(
        message="Create a short onboarding checklist.",
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-1", "v1"),
    )
    follow_up = client.chat.run(
        message="Turn it into three implementation steps.",
        chat_id=first.result.chat_id,
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-2", "v1"),
    )
    print(follow_up.result.amount_billed)
    print(follow_up.result.chat_id, follow_up.result.turn_id)
```

### `client.chat.list(...)`

Route: `GET /v1/chats`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    page = client.chat.list(limit=50)
    print(page.next_cursor)
```

Example response:

```python
{
    "chats": (
        {
            "chat_id": "chat_example",
            "created_at": "2026-06-22T10:00:00Z",
            "title": "Support next steps",
            "updated_at": "2026-06-22T10:01:00Z",
        },
    ),
    "next_cursor": None,
}
```

`next_cursor is None` means there is no next page.

### `client.chat.iter(...)`

Route: repeated `GET /v1/chats`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    for chat in client.chat.iter(limit=50):
        print(chat.chat_id, chat.title)
```

Example yielded item:

```python
{
    "chat_id": "chat_example",
    "created_at": "2026-06-22T10:00:00Z",
    "title": "Support next steps",
    "updated_at": "2026-06-22T10:01:00Z",
}
```

The iterator follows `next_cursor` until the API returns `None`.

### `client.chat.get(...)`

Route: `GET /v1/chats/{chat_id}`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    response = client.chat.get("chat_example")
    print(response.chat.chat_id)
```

Example response:

```python
{
    "chat": {
        "chat_id": "chat_example",
        "created_at": "2026-06-22T10:00:00Z",
        "title": "Support next steps",
        "updated_at": "2026-06-22T10:01:00Z",
    },
    "latest_turns": (
        {
            "turn_id": "turn_example",
            "created_at": "2026-06-22T10:01:00Z",
            "user_message": "Draft three support next steps.",
            "model_answer": "1. Confirm context. 2. Prioritize. 3. Follow up.",
            "model_response_id": "resp_example",
            "model_response_ids": ("resp_example",),
        },
    ),
}
```

### `client.chat.messages(...)`

Route: `GET /v1/chats/{chat_id}/messages`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    response = client.chat.messages("chat_example", limit=50)
    turn = response.turns[0]
    print(turn.user_message, turn.model_answer, turn.model_response_id)
```

Example response:

```python
{
    "chat_id": "chat_example",
    "turns": (
        {
            "turn_id": "turn_example",
            "created_at": "2026-06-22T10:01:00Z",
            "user_message": "Draft three support next steps.",
            "model_answer": "1. Confirm context. 2. Prioritize. 3. Follow up.",
            "model_response_id": "resp_example",
            "model_response_ids": ("resp_example",),
        },
    ),
    "next_cursor": None,
}
```

Use `user_message`, `model_answer`, and `model_response_id` together for
internal evaluation of an existing zenture chat answer.

### `client.chat.iter_messages(...)`

Route: repeated `GET /v1/chats/{chat_id}/messages`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    for turn in client.chat.iter_messages("chat_example", limit=50):
        print(turn.turn_id, turn.model_response_id)
```

Example yielded item:

```python
{
    "turn_id": "turn_example",
    "created_at": "2026-06-22T10:01:00Z",
    "user_message": "Draft three support next steps.",
    "model_answer": "1. Confirm context. 2. Prioritize. 3. Follow up.",
    "model_response_id": "resp_example",
    "model_response_ids": ("resp_example",),
}
```

## Input Wizard

### `client.input_wizard.create(...)`

Route: `POST /v1/input-wizard`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    operation = client.input_wizard.create(
        prompt="Improve this onboarding prompt for a support assistant.",
        idempotency_key=idempotency_key("case-123", "input-wizard", "v1"),
    )
    print(operation.operation_id, operation.status)
```

Example response:

```python
{
    "operation_id": "op_wizard123",
    "status": "succeeded",
    "result": {
        "result_type": "input_wizard",
        "resource_id": "wizard_example",
        "input_wizard_id": "wizard_example",
        "status": "completed",
        "optimized_prompt": "You are a support assistant. Ask one clarifying question...",
        "wizard_session_id": "wizard_session_example",
    },
    "error": None,
}
```

The direct Input Wizard response includes `optimized_prompt` when the operation
finishes during the request. Operation reads only expose safe references.

### `client.input_wizard.run(...)`

Route: `POST /v1/input-wizard`, then `GET /v1/operations/{operation_id}` if
needed

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    result = client.input_wizard.run(
        prompt="Improve this onboarding prompt for a support assistant.",
        idempotency_key=idempotency_key("case-123", "input-wizard", "v1"),
        timeout=120.0,
    )
    print(result.result.optimized_prompt or result.result.resource_id)
```

Example response when the direct route returns the optimized prompt:

```python
{
    "operation_id": "op_wizard123",
    "status": "succeeded",
    "result": {
        "result_type": "input_wizard",
        "resource_id": "wizard_example",
        "input_wizard_id": "wizard_example",
        "status": "completed",
        "optimized_prompt": "You are a support assistant. Ask one clarifying question...",
        "wizard_session_id": "wizard_session_example",
    },
    "error": None,
    "idempotency_key": "case-123-input-wizard-v1",
    "last_request_id": None,
}
```

Example response when only the safe operation result is available from polling:

```python
{
    "operation_id": "op_wizard123",
    "status": "succeeded",
    "result": {
        "result_type": "input_wizard",
        "resource_id": "wizard_example",
    },
    "error": None,
    "idempotency_key": "case-123-input-wizard-v1",
    "last_request_id": None,
}
```

## Evaluations

Evaluation always needs the pair that should be judged: `user_message` and
`ai_answer`.

For internal zenture chat answers, pass `model_response_id`. That is the
AI-answer id, not the user-message id. `chat_id` and `turn_id` are useful
correlation fields. If either `chat_id` or `turn_id` is provided,
`model_response_id` is required and must reference an owned zenture chat
response.

For external answers, omit `chat_id`, `turn_id`, and `model_response_id`. Use
optional `external_id` only as caller-side correlation. External evaluations are
not added to normal zenture chat history.

Invalid internal targets are rejected as `422 validation_failed` before zenture
creates an operation or checks credits. A terminal failed evaluation operation
means execution started and then failed; it should not be used to detect a
malformed internal-vs-external payload.

The SDK validates the most common internal/external mix-up locally: if
`chat_id` or `turn_id` is supplied without `model_response_id`,
`client.evaluations.create(...)` and `client.evaluations.run(...)` raise
Pydantic `ValidationError` before sending an HTTP request. Server-side target
validation, such as a `model_response_id` that does not belong to the API user,
raises `ZentureValidationError` with `status_code == 422` and
`error_code == "validation_failed"`.

If the external answer contains sources, include them directly in `ai_answer`
as Markdown links, footnotes, or plain URLs. The SDK does not require a separate
`sources` field for Public V1; zenture evaluates the submitted Markdown answer
as one payload. Keep source text concise and avoid pasting private documents
unless your application is allowed to send them to zenture.

### `client.evaluations.create(...)`

Route: `POST /v1/evaluate`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    operation = client.evaluations.create(
        user_message="What does zenture do?",
        ai_answer="zenture evaluates AI outputs.",
        external_id="support-ticket-123-answer-a",
        metadata={"source": "support_bot"},
        idempotency_key=idempotency_key("support-ticket-123-answer-a", "evaluate", "v1"),
    )
    print(operation.operation_id, operation.status)
```

Example response:

```python
{
    "operation_id": "op_eval123",
    "status": "queued",
    "result": None,
    "error": None,
}
```

### `client.evaluations.run(...)` for external answers

Route: `POST /v1/evaluate`, then `GET /v1/operations/{operation_id}`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key

with ZentureClient.from_env() as client:
    result = client.evaluations.run(
        user_message="What changed in today's German financial news?",
        ai_answer=(
            "Key points:\n"
            "- German equities moved after fresh macro data. "
            "[Source](https://example.com/market-update)\n"
            "- Banks watched ECB commentary closely. "
            "Source: https://example.com/ecb-briefing"
        ),
        external_id="support-ticket-123-answer-a",
        metadata={"source": "support_bot", "answer_format": "markdown_with_sources"},
        idempotency_key=idempotency_key("support-ticket-123-answer-a", "evaluate", "v1"),
        timeout=120.0,
        include_detail=True,
    )
    print(result.result.evaluation_id, result.result.score)
    print(result.evaluation.status, result.evaluation.score)
```

Example response:

```python
{
    "operation_id": "op_eval123",
    "status": "succeeded",
    "result": {
        "result_type": "evaluation",
        "evaluation_id": "eval_example",
        "score": 86.0,
        "status": "succeeded",
        "target_kind": "api_external_chat_message",
        "completed_at": "2026-06-22T10:05:00Z",
        "amount_billed": {"amount": "2.00", "unit": "credits"},
    },
    "error": None,
    "idempotency_key": "support-ticket-123-answer-a-evaluate-v1",
    "last_request_id": None,
}
```

Set `include_detail=True` when the caller wants the public evaluation detail in
the same SDK call. The SDK then performs one additional
`GET /v1/evaluations/{evaluation_id}` request and attaches it as
`result.evaluation`. Leave it unset when the operation result is enough.

### `client.evaluations.run(...)` for internal zenture chat answers

Route: `POST /v1/evaluate`, then `GET /v1/operations/{operation_id}`

```python
from zenture import ZentureClient
from zenture.idempotency import idempotency_key
from pydantic import ValidationError
from zenture.errors import ZentureValidationError

with ZentureClient.from_env() as client:
    try:
        chat = client.chat.run(
            message="Draft three support next steps.",
            mode="single",
            idempotency_key=idempotency_key("case-456", "chat-turn-1", "v1"),
        )
        chat_id = chat.result.chat_id
        turn_id = chat.result.turn_id
        model_response_id = chat.result.model_response_id or chat.result.model_response_ids[0]

        turn = next(
            item for item in client.chat.messages(chat_id).turns
            if item.turn_id == turn_id
        )

        result = client.evaluations.run(
            user_message=turn.user_message,
            ai_answer=turn.model_answer,
            chat_id=chat_id,
            turn_id=turn_id,
            model_response_id=model_response_id,
            idempotency_key=idempotency_key(model_response_id, "evaluate", "v1"),
            timeout=120.0,
            include_detail=True,
        )
        handle_evaluation_result(result.result.evaluation_id, result.result.target_kind)
    except ValidationError:
        handle_invalid_evaluation_target()
    except ZentureValidationError as exc:
        handle_api_validation_error(exc.status_code, exc.error_code, exc.request_id)
```

Example response:

```python
{
    "operation_id": "op_eval_internal123",
    "status": "succeeded",
    "result": {
        "result_type": "evaluation",
        "evaluation_id": "eval_internal_example",
        "score": 91.0,
        "status": "succeeded",
        "target_kind": "chat_model_response",
        "completed_at": "2026-06-22T10:05:00Z",
        "amount_billed": {"amount": "2.00", "unit": "credits"},
    },
    "error": None,
    "idempotency_key": "resp-example-evaluate-v1",
    "last_request_id": None,
}
```

### `client.evaluations.get(...)` result details

Route: `GET /v1/evaluations/{evaluation_id}`

The detail response uses user-facing zenture field names:

- `amount_billed`: display credit amount charged for the evaluation, for example `{"amount": "2.00", "unit": "credits"}`.
- `zenture_summary`: per-model summary of the evaluation.
- `zenture_suggestion`: per-model improvement suggestion.
- `zenture_kpi_details`: per-model KPI detail map.
- `results`: one row per KPI with `model_id`, `kpi_key`, `value`, and safe `analysis` when available.
- `sources`: per-model source verification results. Source rows can include `status`/`retrievalStatus` such as `available`, `limited`, `unavailable`, or `unverified`, plus `httpStatus`, `verdict`, `url`, `hostname`, `securityLabel`, `accessibilityScore`, and `responseTimeMs` when available.

```python
detail = client.evaluations.get("eval_example")
print(detail.amount_billed.amount)
print(detail.zenture_summary)
print(detail.sources)
```

Example response:

```python
{
    "evaluation_id": "eval_example",
    "status": "completed",
    "score": 86.0,
    "created_at": "2026-06-22T10:05:00Z",
    "amount_billed": {"amount": "2.00", "unit": "credits"},
    "zenture_summary": {
        "public_api": "The answer is relevant but relies on unavailable sources."
    },
    "zenture_suggestion": {
        "public_api": "Replace unavailable links with accessible primary sources."
    },
    "zenture_kpi_details": {
        "public_api": {
            "src_acces": {"value": 0, "analysis": "The cited URL returned 404."}
        }
    },
    "results": [
        {
            "model_id": "public_api",
            "kpi_key": "src_acces",
            "value": 0,
            "analysis": "The cited URL returned 404.",
        }
    ],
    "sources": {
        "public_api": [
            {
                "url": "https://example.com/market-update",
                "status": "unavailable",
                "retrievalStatus": "unavailable",
                "httpStatus": 404,
                "verdict": "unverifiable",
            }
        ]
    },
}
```

### `client.evaluations.list(...)`

Route: `GET /v1/evaluations`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    page = client.evaluations.list(limit=50)
    print(page.next_cursor)
```

Example response:

```python
{
    "evaluations": (
        {
            "evaluation_id": "eval_example",
            "status": "succeeded",
            "score": 86.0,
            "created_at": "2026-06-22T10:05:00Z",
        },
    ),
    "next_cursor": None,
}
```

### `client.evaluations.iter(...)`

Route: repeated `GET /v1/evaluations`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    for evaluation in client.evaluations.iter(limit=50):
        print(evaluation.evaluation_id, evaluation.score)
```

Example yielded item:

```python
{
    "evaluation_id": "eval_example",
    "status": "succeeded",
    "score": 86.0,
    "created_at": "2026-06-22T10:05:00Z",
}
```

### `client.evaluations.get(...)`

Route: `GET /v1/evaluations/{evaluation_id}`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    evaluation = client.evaluations.get("eval_example")
    print(evaluation.status, evaluation.score)
```

Example response:

```python
{
    "evaluation_id": "eval_example",
    "status": "succeeded",
    "score": 86.0,
    "created_at": "2026-06-22T10:05:00Z",
}
```

## Operations

### `client.operations.get(...)`

Route: `GET /v1/operations/{operation_id}`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    operation = client.operations.get("op_example")
    print(operation.status, operation.result)
```

Example response for chat:

```python
{
    "operation_id": "op_chat123",
    "status": "succeeded",
    "result": {
        "result_type": "chat",
        "chat_id": "chat_example",
        "turn_id": "turn_example",
        "model_response_id": "resp_example",
        "model_response_ids": ("resp_example",),
    },
    "error": None,
}
```

Example response for Input Wizard polling:

```python
{
    "operation_id": "op_wizard123",
    "status": "succeeded",
    "result": {
        "result_type": "input_wizard",
        "resource_id": "wizard_example",
    },
    "error": None,
}
```

Operation polling intentionally exposes safe references. It should not be used
as a prompt or answer body store.

### `client.operations.wait(...)`

Route: repeated `GET /v1/operations/{operation_id}`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    final_operation = client.operations.wait("op_example", timeout=120.0)
    print(final_operation.status)
```

Example response:

```python
{
    "operation_id": "op_example",
    "status": "succeeded",
    "result": {
        "result_type": "evaluation",
        "evaluation_id": "eval_example",
        "score": 86.0,
        "status": "succeeded",
    },
    "error": None,
}
```

If local polling times out or is stopped after create succeeded, the raised
exception exposes `operation_id` and `idempotency_key` attributes for recovery.

Polling cadence: the SDK waits `1s`, then `2s`, then `4s`, then caps at `8s`
between reads. If you call `GET /v1/operations/{operation_id}` yourself, use the
same cadence, do not poll faster than once per second per operation, and honor
`Retry-After` on `429` responses.

## Account Reads

### `client.wallet.get()`

Route: `GET /v1/wallet`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    wallet = client.wallet.get()
    print(wallet.plan, wallet.status, wallet.credits_available.amount)
```

Example response:

```python
{
    "plan": "pro",
    "status": "active",
    "credits_available": {"amount": "123.45", "unit": "credits"},
    "current_period_end": "2026-07-22T00:00:00Z",
}
```

### `client.usage.get(...)`

Route: `GET /v1/usage`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    api_usage = client.usage.get(scope="api")
    all_usage = client.usage.get(scope="all")
    print(api_usage.operation_count, all_usage.operation_count)
```

Example response:

```python
{
    "scope": "api",
    "operation_count": 42,
}
```

`scope="api"` returns API-token-visible usage. `scope="all"` returns all usage
counters visible to the token.

### `client.limits.get()`

Route: `GET /v1/limits`

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    limits = client.limits.get()
    print(limits.routes["POST /v1/evaluate"].idempotency_required)
```

Example response:

```python
{
    "routes": {
        "POST /v1/evaluate": {
            "auth_mode": "api_token",
            "scopes": ("evaluation:run",),
            "cost_class": "evaluation_write",
            "idempotency_required": True,
            "cors_policy": "server_only",
            "max_body_bytes": 100000,
            "rate_limit_per_minute": 30,
        }
    },
    "operation_statuses": (
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ),
}
```

## Async Client

`AsyncZentureClient` exposes the same resources and response shapes. Only `await` and
`async with` change:

```python
from zenture import AsyncZentureClient
from zenture.idempotency import idempotency_key

async with AsyncZentureClient.from_env() as client:
    result = await client.evaluations.run(
        user_message="What does zenture do?",
        ai_answer="zenture evaluates AI outputs.",
        external_id="support-ticket-123-answer-a",
        idempotency_key=idempotency_key("support-ticket-123-answer-a", "evaluate", "v1"),
    )
    print(result.result.evaluation_id)
```

Async iterators use `async for`:

```python
from zenture import AsyncZentureClient

async with AsyncZentureClient.from_env() as client:
    async for evaluation in client.evaluations.iter(limit=50):
        print(evaluation.evaluation_id)
```
