# Contributing

`zenture-client` provides the public `zenture` Python SDK for the zenture Public API. The current
repository contains the runtime SDK, typed contract layer, public docs,
examples, and packaging configuration used for prerelease validation.

## Development Standards

- Keep public APIs typed and exposed through `ZentureClient`, `AsyncZentureClient`, and
  resource attributes such as `client.chat`.
- Keep `_transport`, `_resources`, and `_contract` as implementation
  namespaces.
- Use Pydantic models for request, response, and configuration validation.
- Use `httpx` for sync and async HTTP transport.
- Do not log secrets, prompts, raw request bodies, or raw response bodies.
- Add deterministic tests for behavior changes.
- Keep README, docs, examples, and `AGENTS.md` aligned when public behavior
  changes.
- Maintainers may run controlled non-production checks using the private
  environment runbook. Do not commit non-production hostnames, local port
  conventions, tokens, payloads, or logs.

## Pull Requests

All changes should go through a pull request into `main`. Keep pull requests
small and reviewable: one topic per PR, no unrelated formatting churn, and no
mixed feature/security/release work.

Pull requests should include:

- a concise description of the change
- tests or a clear reason tests are not applicable
- documentation updates when behavior, public API, security posture, examples,
  or release process changes
- confirmation that no real zenture API tokens or customer data were added

Use one primary PR type:

- `type: fix` - bug fix or incorrect SDK behavior
- `type: feat` - new public SDK capability
- `type: docs` - README, examples, AGENTS, changelog, or prose docs
- `type: test` - tests only
- `type: chore` - CI, packaging, metadata, dependency upkeep
- `type: refactor` - internal change with no public behavior change
- `type: security` - security hardening or vulnerability fix

Public-surface changes require extra care. Any change to `ZentureClient`,
`AsyncZentureClient`, public resources, public errors, return types, examples, or
OpenAPI-derived contract behavior must update tests and docs in the same PR.
Breaking changes are not allowed in V1 without an explicit release decision,
migration note, and changelog entry.

Before merge:

- `Development CI` must pass.
- CODEOWNERS review must be satisfied once branch protection is available.
- Security, docs, and artifact checklist items in the PR template must be
  answered truthfully.
- Use squash merge only; the PR title becomes the commit title.
- Delete the branch after merge.

## Local Checks

Run the full local gate before handing off SDK changes:

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

Tests must not hit the real zenture API. Use `httpx.MockTransport` or static
checks for docs and examples.

## Release Process

Public PyPI releases are blocked until the SDK reviewer gate passes. Publishing
must use PyPI Trusted Publishing through GitHub Actions OIDC, not long-lived
package tokens.
