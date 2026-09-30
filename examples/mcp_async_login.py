"""Log in explicitly through the browser, then list recent Runs over MCP (asynchronous).

Run it from a terminal you can interact with: ``python examples/mcp_async_login.py``.
Nothing runs on import, and no credential appears in this file or its output.
"""

from __future__ import annotations

import argparse
import asyncio

from zenture.auth import AuthorizationRequired, LoginCancelled, login_async
from zenture.mcp import AsyncMcpClient


async def main() -> None:
    try:
        with await login_async() as session:
            async with AsyncMcpClient.connect(session=session) as client:
                await client.require_product_tools()
                runs = await client.list_runs(limit=5)
                print(f"Recent Runs: {len(runs.runs)}")
    except LoginCancelled:
        print("Login was cancelled; nothing was connected.")
    except AuthorizationRequired:
        print("Authorization is required. Run `zenture auth login` and try again.")


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Browser login, then list recent Runs with the asynchronous MCP client."
    ).parse_args()
    asyncio.run(main())
