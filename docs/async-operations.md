# Async Operations

Some zenture routes create asynchronous operations. The SDK exposes three usage
levels:

- `create(...)` or `create_operation(...)`: create an operation and return the
  initial `PublicOperationResponse`.
- `operations.wait(operation_id, ...)`: poll an existing operation until it is
  terminal.
- `.run(...)`: create an operation and wait for it with a single helper.

Terminal statuses are `succeeded`, `failed`, `cancelled`, and `expired`.
Non-terminal statuses are `queued` and `running`.

## Result Shape

`.run(...)` returns `OperationRunResult` with:

- `operation_id`
- `status`
- `result`
- `error`
- `idempotency_key`
- `last_request_id`

Failed terminal operations return `status="failed"` with an operation `error`.
They are not converted into response parsing failures.

For chat operations, `result` contains safe ids such as `chat_id`, `turn_id`,
and `model_response_id`/`model_response_ids`. Use `chat_id` for follow-up turns
and `model_response_id` for evaluating a zenture chat answer. For external
evaluations, `result` contains an evaluation projection and no chat-history ids.

Completed chat and evaluation operations include `amount_billed` when billing
has been recorded:

```python
{
    "amount": "4.41",
    "unit": "credits",
}
```

`amount` is the user-facing zenture credit amount with two decimal places.

## Timeout and Stop

`timeout` is a total local polling budget, not an HTTP read timeout. Sync
polling accepts `stop: Callable[[], bool] | None`. Async polling propagates
`asyncio.CancelledError` from task cancellation and accepts the same local stop
callable.

When create succeeds but local polling times out or is stopped, the exception
contains `operation_id` and `idempotency_key` attributes for recovery. These
attributes are not interpolated into exception strings.

## Polling Frequency

Prefer `.run(...)` or `operations.wait(...)`; both use the SDK polling policy:
wait `1s`, then `2s`, then `4s`, then cap at `8s` between operation status
reads. This keeps normal callers away from avoidable rate limits.

If you poll manually with `GET /v1/operations/{operation_id}`, use the same
schedule. Do not poll faster than once per second per operation, stop polling as
soon as the operation reaches a terminal status, and honor `Retry-After` if the
API returns `429`.

The same finite wait contract applies to sync and async public Run clients:
`runs.wait(...)` uses the first observed `deadline_at` plus the fixed 35-second
recovery allowance and one polling interval when its timeout is omitted. An
explicit finite timeout always wins. If stream consumption is unavailable,
bounded Run polling is the fallback.

## Async Examples

```python
from zenture import AsyncZenture
from zenture.idempotency import idempotency_key

async with AsyncZenture.from_env() as client:
    operation = await client.chat.create_operation(
        message="Review this response.",
        mode="single",
        idempotency_key=idempotency_key("case-123", "chat-turn-1", "v1"),
    )
    final_operation = await client.operations.wait(operation.operation_id)
```

```python
from zenture import AsyncZenture
from zenture.idempotency import idempotency_key

async with AsyncZenture.from_env() as client:
    evaluation = await client.evaluations.run(
        user_message="What does the SDK do?",
        ai_answer="It helps server-side Python integrations call zenture.",
        external_id="support-ticket-123-answer-a",
        idempotency_key=idempotency_key("support-ticket-123-answer-a", "evaluate", "v1"),
    )
```
