"""List recent Runs over MCP with the authorization stored by `zenture auth login`.

Log in once from a terminal with `zenture auth login`. This script never starts a
login: without a stored authorization it stops and tells you what to run.
Call the synchronous client from ordinary code, not from inside a running event loop.
"""

from __future__ import annotations

import argparse

from zenture.auth import AuthorizationRequired, AuthUnavailable, SecureStoreUnavailable
from zenture.mcp import McpClient


def main() -> None:
    try:
        with McpClient.connect() as client:
            client.require_product_tools()
            runs = client.list_runs(limit=5)
            print(f"Recent Runs: {len(runs.runs)}")
    except AuthorizationRequired:
        print("No usable authorization. Run `zenture auth login` first.")
    except AuthUnavailable:
        print("Authorization could not be checked right now. Try again later.")
    except SecureStoreUnavailable:
        print("No protected credential store here. Use `zenture auth login --session-only`.")


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="List recent Runs with the stored authorization (no login is started)."
    ).parse_args()
    main()
