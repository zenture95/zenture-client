"""Fail CI when installed dependencies use release-unfriendly licenses."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import distributions

IGNORED_PACKAGES = {
    "pip",
    "setuptools",
    "wheel",
    "zenture",
}

ALLOWED_LICENSE_MARKERS = (
    "apache",
    "bsd",
    "mit",
    "mpl",
    "mozilla public license",
    "psf",
    "python software foundation",
    "public domain",
)

BANNED_LICENSE_MARKERS = (
    "agpl",
    "lgpl",
    "gpl",
    "proprietary",
)

DUAL_LICENSE_ALLOWLIST = {
    # docutils publishes Public Domain/BSD/GPL classifiers. The permissive
    # options are acceptable for tooling use, but the GPL classifier should stay
    # visible instead of becoming a blanket exception.
    "docutils",
}


@dataclass(frozen=True)
class LicenseFinding:
    package: str
    version: str
    license_text: str
    status: str
    reason: str


def _license_text(distribution_metadata: object) -> str:
    metadata = distribution_metadata.metadata  # type: ignore[attr-defined]
    license_expression = metadata.get("License-Expression") or ""
    license_value = metadata.get("License") or ""
    classifiers = metadata.get_all("Classifier") or []
    license_classifiers = [
        classifier for classifier in classifiers if classifier.startswith("License ::")
    ]
    return " ".join([license_expression, license_value, *license_classifiers]).strip()


def _classify(package: str, license_text: str) -> tuple[str, str]:
    normalized = license_text.lower()
    if not normalized:
        return ("fail", "missing license metadata")

    has_banned_marker = any(marker in normalized for marker in BANNED_LICENSE_MARKERS)
    has_allowed_marker = any(marker in normalized for marker in ALLOWED_LICENSE_MARKERS)

    if has_banned_marker and package.lower() not in DUAL_LICENSE_ALLOWLIST:
        return ("fail", "contains banned copyleft/proprietary marker")
    if has_banned_marker and has_allowed_marker:
        return ("warn", "contains banned marker but is explicitly dual-license allowlisted")
    if not has_allowed_marker:
        return ("fail", "no approved license marker found")
    return ("pass", "approved license marker found")


def main() -> None:
    findings: list[LicenseFinding] = []
    for distribution in sorted(
        distributions(),
        key=lambda installed: installed.metadata.get("Name", "").lower(),
    ):
        package = distribution.metadata.get("Name", "")
        normalized_package = package.lower()
        if normalized_package in IGNORED_PACKAGES:
            continue
        license_text = _license_text(distribution)
        status, reason = _classify(normalized_package, license_text)
        findings.append(
            LicenseFinding(
                package=package,
                version=distribution.version,
                license_text=license_text,
                status=status,
                reason=reason,
            )
        )

    for finding in findings:
        print(
            f"{finding.status.upper():4} {finding.package}=={finding.version} "
            f"- {finding.reason}: {finding.license_text}"
        )

    failures = [finding for finding in findings if finding.status == "fail"]
    if failures:
        raise SystemExit(f"Dependency license check failed for {len(failures)} package(s).")


if __name__ == "__main__":
    main()
