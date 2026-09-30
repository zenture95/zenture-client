"""Scenario 2: discovery contract checks happen before any browser opens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.errors import AuthUnavailable
from zenture._auth.login import LoginFlow

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from zenture._auth.store import RecordStore

BREAKS: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("prm_resource_differs", {"resource": "http://127.0.0.1:1"}, {}),
    ("prm_resource_trailing_slash", {"resource": "__RESOURCE__/"}, {}),
    ("prm_without_servers", {"authorization_servers": []}, {}),
    ("as_issuer_differs", {}, {"issuer": "http://127.0.0.1:1"}),
    ("no_s256", {}, {"code_challenge_methods_supported": ["plain"]}),
    ("iss_flag_false", {}, {"authorization_response_iss_parameter_supported": False}),
    ("iss_flag_missing", {}, {"authorization_response_iss_parameter_supported": None}),
    ("foreign_token_endpoint", {}, {"token_endpoint": "http://127.0.0.1:1/token"}),
]


def _record_open(opened: list[str]) -> Callable[[str], object]:
    def opener(url: str) -> object:
        opened.append(url)
        return True

    return opener


@pytest.mark.parametrize(("name", "prm", "metadata"), BREAKS, ids=[b[0] for b in BREAKS])
def test_contract_mismatch_fails_before_browser_opens(
    env: Environment,
    store: RecordStore,
    lock_dir: Path,
    name: str,
    prm: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    env.issuer.prm_overrides = {
        k: (v.replace("__RESOURCE__", env.mcp.resource) if isinstance(v, str) else v)
        for k, v in prm.items()
    }
    env.issuer.metadata_overrides = metadata
    opened: list[str] = []
    flow = LoginFlow(runtime_for(store, lock_dir, _record_open(opened)))

    with pytest.raises(AuthUnavailable) as caught:
        flow.login(endpoint=env.endpoint)

    assert caught.value.code == "discovery_contract_violation"
    assert opened == []
    assert env.issuer.auth_requests == []


def test_first_authorization_server_is_the_issuer(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    env.issuer.prm_overrides = {"authorization_servers": [env.issuer.issuer, "http://127.0.0.1:1"]}
    opener, opened = browser_opener()
    session = LoginFlow(runtime_for(store, lock_dir, opener)).login(endpoint=env.endpoint)

    assert len(opened) == 1
    assert opened[0].startswith(f"{env.issuer.issuer}/auth?")
    assert session.binding.sub == "user-1"
