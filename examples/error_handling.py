"""Handle API, rate-limit, and local polling errors."""

from __future__ import annotations

from zenture import ZentureClient
from zenture.errors import ZentureAPIError, ZenturePollingTimeoutError, ZentureRateLimitError
from zenture.idempotency import idempotency_key


def main() -> None:
    key = idempotency_key("example", "error-handling", "v1")
    with ZentureClient.from_env() as client:
        try:
            result = client.chat.run(
                message="Summarize this incident note.",
                idempotency_key=key,
                timeout=120.0,
            )
            print(result.status)
        except ZenturePollingTimeoutError as exc:
            print(exc.operation_id)
        except ZentureRateLimitError as exc:
            print(exc.retry_after)
        except ZentureAPIError as exc:
            print(exc.request_id)


if __name__ == "__main__":
    main()
