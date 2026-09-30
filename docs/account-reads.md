# Account Reads

The SDK exposes read-only helpers for account-adjacent status:

- `client.wallet.get()`
- `client.usage.get(scope="api")`
- `client.usage.get(scope="all")`
- `client.limits.get()`

These routes do not create chat, input-wizard, or evaluation work. They are
useful for dashboards, health checks, and preflight decisions before starting a
billable operation.

## Wallet

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    wallet = client.wallet.get()
    print(wallet.plan, wallet.status, wallet.credits_available.amount)
```

`wallet.plan`, `wallet.status`, and `wallet.credits_available` are safe public
projections. The SDK does not expose internal billing ledgers, Stripe
identifiers, price internals, or invoice payloads.

## Usage

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    api_usage = client.usage.get(scope="api")
    all_usage = client.usage.get(scope="all")
    print(api_usage.operation_count, all_usage.operation_count)
```

`scope="api"` returns usage for API-token-visible activity. `scope="all"`
returns all usage counters visible to the token. The response is intentionally
small and should not be treated as a detailed audit log.

## Limits

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    limits = client.limits.get()
    print(limits.operation_statuses)
    print(limits.routes["POST /v1/evaluate"].idempotency_required)
```

Use route limits to discover public rate-limit, body-size, auth, scope, and
idempotency requirements. Do not hard-code assumptions when the API provides
the value.

## Async

Async clients expose the same resources:

```python
from zenture import AsyncZentureClient

async with AsyncZentureClient.from_env() as client:
    wallet = await client.wallet.get()
    usage = await client.usage.get(scope="api")
    limits = await client.limits.get()
```
