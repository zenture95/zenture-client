"""Build a stable idempotency key for a mutating operation."""

from __future__ import annotations

from zenture import ZentureClient
from zenture.idempotency import idempotency_key


def main() -> None:
    key = idempotency_key("example-case", "chat", "v1")
    with ZentureClient.from_env() as client:
        result = client.chat.run(
            message="Create a concise status summary.",
            idempotency_key=key,
            timeout=120.0,
        )
        print(result.idempotency_key)


if __name__ == "__main__":
    main()
