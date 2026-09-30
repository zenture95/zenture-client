"""Run an external evaluation with the async client."""

from __future__ import annotations

import asyncio

from zenture import AsyncZentureClient
from zenture.idempotency import idempotency_key


async def main() -> None:
    async with AsyncZentureClient.from_env() as client:
        result = await client.evaluations.run(
            user_message="What does the SDK do?",
            ai_answer="It helps server-side Python integrations call zenture.",
            external_id="example-async-external-answer-v1",
            metadata={"source": "example", "kind": "external_answer"},
            idempotency_key=idempotency_key("example", "async-evaluation", "v1"),
            timeout=120.0,
        )
        print(result.operation_id, result.status)


if __name__ == "__main__":
    asyncio.run(main())
