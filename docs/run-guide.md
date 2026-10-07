# Run guide for API and MCP clients

Use a Run to evaluate one explicitly selected text answer against its
original task and stated requirements. Keep the selected content intact,
including relevant citations. Ask for the selection or criteria when ambiguous;
do not invent requirements or select an answer from conversation history.

Current execution supports inline text. The request schema also defines
`zenture_ref`, but Run preparation currently rejects all registered-file inputs.
File registration is available; it does not enable file evaluation.

## Choose the interface

| Need | Public interface | Credential | Result |
|---|---|---|---|
| Evaluate selected content with decisions, findings and coverage | `ZentureClient.runs` / `AsyncZentureClient.runs` | REST API token | `PublicRunResponse` |
| Same Run workflow through hosted MCP | `McpClient` / `AsyncMcpClient` from `zenture.mcp` | Stored account OAuth | Run methods below |
| Generate a chat answer | REST `client.chat` | REST API token | Async Operation |
| Use the evaluation-specific API, including chat-response correlation | REST `client.evaluations` | REST API token | Async Operation plus evaluation detail |
| Improve a prompt | REST `client.input_wizard` | REST API token | Async Operation |

Runs and Async Operations have different IDs and lifecycle semantics. Do not
pass a `run_...` ID to `client.operations.wait()`. `client.runs.run()` prepares
and starts, but does **not** poll to terminal status; `chat.run()` and
`evaluations.run()` do poll. No equivalent automatic wait helper exists on the
MCP peer. Both sync and async clients expose matching operations; use `await`
for async calls and `async for` for the REST event stream.

For REST setup see [README](../README.md#authentication); for stored MCP login
see [authentication](authentication.md). An agent uses existing authorization
and asks the human to run `zenture auth login` if needed; it does not start login
on the user's behalf. Never print payloads, credentials or recovery keys.

## The selected evaluation input

| Field | Purpose and source | Rules |
|---|---|---|
| `task` | Original task and explicit evaluation criteria from the user | Required, non-blank, at most 20,000 Unicode code points |
| `artifact.type` | How the selected content is provided | Use `text` for current execution; `zenture_ref` exists in the schema but is rejected during preparation |
| `artifact.value` for `text` | The actual selected answer, preserving relevant citations/formatting | Required, 1-50,000 code points. Values beginning with `http://`, `https://`, `data:` or `file:` are rejected case-insensitively; a URL is not the answer |
| `artifact.value` for `zenture_ref` | Reference form retained in the schema; currently not evaluable | `art_` followed by 3-128 ASCII letters, digits, `_` or `-`; registration does not make it a supported Run input |
| `profile` | Requested evaluation profile | `standard` by default; `fast` or `detailed` only with explicit user intent |
| `idempotency_key` | Caller-created identity of the intended operation | Save before dispatch; preserve the exact request and key for recovery. Rules differ by channel below |
| `predecessor_run_id` | Optional earlier Run of the same account that the new Run follows up | REST `create`/`run` and raw MCP `run` only; omit or `null` for none. Copy an owned `run_...` ID and preserve it on recovery |

Profiles select service-defined evaluation plans and budgets. Their names do
not promise a fixed duration, price or particular check. Use actual coverage,
limitations and billing projections; the service may reject unsupported
artifact/profile combinations. Registered-file evaluation is currently unavailable.
For an inspectable proposal before starting,
REST exposes `prepare()`; MCP's `run` combines preparation and start.

Minimal raw MCP `run` arguments (valid, but no recoverable caller key):

```json
{"task":"Check that the answer names exactly two colours.","artifact":{"type":"text","value":"Blue and green."}}
```

Recommended recoverable arguments:

```json
{
  "task": "Check that the answer names exactly two colours.",
  "artifact": {"type": "text", "value": "Blue and green."},
  "profile": "standard",
  "idempotency_key": "review_case_123"
}
```

The synthetic key above illustrates a persisted identity. Allocate a different
key for every deliberate new evaluation; recover the same evaluation with the
same key. Do not reuse an example constant across unrelated user requests.

## REST method reference

All mutations require a caller-owned `idempotency_key`. The Run convenience
methods append `:prepare` / `:create`; allow room for the longer suffix inside
the REST 255-character key limit. Do not append these suffixes yourself.
Use separate keys for upload issuance, byte upload, cancellation and feedback.
See [REST idempotency](idempotency.md) for automatic transport retry rules.

| Method | Inputs and behavior | Returns |
|---|---|---|
| `runs.prepare(task=..., artifact=..., idempotency_key=..., profile="standard")` | Prepare only; inspect admissibility, expiry, planned/unavailable checks and estimated/maximum Credits | Proposal with `proposal_id`, `proposal_hash`, `expires_at`, `start_admissible`, `billing_projection` |
| `runs.create(proposal_id=..., proposal_hash=..., idempotency_key=..., wait=None, predecessor_run_id=None)` | Copy ID and hash together from prepare; may consume Credits/Trial slot. Integer `wait` is clamped to 0-15 seconds. Optional predecessor is sent only at start | Current `PublicRunResponse`, possibly ongoing |
| `runs.run(task=..., artifact=..., idempotency_key=..., profile="standard", wait=None, predecessor_run_id=None)` | Prepare then start; not a polling helper. Forwards optional predecessor only to create | Current `PublicRunResponse` |
| `runs.list(...)` | One compact owned page; filters below | `runs`, `has_more`, `next_cursor` |
| `runs.get(run_id, view="summary")` | `summary` for progress; `full` requests result content | `PublicRunResponse` directly |
| `runs.wait(run_id, timeout=None, initial_interval=1.0, max_interval=8.0, stop=None)` | Finite polling; explicit timeout is seconds. Default starts at 120 seconds, then uses the first supplied Run deadline plus recovery allowance. `stop` stops local waiting | Terminal summary, including failure states |
| `runs.list_events(run_id, cursor=None, limit=50)` | One replay page, limit 1-50 | `events`, `has_more`, `next_cursor` |
| `runs.iter_events(run_id, last_event_id=None, timeout=None, initial_interval=1.0, max_interval=8.0, stop=None)` | SSE with reconnection; `last_event_id` takes a saved **event_cursor**. Set timeout for bounded listening. `stop` is a synchronous predicate | Typed events, heartbeats and stream-error variants |
| `runs.cancel(run_id, idempotency_key=...)` | Explicit user stop request only | Current Run; cancellation can be pending |
| `runs.record_outcome(run_id, outcome=..., idempotency_key=..., finding_adjudications=None, edited_artifact_ref=None)` | Explicit feedback; fields below | Updated Run, original decision preserved |
| `runs.signed_upload(file_name=..., mime_type=..., byte_size=..., content_hash=..., idempotency_key=...)` | Authorize matching file metadata | `upload_id`, `expires_at`, optional `upload_url` |
| `runs.attach_artifact(upload_id=..., file_name=..., mime_type=..., content=..., content_hash=..., idempotency_key=..., byte_size=None)` | Send actual bytes before upload expiry; not a model-facing MCP call | Registered `artifact_ref` and file metadata |

Use the returned `run_id` after starting. A local wait timeout or stop does not
cancel the server Run: retain that ID and read it later. `wait()` returns a
summary; fetch `view="full"` to inspect available result content.

For follow-ups, pass `predecessor_run_id` to REST `runs.create()` or `runs.run()`.
It is not a Prepare field. An unavailable predecessor returns
`predecessor_run_invalid` (422); changing it while reusing the same start key
returns `idempotency_conflict` (409). Recovery must preserve the predecessor,
including its absence. A deliberate new evaluation needs a new saved key.

## MCP method reference

Call `require_product_tools()` to check the five core tool names. It checks
neither permission nor Credits. `list_tools()` returns names only, not tool
schemas/descriptions. `attach_artifact` is optional; check membership first.

The raw MCP `run` tool also accepts `predecessor_run_id` with the same follow-up
and recovery semantics as REST. The Python `McpClient.run()` / `AsyncMcpClient.run()`
wrappers do not expose that parameter yet; use REST when a Python caller needs
to link a predecessor. Do not pass it as an unsupported wrapper argument.

| Method | Inputs and behavior | Python return |
|---|---|---|
| `run(task=..., artifact=..., profile="standard", idempotency_key=...)` | Prepare and start. Save key before dispatch; never automatically replace an uncertain attempt | Run response with `.run_id`, `.status`, and `.idempotency_key` (key excluded from model serialization) |
| `attach_artifact(file_name=..., mime_type=..., byte_size=..., content_hash=...)` | Metadata only; host resolver must supply selected bytes. No local Python path or bytes parameter | Registered `.artifact_ref`; does not enable file evaluation |
| `list_runs(...)` | Same filters below; timestamps are ISO-8601 **strings** | `.runs`, `.has_more`, `.next_cursor` |
| `get_run(run_id, view="summary", replay_cursor=None, replay_limit=50, include_event_replay=False)` | `full` for available content; replay requires explicit `True`, even with a cursor | Wrapper with **`.run`** and optional **`.event_replay`** |
| `replay_events(run_id, cursor=None, limit=50)` | Convenience method for one replay page, not another advertised tool | Events response |
| `cancel_run(run_id)` | Explicit user stop; no caller key parameter | Run response directly |
| `record_run_outcome(run_id, outcome=..., finding_adjudications=None, edited_artifact_ref=None)` | Explicit feedback; no caller key parameter | Run response directly |

MCP Run keys are case-sensitive, 1-128 ASCII letters, digits, `_` or `-`.
Omission in the Python peer generates a random key; omission in a raw MCP call
creates a fresh server identity and returns no key receipt. Explicit `null` /
Python `None` is rejected for this key. Save your own key **before** calling if
recovery after interruption matters. See [MCP recovery](mcp-client.md#run-idempotency).

## Listing and event replay payloads

| Field | Meaning | Default / bounds |
|---|---|---|
| `status` | Run categories: `active`, `completed`, `failed`, `cancelled`; `succeeded` is a deprecated alias for `completed` | Omit / `None` / `[]`: any status |
| `decision` | `ready`, `revise`, `human_review`, `insufficient_evidence` | Omit / `None` / `[]`: any decision |
| `profile` | Filter by profile actually used, without changing it | Omit / `None` / `[]`: any profile |
| `created_after`, `created_before` | Creation window; after must not exceed before | Open when omitted; timezone-aware Python `datetime` for REST, ISO-8601 string for MCP |
| `limit` | Number of Runs per page | 5; 1-50 |
| `cursor` | Copy the preceding `next_cursor` unchanged; preserve filters | Omit / `None` for first page; at most 512 characters |
| `replay_cursor` | Copy `event_replay.next_cursor` to get the next event page | Omit / `None` for first page; at most 512 characters |
| `replay_limit` | Event count, not Run count | 50; 1-50 |
| `include_event_replay` | Enable replay on MCP `get_run` | `false`; a cursor alone does not enable it |

List/replay methods return one page. Request the next only when needed. An
empty page is not proof that a lost start never happened. Clarify ambiguous
matches rather than creating replacement work. Event replay describes progress;
get the full Run projection for the authoritative available evaluation result.

## Artifact metadata and feedback payloads

File metadata must describe actual selected bytes: `file_name` is 1-255
characters, `mime_type` is the real media type (1-127 characters), `byte_size`
is 1-10,485,760 bytes, and `content_hash` is the complete SHA-256 digest.
MCP accepts uppercase/whitespace around the hash and normalizes it; REST
expects 64 lowercase hexadecimal characters. Neither a name nor a URL supplies
bytes. The service validates file support and content, not just the extension.

REST `content` accepts nonempty `bytes`, a binary file, or an iterable of byte
chunks (an **async iterable** with the async client). For streams, provide the
exact `byte_size`; for bytes, it can be inferred. Byte count/hash are checked;
streams are not automatically replayable. After success, check `artifact_ref`
is present. MCP callers instead rely on the host's selected-file resolver;
reselection cannot add a missing host capability.

These methods register files; they do not enable their evaluation. Run preparation
currently rejects `zenture_ref`: textual references return `artifact_unavailable`,
and other media types return `artifact_type_unsupported`. Do not promise a
registered-file evaluation or keep retrying an unavailable evaluation path.

| Feedback field | Meaning / rules |
|---|---|
| `outcome` | Required: `used` as-is, `edited` after changes, `rejected` not used, `escalated` for further review, `not_sure` undecided |
| `finding_adjudications` | Optional list, at most 20; `None` / `[]` skips finding-level feedback |
| `finding_adjudications[].finding_ref` | Copy a 1-128 character reference from this Run's full result; never invent it |
| `finding_adjudications[].outcome` | `confirmed`, `rejected`, `partially_valid`, `not_sure` |
| `edited_artifact_ref` | Registered owned `art_...` reference, required exactly for `edited`; omit / `None` for other outcomes |

Feedback records user action; it does not replace the original acceptance
decision or evaluate the edited artifact. To review an edited answer, the user
must intend a new evaluation. Do not infer feedback from silence or prose.

## Interpret the returned Run

JSON objects become typed Python models; arrays generally become tuples.
REST `runs.get()` returns the model directly. MCP `get_run()` wraps it in `.run`.
Raw MCP structured content is the Run object itself, with optional event_replay;
it does **not** have the Python wrapper's extra `run` level.

| Field / group | Interpretation and next action |
|---|---|
| `run_id`, `generation`, `family`, `work_type`, `profile` | Identity and service-reported evaluation classification; keep `run_id` for later calls |
| `status` | Nonterminal: `created`, `queued`, `running`, `waiting_for_dependency`, `partially_complete`, `cancel_requested`. Terminal: `completed`, `failed`, `cancelled`, `expired`, `budget_exhausted`; historical `succeeded` means technical success |
| `acceptance_decision` | `ready`: accepted against evaluated requirements; `revise`: changes needed; `human_review`: human judgment needed; `insufficient_evidence`: evidence cannot support acceptance. Absent is not `ready` |
| `created_at`, `updated_at`, `started_at`, `completed_at`, `deadline_at` | Timezone-aware timestamps; optional values may be absent. A deadline or estimate is not a promise of completion |
| `queue` | Reason, jobs ahead and optional start/completion estimates with their timestamp; report uncertainty |
| `task_contract_summary`, `capability_coverage` | Service-reported task/coverage metadata; do not invent missing checks or assume all requested checks ran |
| `safe_result_content` | Inspect `status`: `available` exposes `content`; `unavailable` exposes a reason. Missing projection is not an empty positive result |
| `safe_result_content.content` | `summary`, `findings`, `evidence_summaries`, `limitations`, `decision`, `coverage` and references; preserve qualifying limitations when explaining the decision |
| `reason_code`, `next_action` | Service explanation and suggested next step; not permission to bypass user intent or current authority |
| `artifact_refs`, `run_insight_ref`, result references | Opaque identifiers to preserve; do not synthesize URLs or decode them |
| `cancellation_requested` | Requested cancellation; the actual `status` remains authoritative |
| `event_cursor` | Opaque event checkpoint, usable for supported replay/stream resumption; distinct from `event_id` and the Run-list cursor |
| `billing_projection` | `pending`, `settled`, `released` or `unavailable`. `final_credits.status="available"` carries decimal-string `amount` and `unit="credits"`; unavailable/null is not zero |
| `billing_summary`, `usage_summary` | Deprecated compatibility fields; prefer billing_projection |

Report technical status, acceptance decision, relevant findings and limitations
separately. `ready` is scoped to the evaluated requirements and evidence, not a
universal correctness guarantee. Never interpret a receipt, elapsed time or
cancellation as proof of completion or zero charge.

`safe_result_content.reason_code="result_content_expired"` means the retained
content has expired. The Run remains readable through REST and MCP; do not treat
this as a dependency outage or retry expecting the expired content to return.

## Read the result without overstating it

```python
# client is an already connected REST client; saved_run_id is from the start.
result = client.runs.get(saved_run_id, view="full")
technical_status = result.status
acceptance_decision = result.acceptance_decision
projection = result.safe_result_content
if projection is not None and projection.status == "available":
    summary = projection.content.summary
    findings = projection.content.findings
    limitations = projection.content.limitations
else:
    summary = None  # No evaluation summary is available; do not fabricate one.
    findings = ()
    limitations = ()
```

For MCP, change only the first line to
`result = client.get_run(saved_run_id, view="full").run`. Empty local collections
in this example do not establish that the evaluation found no problems: report
that result content is unavailable, together with the actual status/decision.
Do not print or log result content by default; present only what the user needs.

## Complete examples and recovery

The importable [REST Run example](../examples/run_review.py) and
[MCP Run example](../examples/mcp_run_review.py) accept an already-saved key,
selected task and selected text. They start once, wait within a caller budget,
then read full content. Importing them never logs in or calls the service.
A local timeout retains the original run_id; it does not start or cancel work.

For MCP, `ZentureMCPError` exposes safe `code`, `status_code`, `retryable`,
`next_action`, `retry_after_seconds`, optional `request_id` and attempted
`idempotency_key`. See [errors](errors.md#run-and-mcp-recovery) before retrying.
An authentication failure may require human login; a 403 does not become allowed
by logging in again. Do not print exception internals, selected content or keys.
