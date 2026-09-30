"""Run a chat operation with the async client."""

from __future__ import annotations

import asyncio

from zenture import AsyncZentureClient
from zenture.idempotency import idempotency_key


async def main() -> None:
    key = idempotency_key("example", "async-chat", "v1")
    async with AsyncZentureClient.from_env() as client:
        result = await client.chat.run(
            message="Summarize this support note.",
            mode="single",
            idempotency_key=key,
            timeout=120.0,
        )
        print(result.operation_id, result.status)


if __name__ == "__main__":
    asyncio.run(main())
