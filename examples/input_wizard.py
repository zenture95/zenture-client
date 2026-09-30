"""Run the input wizard helper."""

from __future__ import annotations

from zenture import ZentureClient
from zenture.idempotency import idempotency_key


def main() -> None:
    key = idempotency_key("example", "input-wizard", "v1")
    with ZentureClient.from_env() as client:
        result = client.input_wizard.run(
            prompt="Improve this prompt for an internal support assistant.",
            idempotency_key=key,
            timeout=120.0,
        )
        print(result.operation_id, result.status)
        if result.result and result.result.optimized_prompt:
            print(result.result.optimized_prompt)


if __name__ == "__main__":
    main()
