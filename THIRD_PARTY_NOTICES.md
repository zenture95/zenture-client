# Third-Party Notices

This document summarizes third-party software used by the zenture Python client.
It is provided for release transparency and does not replace the upstream
license files or metadata published by each project.

## Runtime Dependencies

The following direct dependencies are required by the base client install,
including API, MCP and authentication support. License identities were checked
against the installed distribution metadata and shipped upstream license files
for the versions listed below; dependency ranges remain as declared in
`pyproject.toml`. Upstream links identify the projects that publish those files.

| Package | License | Notes |
| --- | --- | --- |
| `httpx` | BSD-3-Clause | Required direct HTTP client dependency (`>=0.27,<1`); verified at `0.28.1`, shipped `LICENSE.md`. [Upstream](https://github.com/encode/httpx). |
| `httpx2` | BSD-3-Clause | Required direct HTTP client dependency (`>=2.5,<3`); verified at `2.13.1`, shipped `LICENSE.md`. [Upstream](https://github.com/pydantic/httpx2). |
| `pydantic` | MIT | Required direct data validation/model dependency (`>=2.7,<3`); verified at `2.13.5`, shipped `LICENSE`. [Upstream](https://github.com/pydantic/pydantic). |
| `mcp` | MIT | Required direct Model Context Protocol client transport dependency (`>=2.0.0,<2.1`); verified at `2.0.1`, shipped `LICENSE`. [Upstream](https://github.com/modelcontextprotocol/python-sdk). |
| `keyring` | MIT | Required direct protected credential-store dependency (`>=25.6,<26`); verified at `25.7.0`, shipped `LICENSE`. [Upstream](https://github.com/jaraco/keyring). |
| `PyJWT` (`pyjwt[crypto]`) | MIT | Required direct JWT dependency with the `crypto` extra (`>=2.10,<3`); verified at `2.15.1`, shipped `LICENSE`. [Upstream](https://github.com/jpadilla/pyjwt). |
| `cryptography` | Apache-2.0 OR BSD-3-Clause | Required through the mandatory PyJWT `crypto` extra (`cryptography>=3.4.0` in the verified PyJWT metadata); verified at `50.0.2`, shipped `LICENSE`, `LICENSE.APACHE` and `LICENSE.BSD`. [Upstream](https://github.com/pyca/cryptography). |

The following selected transitive dependencies are also listed for context.
This document is not a complete transitive dependency or SBOM inventory.

| Package | License | Notes |
| --- | --- | --- |
| `httpcore` | BSD-3-Clause | Transitive dependency of `httpx`. |
| `anyio` | MIT | Transitive dependency of `httpx`. |
| `certifi` | MPL-2.0 | Transitive dependency of `httpx`; provides CA certificates. |
| `h11` | MIT | Transitive dependency of `httpcore`. |
| `idna` | BSD-3-Clause | Transitive dependency of `httpx`. |
| `annotated-types` | MIT | Transitive dependency of `pydantic`. |
| `pydantic-core` | MIT | Transitive dependency of `pydantic`. |
| `typing-extensions` | PSF-2.0 | Transitive dependency of `pydantic`. |
| `typing-inspection` | MIT | Transitive dependency of `pydantic`. |

## MCP Extra

MCP is already a required base dependency. The optional `mcp` extra remains
available in `pyproject.toml` and declares `mcp==2.0.0`, narrowing the base range
to that exact version. Selecting this extra does not make MCP installation
optional in the base client.

## Build Dependencies

These packages are used to build source distributions and wheels.

| Package | License | Notes |
| --- | --- | --- |
| `hatchling` | MIT | PEP 517 build backend. |
| `packaging` | Apache-2.0 OR BSD-2-Clause | Transitive dependency of `hatchling`. |
| `pathspec` | MPL-2.0 | Transitive dependency of `hatchling`. |
| `pluggy` | MIT | Transitive dependency of `hatchling`. |
| `trove-classifiers` | Apache-2.0 | Transitive dependency of `hatchling`. |

## Development and Test Dependencies

These packages are used for local development, CI, testing, type checking, and
package validation. They are not required for normal client use.

| Package | License | Notes |
| --- | --- | --- |
| `build` | MIT | Package build command. |
| `coverage` | Apache-2.0 | Test coverage reporting. |
| `mypy` | MIT | Static type checking. |
| `pyright` | MIT | Static type checking. |
| `pytest` | MIT | Test runner. |
| `pytest-asyncio` | Apache-2.0 | Async pytest support. |
| `respx` | BSD-3-Clause | HTTPX test mocking. |
| `ruff` | MIT | Formatting and linting. |
| `twine` | Apache-2.0 | Package distribution validation and upload tooling. |

## License Summary Scope

The required direct dependencies above use BSD-3-Clause or MIT licenses.
`cryptography`, required by the PyJWT `crypto` extra, offers Apache-2.0 OR
BSD-3-Clause. The selected transitive and build dependencies include MPL-2.0
packages (`certifi` and `pathspec`). These notices report license identities;
they do not establish a complete reviewed dependency/license graph or a license
policy acceptance for every transitive dependency.
