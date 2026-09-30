"""Validate built sdist and wheel contents for public release safety."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

FORBIDDEN_NAME_PARTS = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "scratch/",
)
FORBIDDEN_TOKEN_LITERAL = re.compile(r"\bzt_(?:live|test)_[A-Za-z0-9_-]+\b")
FORBIDDEN_PUBLIC_TEXT_PATTERNS = {
    "local debugging origin": re.compile(r"\blocalhost\b|\b127\.0\.0\.1\b|\[::1\]", re.IGNORECASE),
    "integration origin": re.compile(r"\bapi-int\.zenture\.app\b", re.IGNORECASE),
    "non-production API origin": re.compile(r"\bapi-[a-z0-9-]+\.zenture\.app\b", re.IGNORECASE),
    "token prefix": re.compile(r"zt_(?:live|test)_"),
}
PUBLIC_ARTIFACT_PARTS = (
    "/README.md",
    "/AGENTS.md",
    "/SECURITY.md",
    "/CONTRIBUTING.md",
    "/docs/",
    "/examples/",
    "/openapi/",
)


def _one_artifact(dist_dir: Path, pattern: str) -> Path:
    matches = sorted(dist_dir.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one {pattern} artifact, found {len(matches)}.")
    return matches[0]


def _tar_names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return set(archive.getnames())


def _zip_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def _assert_no_forbidden_names(names: set[str], *, artifact: str) -> None:
    for name in names:
        for part in FORBIDDEN_NAME_PARTS:
            if part in name:
                raise SystemExit(f"{artifact} unexpectedly contains {name}.")


def _assert_no_forbidden_text_in_tar(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            _assert_no_forbidden_text(member.name, data)


def _assert_no_forbidden_text_in_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            _assert_no_forbidden_text(name, archive.read(name))


def _assert_no_forbidden_text(name: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    if FORBIDDEN_TOKEN_LITERAL.search(text):
        raise SystemExit(f"{name} contains forbidden token-shaped literal.")

    if not any(part in name for part in PUBLIC_ARTIFACT_PARTS):
        return

    for label, pattern in FORBIDDEN_PUBLIC_TEXT_PATTERNS.items():
        if pattern.search(text):
            raise SystemExit(f"{name} contains forbidden {label}.")


def _has_suffix(names: set[str], suffix: str) -> bool:
    return any(name.endswith(suffix) for name in names)


def _has_prefix(names: set[str], prefix: str) -> bool:
    return any(name.startswith(prefix) for name in names)


def _assert_metadata(data: bytes) -> None:
    metadata = Parser().parsestr(data.decode("utf-8"))
    if metadata["Name"] != "zenture":
        raise SystemExit("Artifact distribution must be zenture.")
    if not metadata["Version"]:
        raise SystemExit("Artifact is missing its release version.")
    urls = metadata.get_all("Project-URL", [])
    if "Repository, https://github.com/zenture95/zenture-client" not in urls:
        raise SystemExit("Artifact is missing the canonical repository URL.")
    if "Issues, https://github.com/zenture95/zenture-client/issues" not in urls:
        raise SystemExit("Artifact is missing the canonical issue URL.")


def _assert_sdist(path: Path) -> None:
    names = _tar_names(path)
    _assert_no_forbidden_names(names, artifact="sdist")
    _assert_no_forbidden_text_in_tar(path)
    with tarfile.open(path, "r:gz") as archive:
        metadata = archive.extractfile(f"{path.name.removesuffix('.tar.gz')}/PKG-INFO")
        if metadata is None:
            raise SystemExit("sdist is missing PKG-INFO.")
        _assert_metadata(metadata.read())
    required_suffixes = [
        "AGENTS.md",
        "README.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
        "docs/pagination.md",
        "examples/pagination.py",
        "openapi/zenture-public-api-v1.openapi.json",
        "tests/unit/test_docs_examples.py",
        "src/zenture/py.typed",
    ]
    forbidden_suffixes = [
        "PLAN_ZENTURE_SDK.md",
        ".env",
        ".coverage",
    ]

    for suffix in required_suffixes:
        if not _has_suffix(names, suffix):
            raise SystemExit(f"sdist is missing {suffix}.")
    for suffix in forbidden_suffixes:
        if _has_suffix(names, suffix):
            raise SystemExit(f"sdist unexpectedly contains {suffix}.")


def _assert_wheel(path: Path) -> None:
    names = _zip_names(path)
    _assert_no_forbidden_names(names, artifact="wheel")
    _assert_no_forbidden_text_in_zip(path)
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit("wheel must contain exactly one METADATA file.")
        _assert_metadata(archive.read(metadata_names[0]))
    required = [
        "zenture/__init__.py",
        "zenture/py.typed",
    ]
    forbidden_prefixes = [
        "docs/",
        "examples/",
        "openapi/",
        "tests/",
    ]
    forbidden_suffixes = [
        "AGENTS.md",
        "PLAN_ZENTURE_SDK.md",
    ]

    for file_name in required:
        if file_name not in names:
            raise SystemExit(f"wheel is missing {file_name}.")
    if not any(name.endswith(".dist-info/METADATA") for name in names):
        raise SystemExit("wheel is missing METADATA.")
    for prefix in forbidden_prefixes:
        if _has_prefix(names, prefix):
            raise SystemExit(f"wheel unexpectedly contains {prefix}.")
    for suffix in forbidden_suffixes:
        if _has_suffix(names, suffix):
            raise SystemExit(f"wheel unexpectedly contains {suffix}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args()

    dist_dir = args.dist_dir
    if not dist_dir.is_dir():
        raise SystemExit(f"{dist_dir} is not a directory.")

    _assert_sdist(_one_artifact(dist_dir, "*.tar.gz"))
    _assert_wheel(_one_artifact(dist_dir, "*.whl"))


if __name__ == "__main__":
    main()
