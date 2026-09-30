# Security Policy

## Supported Versions

`zenture` is prerelease software. Security reports are welcome for SDK
runtime behavior, documentation, examples, packaging, CI, and release workflows.

## Reporting a Vulnerability

Open a GitHub issue in the `zenture` repository for security hardening
requests, dependency concerns, release-process issues, documentation gaps,
redaction problems, or suspected SDK vulnerabilities that do not include active
secrets, exploit payloads, private customer data, or instructions for abusing
zenture systems.

Use a clear title such as `Security: <short description>` and include:

- affected file, workflow, package, or behavior
- expected security property
- observed risk
- reproduction steps using fake tokens and synthetic data only
- suggested severity if known

Do not post real zenture API tokens, JWTs, customer data, private prompts,
provider payloads, production URLs with sensitive query strings, or working
exploit instructions in GitHub issues.

Once GitHub Security Advisories are enabled, use a private GitHub Security Advisory
for high-impact vulnerabilities or anything that requires coordinated
disclosure before public discussion.

## Secret Handling Expectations

zenture API tokens are server-side credentials. They must not be embedded in
browsers, mobile apps, frontend bundles, public repositories, notebooks with
shared output, logs, analytics, traces, or customer-visible error reports.

The SDK must not log or expose:

- API tokens or Authorization headers
- JWTs
- prompts or model answers
- raw request or response bodies
- payment, billing, or provider payloads

Examples and tests must use fake credentials only.

## Token Environment Binding

zenture API tokens are environment-bound. Public SDK users should use tokens
only with the production zenture API.

The SDK performs client-side validation to prevent accidental cross-environment
token use. The API must enforce the same rule server-side. Public SDK
documentation must not publish non-production API hostnames, deployment names,
local port conventions, or token validation internals.

## Release Security

Public package publishing must use PyPI Trusted Publishing through GitHub
Actions OIDC. Long-lived PyPI API tokens are not allowed in repository secrets.

Before any public PyPI release, the repository must have:

- branch protection or rulesets on the default branch
- required CI checks
- CODEOWNERS review for protected paths
- secret scanning and push protection
- dependency update automation
- protected `pypi` release environment with manual approval
