# Errors

The SDK raises typed exceptions for public API errors and local SDK failures.
API errors include safe fields such as `error_code`, `status_code`,
`request_id`, and `retry_after` when available.

Common exception types:

- `ZentureAPIError`
- `ZentureAuthenticationError`
- `ZenturePermissionError`
- `ZentureValidationError`
- `ZentureMissingIdempotencyKeyError`
- `ZentureIdempotencyConflictError`
- `ZentureInsufficientCreditsError`
- `ZentureRateLimitError`
- `ZentureCapacityError`
- `ZentureDependencyUnavailableError`
- `ZentureInternalServerError`
- `ZentureOperationExpiredError`
- `ZentureTransportError`
- `ZentureResponseError`
- `ZenturePollingTimeoutError`
- `ZenturePollingStoppedError`

## Polling Errors

`ZentureInsufficientCreditsError` means the wallet has fewer than the public API
minimum required balance for paid AI operations. Call `client.wallet.get()` to
read the current plan and available credits before retrying.

`ZenturePollingTimeoutError` and `ZenturePollingStoppedError` may include
`operation_id`, `idempotency_key`, and `last_request_id` attributes. Use them to
resume an operation or retry safely with the same idempotency key.

## Evaluation Target Errors

`client.evaluations.create(...)` and `client.evaluations.run(...)` support two
different target modes:

- External evaluation: send `user_message`, `ai_answer`, optional
  `external_id`, and optional `metadata`. Omit `chat_id`, `turn_id`, and
  `model_response_id`.
- Internal zenture chat evaluation: send `user_message`, `ai_answer`, and the
  AI-answer `model_response_id`. `chat_id` and `turn_id` are optional
  correlation fields, but if either is present, `model_response_id` is required.

The SDK catches the common internal/external mix-up locally. Passing `chat_id`
or `turn_id` without `model_response_id` raises Pydantic `ValidationError`
before any HTTP request is sent. Server-side target validation, such as a
`model_response_id` that does not belong to the authenticated API user, raises
`ZentureValidationError` with `status_code == 422` and
`error_code == "validation_failed"`.

Do not treat this case as a terminal failed operation. A failed evaluation
operation means zenture accepted the request target and execution failed later;
a malformed internal-vs-external payload is a validation error.

## Redaction

Exceptions must not expose API tokens, Authorization headers, prompts, answers,
raw request bodies, raw response bodies, JWTs, provider payloads, or billing
internals. Do not add logs that print exception internals without considering
redaction.

## Run and MCP recovery

Local `ValueError` / Pydantic `ValidationError` means the caller must correct
inputs. Inspect the documented field contract; do not print validation payloads.
REST failures use `ZentureAPIError` subclasses. MCP product/transport failures
use `ZentureMCPError` and `ZentureMCPProtocolError` from `zenture.errors`.
MCP authorization failures use the distinct exceptions in `zenture.auth`.

| MCP next_action | Caller response |
|---|---|
| `check_request` | Review the input fields and combinations. The current error may not identify the exact field; do not guess values |
| `check_run_id` | Read the known Run, or search a bounded recent owned list; clarify ambiguous matches |
| `retry_later` | Respect retry_after_seconds when present; preserve the Run ID and original key/request |
| `reauthorize` | Explain that the human must restore authorization; do not launch login as an agent |
| `reattach_file` | Reselect a missing source if the host supports file bytes; reselection cannot add a missing resolver |

`retryable=True` does not authorize a replacement chargeable Run. After an
uncertain start, recover with the saved original key and identical request.
If no key exists, use bounded owned readback; an empty page is not proof that
nothing started. Never fall back to a new key to bypass a conflict or expiry.

`ZentureMCPError.idempotency_key` retains the attempted Run key; save keys before
dispatch because cancellation can propagate without an error receipt. A receipt
proves identity, not completion or billing. `ZenturePollingTimeoutError` and
`ZenturePollingStoppedError` from REST Run waiting keep run_id in `operation_id`;
read that Run later. Neither stops server execution. The sample MCP polling
workflow follows the same rule.

## Validation field details

`ZentureAPIError` and `ZentureMCPError` expose an immutable `issues` tuple.
Each `zenture.validation_issues.ValidationIssue` contains a public JSON Pointer
`path`, a stable `category`, and optional `ValidationConstraints` with static
schema facts such as a length limit or the allowed enum values. Unknown field
names are coarsened to a known parent; submitted values and validator context
are excluded. Ordinary exception `str` and `repr` do not append these details.

For example, an MCP validation error may provide `/profile` with category
`invalid_enum` and allowed values `fast`, `standard`, and `detailed`. Use that
information to correct the original request, preserving its recovery key.
The tuple is empty when the producer supplies no details or when optional
details fail structural or public-schema checks. The existing error code,
status and retry policy remain the primary failure contract.

The sync and async REST SDK automatically send `X-Zenture-Error-Details: issues`
for Run and artifact requests, including event streams. Direct HTTP callers can
send the same header to request safe details. Gateway includes `error.issues`
only for the recognized value when safe details exist; missing or unknown values
retain the coarse error body for older readers. Older servers can omit details.
Auth and unrelated endpoints do not receive this header. Existing generic
transport header overrides remain available, including a deliberate override of
this header; ordinary caller headers preserve automatic negotiation.
Local Run request models retain Pydantic `ValidationError`, with input values,
unknown field names and validator context removed from exposed failures.
