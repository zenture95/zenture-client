"""Create an operation and poll it explicitly."""

from __future__ import annotations

from zenture import ZentureClient
from zenture.idempotency import idempotency_key


def main() -> None:
    key = idempotency_key("example", "operation-polling", "v1")
    with ZentureClient.from_env() as client:
        operation = client.chat.create_operation(
            message="Review this response for clarity.",
            mode="single",
            idempotency_key=key,
        )
        final_operation = client.operations.wait(operation.operation_id, timeout=120.0)
        print(final_operation.operation_id, final_operation.status)


if __name__ == "__main__":
    main()
