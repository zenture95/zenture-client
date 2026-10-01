"""Access JWTs that fail verification are rejected and never reach the store (scenario 4)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from auth_harness import Environment, FakeIssuer, browser_opener, runtime_for
from cryptography.hazmat.primitives.asymmetric import ec

from zenture._auth.errors import AuthUnavailable
from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import RecordKey, RecordStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _es256(issuer: FakeIssuer, claims: dict[str, Any], **headers: Any) -> str:
    return jwt.encode(
        # Inspect the synthetic issuer signing key to forge verification cases.
        claims,
        issuer._key,  # pyright: ignore[reportPrivateUsage]
        algorithm="ES256",
        headers={"kid": issuer.kid, **headers},
    )


def wrong_iss(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    return _es256(issuer, {**claims, "iss": "http://127.0.0.1:1"})


def past_exp(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    return _es256(issuer, {**claims, "exp": int(time.time()) - 3600})


def alg_none(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, "", algorithm="none", headers={"kid": issuer.kid})


def hs256_with_public_jwk(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    public_jwk = json.dumps(issuer.jwks()["keys"][0], separators=(",", ":")).encode()
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": issuer.kid}).encode())
    body = _b64(json.dumps(claims).encode())
    signature = hmac.new(public_jwk, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(signature)}"


def missing_kid(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    # Inspect the synthetic issuer signing key to forge verification cases.
    return jwt.encode(claims, issuer._key, algorithm="ES256")  # pyright: ignore[reportPrivateUsage]


def other_algorithm(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    key = ec.generate_private_key(ec.SECP384R1())
    return jwt.encode(claims, key, algorithm="ES384", headers={"kid": issuer.kid})


def unknown_kid(issuer: FakeIssuer, claims: dict[str, Any]) -> str:
    return _es256(issuer, claims, kid="rotated-away")


FORGERIES: list[Callable[[FakeIssuer, dict[str, Any]], str]] = [
    wrong_iss,
    past_exp,
    alg_none,
    hs256_with_public_jwk,
    missing_kid,
    other_algorithm,
    unknown_kid,
]


@pytest.mark.parametrize("forge", FORGERIES, ids=[f.__name__ for f in FORGERIES])
def test_forged_or_invalid_access_token_is_rejected_and_nothing_is_stored(
    env: Environment, store: RecordStore, lock_dir: Path, forge: Any
) -> None:
    env.issuer.forge_access = forge
    opener, _ = browser_opener()
    flow = LoginFlow(runtime_for(store, lock_dir, opener))

    with pytest.raises(AuthUnavailable) as caught:
        flow.login(endpoint=env.endpoint)

    assert caught.value.code == "token_response_rejected"
    assert store.load(RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID)) is None
