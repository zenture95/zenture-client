# Security

`zenture` is server-side only. API tokens are secrets and must never be
embedded in browser code, mobile apps, frontend bundles, public notebooks,
logs, traces, analytics, screenshots, or support messages.

## Token Handling

- Load tokens from `ZENTURE_API_KEY` via `ZentureClient.from_env()` or
  `AsyncZentureClient.from_env()`.
- Do not hardcode token values in Python files.
- Do not log Authorization headers.
- Do not include prompts or model answers in error messages.

## Public Surface

The SDK exposes public clients and resource helpers. It does not expose an
API-token management surface. Agentic chat mode is not part of Public V1, and
this SDK must not document or add public helpers for it. Internal `_contract`
models are implementation details and must not be exported from the top-level
package.

## Base URL Restrictions

The default production origin is `https://api.zenture.app`. Public SDK
documentation must not publish non-production API hostnames, deployment names,
or local port conventions. Never derive constructor `base_url` values from user
input.

## Disclosure

Report vulnerabilities through the process described in `SECURITY.md`. Do not
include live credentials, customer data, or raw provider payloads in reports.
