"""``zenture`` command line: explicit login, verification and local removal of the authorization.

Importing this module performs no I/O; nothing runs before ``main`` is called.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import IO, TYPE_CHECKING

from zenture._auth import login as login_module
from zenture._auth.errors import (
    AuthError,
    AuthorizationRequired,
    AuthUnavailable,
    LoginCancelled,
    PermissionDenied,
    SecureStoreUnavailable,
)
from zenture._auth.login import LoginFlow, Runtime
from zenture._auth.stored import StatusReport, check_status, clear_stored
from zenture.errors import ZentureMCPError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from zenture._auth.device import DevicePrompt

EXIT_OK = 0
EXIT_NOT_LOGGED_IN = 1
EXIT_USAGE = 2
EXIT_AUTHORIZATION_REQUIRED = 3
EXIT_UNAVAILABLE = 4
EXIT_STORE_UNAVAILABLE = 5
EXIT_CANCELLED = 6
EXIT_PERMISSION_DENIED = 7
EXIT_INTERRUPTED = 130

_EXIT_CODES = """\
exit codes:
  0    success (status: connected)
  1    status: not_logged_in
  2    invalid command line
  3    authorization_required: log in again
  4    unavailable: nothing changed, try again later
  5    store_unavailable: no protected credential store (try --session-only)
  6    login cancelled (denied or declined)
  7    permission denied by the server
  130  interrupted (Ctrl+C, also while waiting for a device login)
"""

_REMOTE_NOTE = (
    "Access that was already granted stays valid until you revoke the connection under "
    "zenture -> Zugriff & Sicherheit -> Verbindungen."
)

_EXPLANATIONS = {
    "browser_unavailable": (
        "No browser could be opened. Run `zenture auth login --device` to sign in with a "
        "code on another device."
    ),
    "loopback_unavailable": (
        "No local address is available for the browser sign-in. Run "
        "`zenture auth login --device` to sign in with a code on another device."
    ),
    "login_timed_out": "The browser sign-in was not completed in time. Run the command again.",
    "device_login_interrupted": (
        "Device login stopped. The pending request simply expires; nothing was connected."
    ),
    "device_code_expired": "The code expired. Run `zenture auth login --device` to start again.",
    "device_code_invalid": "The code is no longer valid. Run `zenture auth login --device` again.",
    "device_outcome_unknown": (
        "The result of the device login could not be confirmed. Check your connections under "
        "zenture -> Zugriff & Sicherheit -> Verbindungen, then run "
        "`zenture auth login --device` again."
    ),
    "mcp_unavailable": (
        "The zenture MCP endpoint could not be reached. Your stored authorization is "
        "unchanged; try `zenture auth login` again later."
    ),
    "device_login_unsupported": "This server does not offer device login.",
    "login_cancelled": "The authorization was declined or cancelled; nothing was connected.",
    "secure_store_unavailable": (
        "No protected credential store is available on this system. Use "
        "`zenture auth login --session-only` to sign in for this process only."
    ),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zenture",
        description="Sign in to the hosted zenture MCP endpoint and inspect the local authorization.",
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    groups = parser.add_subparsers(dest="group", required=True, metavar="auth")
    auth = groups.add_parser(
        "auth",
        help="manage the local authorization",
        description="Manage the local authorization.",
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = auth.add_subparsers(dest="command", required=True, metavar="login|status|clear")
    login = commands.add_parser(
        "login",
        help="authorize this machine",
        description=(
            "Authorize through the system browser. A stored authorization is reused after one "
            "refresh and a non-activity probe; a new authorization starts only when it is no "
            "longer usable."
        ),
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument(
        "--device",
        action="store_true",
        help="headless login: show a code to enter on another device instead of opening a browser",
    )
    login.add_argument(
        "--session-only",
        action="store_true",
        help=("verify the login but store nothing; the authorization cannot outlive this command"),
    )
    commands.add_parser(
        "status",
        help="verify the stored authorization",
        description=(
            "Verify the stored authorization with one refresh and one authenticated MCP "
            "initialize/tools-list probe. Prints one of: not_logged_in, connected, "
            "authorization_required, unavailable, store_unavailable."
        ),
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands.add_parser(
        "clear",
        help="delete the local authorization record",
        description=(
            "Delete only the local record. Remote access is revoked under "
            "zenture -> Zugriff & Sicherheit -> Verbindungen."
        ),
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    return parser


def _exit_code(exc: AuthError) -> int:
    if isinstance(exc, LoginCancelled):
        # Ctrl+C during device login exits like browser login and status; a denial stays 6.
        return EXIT_INTERRUPTED if exc.code == "device_login_interrupted" else EXIT_CANCELLED
    if isinstance(exc, SecureStoreUnavailable):
        return EXIT_STORE_UNAVAILABLE
    if isinstance(exc, PermissionDenied):
        return EXIT_PERMISSION_DENIED
    if isinstance(exc, AuthorizationRequired):
        return EXIT_AUTHORIZATION_REQUIRED
    if isinstance(exc, AuthUnavailable):
        return EXIT_UNAVAILABLE
    return EXIT_UNAVAILABLE


def _explain(exc: AuthError) -> str:
    known = _EXPLANATIONS.get(exc.code)
    if known is not None:
        return known
    if isinstance(exc, AuthorizationRequired):
        return f"Authorization is required ({exc.code}). Run `zenture auth login`."
    if isinstance(exc, PermissionDenied):
        return "The server refused access for this authorization."
    return f"Could not complete the request ({exc.code}). Nothing was changed."


def _login(
    args: argparse.Namespace, runtime: Runtime, endpoint: str | None, out: IO[str], err: IO[str]
) -> int:
    def present(prompt: DevicePrompt) -> None:
        print(
            f"To authorize this device, open {prompt.verification_uri}\n"
            f"and enter the code {prompt.user_code}\n"
            "Waiting for approval (up to 10 minutes; press Ctrl+C to stop)...",
            file=out,
            flush=True,
        )

    flow = LoginFlow(dataclasses.replace(runtime, presenter=present))
    try:
        session = flow.login(device=args.device, session_only=args.session_only, endpoint=endpoint)
    except AuthError as exc:
        print(_explain(exc), file=err)
        return _exit_code(exc)
    except ZentureMCPError as exc:
        print(
            f"The MCP endpoint check failed ({exc.code}). This is not a temporary outage; "
            "the stored authorization is unchanged and no new authorization was started.",
            file=err,
        )
        return EXIT_UNAVAILABLE
    binding = session.binding
    session.close()
    if args.session_only:
        print(
            f"Login verified for connection {binding.connection_id}. Nothing was stored: the "
            "authorization cannot outlive this command. Run `zenture auth login` without "
            "--session-only to keep it.",
            file=out,
        )
    else:
        print(
            f"Logged in (connection {binding.connection_id}). The authorization is stored in "
            "the system credential store.",
            file=out,
        )
    return EXIT_OK


_STATUS_EXIT = {
    "connected": EXIT_OK,
    "not_logged_in": EXIT_NOT_LOGGED_IN,
    "authorization_required": EXIT_AUTHORIZATION_REQUIRED,
    "unavailable": EXIT_UNAVAILABLE,
    "store_unavailable": EXIT_STORE_UNAVAILABLE,
}


def _print_status(report: StatusReport, out: IO[str]) -> None:
    print(f"status: {report.state}", file=out)
    if report.issuer is not None:
        print(f"issuer: {report.issuer}", file=out)
    if report.resource is not None:
        print(f"resource: {report.resource}", file=out)
    binding = report.binding
    if binding is not None:
        print(
            f"connection: {binding.connection_id} (sub={binding.sub}, "
            f"owner_epoch={binding.owner_epoch}, "
            f"authorization_generation={binding.authorization_generation})",
            file=out,
        )
    if report.code is not None:
        print(f"reason: {report.code}", file=out)
    if report.state == "authorization_required" or report.state == "not_logged_in":
        print("next: run `zenture auth login`", file=out)


def _clear(runtime: Runtime, endpoint: str | None, out: IO[str], err: IO[str]) -> int:
    try:
        existed = clear_stored(endpoint, runtime)
    except AuthError as exc:
        print(_explain(exc), file=err)
        return _exit_code(exc)
    if existed:
        print(f"Local authorization record deleted. {_REMOTE_NOTE}", file=out)
    else:
        print(f"No stored authorization found. {_REMOTE_NOTE}", file=out)
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: Runtime | None = None,
    endpoint: str | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the command line; ``runtime``/``endpoint`` are test seams, not CLI options."""

    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args = _build_parser().parse_args(argv)
    chosen = runtime or login_module.default_runtime()
    try:
        if args.command == "login":
            return _login(args, chosen, endpoint, out, err)
        if args.command == "status":
            report = check_status(endpoint, chosen)
            _print_status(report, out)
            return _STATUS_EXIT[report.state]
        return _clear(chosen, endpoint, out, err)
    except KeyboardInterrupt:
        print("Interrupted.", file=err)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
