# Migrating to the canonical zenture client

The canonical distribution and Python import package are both `zenture`.
The previous published distribution is `zenture-sdk`; it also owns the
`zenture` import package. Never install both distributions in the same Python
environment: uninstall one before installing the other.

This is a hard cutover. The canonical REST classes are `ZentureClient` and
`AsyncZentureClient`; there are no `Zenture` / `AsyncZenture` aliases or new
compatibility distributions. The default install includes REST, hosted MCP,
native authentication, and the `zenture` command. It provides no local MCP
server. Backend/Engine remain the authorization, execution, billing, and
tenant authorities.

## Upgrade in one virtual environment

Canonical PyPI publication is pending. Use an approved checkout or a candidate
wheel supplied by the repository owner until publication. Do not treat
`pip install zenture` as an available public release.

Use a user-owned virtual environment for this application and keep the same
interpreter for every install and uninstall. For example, with Python 3.11
on a POSIX shell:

```bash
python3 -m venv .venv-zenture
.venv-zenture/bin/python -m pip uninstall -y zenture-sdk
.venv-zenture/bin/python -m pip install /path/to/zenture-client
```

For an existing virtual environment containing `zenture-sdk`, skip creation
and substitute its Python executable in both commands. On Windows, the
executable inside the environment is `Scripts/python.exe` instead of
`bin/python`. These command forms do not establish platform verification.

To install an approved provided wheel instead of the checkout, use this as
the install step after uninstalling `zenture-sdk`:

```bash
.venv-zenture/bin/python -m pip install /path/to/approved/zenture-candidate.whl
```

Replace that placeholder with the actual wheel filename supplied to you.

Update your application imports and class references together:

```diff
-from zenture import AsyncZenture, Zenture
+from zenture import AsyncZentureClient, ZentureClient
```

The existing REST API-token contract is unchanged. Keep using an externally
provided `ZENTURE_API_KEY` with the canonical REST clients. API-token access
remains noninteractive; MCP login does not read that token. MCP uses separate
account OAuth credentials and explicit login; ordinary client calls never
start login. See [authentication](./authentication.md) and
[hosted MCP clients](./mcp-client.md) for those public interfaces.

## Return to the published baseline

Use the same application virtual environment, uninstall the canonical
distribution, and reinstall the exact historical published version:

```bash
.venv-zenture/bin/python -m pip uninstall -y zenture
.venv-zenture/bin/python -m pip install zenture-sdk==1.0.0rc3
```

Restore the application imports and constructor names to the legacy form.
This inverse diff targets the historical published baseline, not the canonical
client:

```diff
-from zenture import AsyncZentureClient, ZentureClient
+from zenture import AsyncZenture, Zenture
```

`zenture-sdk==1.0.0rc3` is a retained historical baseline. It is not overwritten,
an alias, or a separate fallback product. Returning to it restores that
artifact's legacy client surface; it does not supply the canonical client's
new native OAuth, CLI, or Run functionality. The return path does not establish
compatibility with current services. Review the baseline's actual capabilities
before relying on it for your application.

## Configuration and credentials

If you reuse application configuration, keep credentials external to source
and opaque: do not inspect, print, copy into code, or log their values. Preserve
the separation between REST API tokens and MCP account authorization.

Package uninstall neither revokes server Connections nor promises credential
cleanup. Local credential removal and server revocation are separate actions:
`zenture auth clear` removes only the local record, while revocation happens
in the webapp under *Zugriff & Sicherheit -> Verbindungen*. Follow the existing
[status and removal guidance](./authentication.md#status-and-removal)
separately when needed; perform any canonical CLI cleanup before uninstalling
the package that provides it.

## What has been verified

An offline artifact rehearsal installed published `zenture-sdk==1.0.0rc3`,
uninstalled it, installed the canonical candidate, then uninstalled the
candidate and returned to the published baseline in one isolated virtual
environment. It checked the distribution and class names at each stage and
the absence of the other distribution and incompatible class exports. The
historical and candidate artifact hashes remained unchanged.

That rehearsal covered macOS with Python 3.11 and reused existing dependencies.
It did not prove clean dependency installation, the full Python or three-OS
matrix, native authentication, service compatibility, or public publication.
Those release gates remain separate; the rehearsal is package transition
proof only.
