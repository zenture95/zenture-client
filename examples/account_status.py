"""Read wallet, usage, and public route limits."""

from __future__ import annotations

from zenture import ZentureClient


def main() -> None:
    with ZentureClient.from_env() as client:
        wallet = client.wallet.get()
        api_usage = client.usage.get(scope="api")
        limits = client.limits.get()

        print(wallet.plan, wallet.status, wallet.credits_available.amount)
        print(api_usage.operation_count)
        print(limits.operation_statuses)


if __name__ == "__main__":
    main()
