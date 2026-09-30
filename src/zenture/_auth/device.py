"""RFC 8628 device authorization: present a code, poll the token endpoint, verify tokens."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from zenture._auth.errors import AuthorizationRequired, AuthUnavailable, LoginCancelled
from zenture._auth.http import PRE_SEND_ERRORS, JsonResponse, request_json
from zenture._auth.model import CLIENT_ID, SCOPE, is_secure_origin
from zenture._auth.tokens import KeyCache, TokenRejected, TokenSet, parse_token_response

if TYPE_CHECKING:
    import threading

    from zenture._auth.discovery import Discovery

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_TIMEOUT_SECONDS = 600.0
_DEFAULT_INTERVAL = 5.0
_MAX_INTERVAL = 60.0
_SLOW_DOWN_STEP = 5.0
_USER_CODE = re.compile(r"^[A-Za-z0-9]{8}$")
_MAX_FIELD = 2048


@dataclass(frozen=True, slots=True, repr=False)
class DevicePrompt:
    """What the user must see: the plain verification URI and the code as ``XXXX-XXXX``."""

    verification_uri: str
    user_code: str

    def __repr__(self) -> str:
        return "DevicePrompt()"


Presenter = Callable[[DevicePrompt], None]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


def terminal_presenter(prompt: DevicePrompt) -> None:
    """Show the prompt on the terminal only (never through logging)."""

    print(
        f"To authorize this device, open {prompt.verification_uri}\n"
        f"and enter the code {prompt.user_code}",
        file=sys.stderr,
        flush=True,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _Authorization:
    device_code: str
    prompt: DevicePrompt
    expires_in: float
    interval: float

    def __repr__(self) -> str:
        return "_Authorization()"


def _invalid() -> AuthUnavailable:
    return AuthUnavailable("device_authorization_invalid", next_action="retry_later")


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def _start(client: httpx.Client, discovery: Discovery) -> _Authorization:
    endpoint = discovery.device_authorization_endpoint
    if endpoint is None:
        raise AuthUnavailable("device_login_unsupported", next_action="check_endpoint")
    form = {"client_id": CLIENT_ID, "scope": SCOPE, "resource": discovery.resource}
    try:
        response = request_json(client, "POST", endpoint, form=form)
    except httpx.HTTPError as exc:
        raise AuthUnavailable("device_authorization_unreachable") from exc
    body = response.body
    if response.status != 200 or body is None:
        raise _invalid()
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_uri = body.get("verification_uri")
    expires_in = _positive(body.get("expires_in"))
    interval = _positive(body.get("interval", _DEFAULT_INTERVAL))
    if not (
        isinstance(device_code, str)
        and device_code
        and len(device_code) <= _MAX_FIELD
        and isinstance(user_code, str)
        and _USER_CODE.fullmatch(user_code.replace("-", ""))
        and isinstance(verification_uri, str)
        and len(verification_uri) <= _MAX_FIELD
        and is_secure_origin(verification_uri)
        and expires_in is not None
        and interval is not None
    ):
        raise _invalid()
    code = user_code.replace("-", "").upper()
    return _Authorization(
        device_code=device_code,
        prompt=DevicePrompt(verification_uri, f"{code[:4]}-{code[4:]}"),
        expires_in=expires_in,
        interval=min(interval, _MAX_INTERVAL),
    )


def _poll_error(response: JsonResponse) -> str | None:
    if response.status == 400 and response.body is not None:
        error = response.body.get("error")
        if isinstance(error, str):
            return error
    return None


def _unknown_outcome() -> AuthorizationRequired:
    return AuthorizationRequired("device_outcome_unknown", next_action="login")


def run_device_login(
    client: httpx.Client,
    discovery: Discovery,
    keys: KeyCache,
    *,
    present: Presenter,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
    cancel: threading.Event | None = None,
) -> TokenSet:
    """Authorize through the device grant; nothing persists before token verification."""

    authorization = _start(client, discovery)
    deadline = clock() + min(authorization.expires_in, DEVICE_TIMEOUT_SECONDS)
    present(authorization.prompt)
    interval = authorization.interval
    form = {
        "grant_type": DEVICE_GRANT,
        "device_code": authorization.device_code,
        "client_id": CLIENT_ID,
        "resource": discovery.resource,
    }
    try:
        while True:
            sleep(interval)
            if cancel is not None and cancel.is_set():
                raise LoginCancelled("device_login_interrupted")
            if clock() >= deadline:
                raise AuthorizationRequired("device_code_expired", next_action="login")
            response = _poll(client, discovery, form)
            if response.status == 200:
                return _tokens(response, discovery, keys, client)
            error = _poll_error(response)
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = min(interval + _SLOW_DOWN_STEP, _MAX_INTERVAL)
            elif error == "access_denied":
                raise LoginCancelled
            elif error == "expired_token":
                raise AuthorizationRequired("device_code_expired", next_action="login")
            elif error == "invalid_grant":
                raise AuthorizationRequired("device_code_invalid", next_action="login")
            else:
                # The code may already be consumed: never poll it again.
                raise _unknown_outcome()
    except KeyboardInterrupt:
        raise LoginCancelled("device_login_interrupted") from None


def _poll(client: httpx.Client, discovery: Discovery, form: dict[str, str]) -> JsonResponse:
    try:
        return request_json(client, "POST", discovery.token_endpoint, form=form)
    except PRE_SEND_ERRORS as exc:
        raise AuthUnavailable("token_endpoint_unreachable") from exc
    except httpx.HTTPError as exc:
        raise _unknown_outcome() from exc


def _tokens(
    response: JsonResponse, discovery: Discovery, keys: KeyCache, client: httpx.Client
) -> TokenSet:
    try:
        return parse_token_response(response, discovery=discovery, keys=keys, client=client)
    except TokenRejected as exc:
        raise _unknown_outcome() from exc
