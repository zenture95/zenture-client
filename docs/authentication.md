# Authentication

`zenture` has two separate credentials. Pick the one that matches the channel:

| Channel | Credential | How it is obtained |
|---|---|---|
| REST API (`ZentureClient`, `AsyncZentureClient`) | zenture API token | Created by you in the webapp; read from `ZENTURE_API_KEY` |
| Hosted MCP endpoint (`zenture.mcp`) | Your zenture account, authorized on this machine | `zenture auth login` (or `zenture.auth.login`) |

The two never mix: the MCP login never reads `ZENTURE_API_KEY`, and an API token
cannot be used for MCP.

## API tokens (REST)

`zenture` is server-side only. zenture API tokens are credentials for trusted
backend environments such as services, workers, automation jobs, CI, and
controlled notebooks.

Do not embed API tokens in browsers, mobile apps, frontend bundles, public
notebooks, logs, traces, analytics, screenshots, support tickets, or
customer-visible errors.

### Token Creation

Create API tokens in the existing zenture webapp with an existing account:
`https://ai.zenture.app/profile?tab=api-tokens`.

The SDK intentionally does not expose an API-token management surface for
creating, rotating, revoking, or auditing tokens.

### Environment Setup

Store tokens outside Python source code:

```bash
# .env, not committed
ZENTURE_API_KEY=your_webapp_created_api_token
```

```bash
set -a
. ./.env
set +a
```

Use `ZentureClient.from_env()` or `AsyncZentureClient.from_env()` so application code reads
the token from `ZENTURE_API_KEY`.

### Base URL

The production origin is `https://api.zenture.app`.

Public SDK usage targets the production API. Never derive constructor
`base_url` values from user input. URL credentials, paths, query strings,
fragments, plain HTTP origins, and arbitrary HTTPS origins are rejected by SDK
configuration validation.

## Native login for MCP

Login is always an explicit action. Importing `zenture.auth`, `zenture.mcp` or
`zenture.cli`, and connecting with `McpClient.connect()`, never opens a browser,
never contacts the network by itself at import and never touches the credential
store at import. Only `zenture auth login`, `zenture.auth.login(...)` and
`zenture.auth.login_async(...)` start a login.

### Browser login (default)

```bash
zenture auth login
```

The client opens your system browser and asks you to approve access in zenture.
It uses the OAuth authorization code flow with PKCE: the browser returns to a
loopback address that exists only on your machine for the duration of the login
(the listener binds before the browser opens, accepts one callback and is closed
afterwards; on Windows the port is claimed exclusively so no other local process can share it). The sign-in must finish within 10 minutes. If no browser can be
opened or no loopback address is available, the command stops and suggests
`--device`; it never switches flows on its own.

### Device login (headless machines)

```bash
zenture auth login --device
```

The terminal shows a verification address and a one-time code; the client only shows an address on the issuer's own origin. Enter the code on
any other device where you are signed in to zenture. The client waits at most
10 minutes for your approval. Press Ctrl+C to stop: the pending request simply
expires on the server and nothing is connected. If the code expires, or the
result of the approval could not be confirmed, log in again; the client never
retries with a code that may already be consumed.

### Reusing an existing login

When a stored authorization exists, `zenture auth login` refreshes it once and
verifies it with one authenticated probe instead of opening the browser. A new
browser authorization starts only when the stored one is no longer usable.

### Where the authorization lives

The authorization is stored in the operating system's credential store, one item
per account on this machine (single stored account). Only a refresh credential
is stored; short-lived access tokens stay in memory. Logging in again replaces the
stored account; a session object created earlier for the replaced account is
refused instead of silently acting as the new one.

| Platform | Store | Status |
|---|---|---|
| macOS | Keychain | proven |
| Windows | Credential Manager | implemented, not yet verified |
| Linux | Secret Service | implemented, not yet verified |

If no protected store is available you get `SecureStoreUnavailable`. Use
`zenture auth login --session-only` (or `login(session_only=True)`) to keep the
authorization in memory for the current process only; nothing is written and it
cannot outlive the process. In the CLI, `--session-only` verifies the login and
then discards it.

### Refresh safety

Access is refreshed automatically when it expires, with one refresh at a time
per account even across several processes. A refresh replaces the stored
credential. If a refresh is interrupted at a moment where the server may already
have replaced it (for example the connection drops after the request was sent),
the client cannot know which credential is valid. The first interrupted refresh
raises `AuthorizationRequired` (`rotation_outcome_unknown`) and marks the local
record. The next attempt deletes that record and requires you to log in again.
The client never reuses the old credential and never retries it.

### Status and removal

```bash
zenture auth status
zenture auth clear
```

`status` refreshes once and performs one authenticated connection check that does
not start any Run, then prints one of `not_logged_in`, `connected`,
`authorization_required`, `unavailable` or `store_unavailable`, with the issuer,
resource and connection summary (never a credential) and a fitting exit code.
`clear` deletes only the local record. Access that was already granted stays valid
until you revoke the connection in zenture under
*Zugriff & Sicherheit -> Verbindungen*.

### Errors

All are importable from `zenture.auth`.

| Error | Next action |
|---|---|
| `AuthorizationRequired` | Run `zenture auth login` |
| `AuthUnavailable` | Try again later; nothing was changed |
| `SecureStoreUnavailable` | Use `zenture auth login --session-only` |
| `PermissionDenied` | Contact the account owner; a new login does not widen access |
| `LoginCancelled` | Nothing was connected |

Exit codes of `zenture auth ...`: `0` ok, `1` not logged in, `2` usage, `3`
authorization required, `4` unavailable, `5` store unavailable, `6` cancelled or denied,
`7` permission denied, `130` interrupted (Ctrl+C, also during a device login). Never print, log or paste credentials;
the client itself never does.
