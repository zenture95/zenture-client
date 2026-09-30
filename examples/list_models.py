"""List public models available to the current API token as JSON."""

from __future__ import annotations

import json
import sys
from typing import Any

from zenture import ZentureClient


def build_models_payload(client: Any) -> dict[str, list[dict[str, Any]]]:
    """Build a mode-keyed, JSON-serializable model discovery payload."""

    payload: dict[str, list[dict[str, Any]]] = {}
    for mode in ("single", "multi"):
        models = client.models.list(mode=mode)
        payload[mode] = [
            model.model_dump(mode="json", exclude_none=True) for model in models.models
        ]
    return payload


def main() -> None:
    with ZentureClient.from_env() as client:
        sys.stdout.write(f"{json.dumps(build_models_payload(client), indent=2, sort_keys=True)}\n")


if __name__ == "__main__":
    main()
