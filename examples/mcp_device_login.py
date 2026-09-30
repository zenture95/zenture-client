"""Log in on a headless machine with a code you enter on another device.

`login(device=True)` prints a verification address and a one-time code, then waits
at most ten minutes for approval. The authorization stays in memory only
(`session_only=True`) and is gone when this process ends.
"""

from __future__ import annotations

import argparse

from zenture.auth import AuthorizationRequired, LoginCancelled, login
from zenture.mcp import McpClient


def main() -> None:
    try:
        with (
            login(device=True, session_only=True) as session,
            McpClient.connect(session=session) as client,
        ):
            client.require_product_tools()
            print(f"Recent Runs: {len(client.list_runs(limit=5).runs)}")
    except LoginCancelled:
        print("The request was declined or cancelled; nothing was connected.")
    except AuthorizationRequired:
        print("The code expired or could not be confirmed. Run this script again.")


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Device login (code on another device), then list recent Runs."
    ).parse_args()
    main()
