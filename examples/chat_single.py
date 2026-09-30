"""Run a single-model chat operation."""

from __future__ import annotations

from zenture import ZentureClient
from zenture.idempotency import idempotency_key


def main() -> None:
    key = idempotency_key("example", "chat-single", "v1")
    with ZentureClient.from_env() as client:
        result = client.chat.run(
            message="Summarize this customer support update.",
            mode="single",
            idempotency_key=key,
            timeout=120.0,
        )
        print(result.operation_id, result.status)


if __name__ == "__main__":
    main()
