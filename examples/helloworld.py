"""Minimal connectivity check for the zenture SDK."""

from __future__ import annotations

from zenture import ZentureClient


def main() -> None:
    with ZentureClient.from_env() as client:
        print(client.helloworld())


if __name__ == "__main__":
    main()
