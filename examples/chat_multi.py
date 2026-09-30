"""Run a multi-model chat comparison."""

from __future__ import annotations

from zenture import ZentureClient
from zenture.idempotency import idempotency_key


def main() -> None:
    key = idempotency_key("example", "chat-multi", "v1")
    with ZentureClient.from_env() as client:
        available_models = [
            model.id for model in client.models.list(mode="multi").models if model.is_available
        ]
        if len(available_models) < 2:
            raise RuntimeError("At least two available multi-mode models are required.")

        result = client.chat.run(
            message="Compare these draft answers for factual consistency.",
            mode="multi",
            models=available_models[:2],
            idempotency_key=key,
            timeout=120.0,
        )
        print(result.operation_id, result.status)


if __name__ == "__main__":
    main()
