# Authentication

`zenture` is server-side only. zenture API tokens are credentials for
trusted backend environments such as services, workers, automation jobs, CI, and
controlled notebooks.

Do not embed API tokens in browsers, mobile apps, frontend bundles, public
notebooks, logs, traces, analytics, screenshots, support tickets, or
customer-visible errors.

## Token Creation

Create API tokens in the existing zenture webapp with an existing account:
`https://ai.zenture.app/profile?tab=api-tokens`.

The SDK intentionally does not expose an API-token management surface for
creating, rotating, revoking, or auditing tokens.

## Environment Setup

Store tokens outside Python source code:

```bash
# .env, not committed
ZENTURE_API_KEY=your_webapp_created_api_token
```

```bash
set -a
. ./.env
set +a
```

Use `ZentureClient.from_env()` or `AsyncZentureClient.from_env()` so application code reads
the token from `ZENTURE_API_KEY`.

## Base URL

The production origin is `https://api.zenture.app`.

Public SDK usage targets the production API. Never derive constructor
`base_url` values from user input. URL credentials, paths, query strings,
fragments, plain HTTP origins, and arbitrary HTTPS origins are rejected by SDK
configuration validation.
