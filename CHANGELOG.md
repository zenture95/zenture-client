# Changelog

All notable changes to `zenture-sdk` will be documented in this file.

The project follows semantic versioning once public releases begin. Prerelease
versions may change while the Public API contract is still in release-candidate
state.

## Unreleased

- Added nullable `deadline_at` Run response fields, additive response tolerance,
  and finite sync/async Run waits with deadline-aware timeout context.
- Added an opt-in, internal MCP client peer adapter with sync/async transport
  ports, official Streamable HTTP integration, typed Run mappings, bounded safe
  errors and local cross-channel composition tests. The API-only installation
  remains unchanged; public `zenture-client` promotion is deferred.

## 1.0.0rc3 - 2026-06-28

- Clarified external vs. internal evaluation flows across SDK docs.
- Added local SDK validation for internal evaluation target payloads.
- Updated evaluation error handling docs for local `ValidationError` and API
  `ZentureValidationError`.

## 1.0.0rc2 - 2026-06-27

- Updated the README logo to use the public production-hosted zenture logo so
  PyPI renders the project description correctly.

## 1.0.0rc1 - 2026-06-27

- Added `client.wallet.get()` and `await client.wallet.get()` for the public
  `GET /v1/wallet` account projection with `wallet:read` scope.
- Removed the unpublished `client.billing.get()` mapping and `/v1/billing`
  SDK surface in favor of wallet terminology before public release.
- Added optional `chat.run(..., include_content=True)` and
  `evaluations.run(..., include_detail=True)` attachments for common
  chat-then-evaluate application flows.
- Added an end-to-end chat evaluation smoke example with wallet, model
  discovery, input wizard, chat, evaluation, and billed-amount summary output.
- Synced the Public API OpenAPI artifact with pagination parameters for chats,
  chat messages, and evaluations.
- Added `limit` and `cursor` support plus sync/async iterator helpers for chat
  summaries, chat messages, and evaluations.
- Added pagination documentation and an executable pagination example.
- Updated operation-error contract handling to match the current OpenAPI
  `PublicOperationError` schema.

## 0.1.0b1 - 2026-06-15

- Added SDK architecture and implementation plan.
- Added repository governance bootstrap documents.
- Added public sync/async clients, private httpx transport, strict contract
  models, model discovery, single-/multi-model chat, evaluation creation,
  operation reads, billing, usage, limits, idempotency, retry, timeout and
  polling primitives.
