"""Public error taxonomy of the native authorization core."""

from __future__ import annotations

from zenture.errors import ZentureError


class AuthError(ZentureError):
    """Base class for native authorization failures.

    Messages carry a fixed machine code only; they never contain tokens,
    authorization codes, state values or server response bodies.
    """

    default_code = "auth_error"
    default_action = "check_request"

    def __init__(self, code: str | None = None, *, next_action: str | None = None) -> None:
        self.code = code or self.default_code
        self.next_action = next_action or self.default_action
        super().__init__(f"{type(self).__name__}: {self.code}")


class AuthorizationRequired(AuthError):
    """The stored or presented authorization can no longer be used; log in again."""

    default_code = "authorization_required"
    default_action = "login"


class AuthUnavailable(AuthError):
    """Authorization could not be completed now; the stored authorization is unchanged."""

    default_code = "auth_unavailable"
    default_action = "retry_later"


class SecureStoreUnavailable(AuthError):
    """No protected native credential store is usable; session-only login is possible."""

    default_code = "secure_store_unavailable"
    default_action = "use_session_only"


class LoginCancelled(AuthError):
    """The user declined or cancelled the browser authorization."""

    default_code = "login_cancelled"
    default_action = "none"


class PermissionDenied(AuthError):
    """The server refused the request for the authorized client (HTTP 403); no scope uplift."""

    default_code = "permission_denied"
    default_action = "contact_owner"
