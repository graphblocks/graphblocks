#!/usr/bin/env python3
"""Run fail-closed Python and Rust dependency audits with expiring exceptions."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCEPTIONS_PATH = ROOT / "security/vulnerability-exceptions.yaml"
ALLOWED_ECOSYSTEMS = frozenset({"python", "rust"})
EXCEPTION_FIELDS = frozenset(
    {
        "id",
        "ecosystem",
        "advisoryId",
        "package",
        "reason",
        "evidenceUrl",
        "expiresOn",
    }
)


class DependencyAuditError(ValueError):
    """Raised when dependency-audit configuration is not exact and safe."""


def _exact_text(value: object, owner: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DependencyAuditError(f"{owner} must be exact non-empty text")
    return value


def load_exceptions(
    path: Path,
    *,
    today: date | None = None,
) -> tuple[dict[str, str], ...]:
    """Load a closed, non-expired vulnerability exception manifest."""

    try:
        raw_manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise DependencyAuditError(
            "vulnerability exception manifest cannot be loaded"
        ) from error
    if type(raw_manifest) is not dict or set(raw_manifest) != {
        "version",
        "exceptions",
    }:
        raise DependencyAuditError(
            "vulnerability exception manifest must contain exactly version and exceptions"
        )
    if raw_manifest["version"] != 1 or type(raw_manifest["exceptions"]) is not list:
        raise DependencyAuditError(
            "vulnerability exception manifest version or exceptions is invalid"
        )

    current_date = today or date.today()
    exceptions: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_advisories: set[tuple[str, str]] = set()
    for index, raw_exception in enumerate(raw_manifest["exceptions"]):
        owner = f"vulnerability exception {index}"
        if type(raw_exception) is not dict or set(raw_exception) != EXCEPTION_FIELDS:
            raise DependencyAuditError(
                f"{owner} must contain exactly {sorted(EXCEPTION_FIELDS)!r}"
            )
        exception = {
            field: _exact_text(raw_exception[field], f"{owner} {field}")
            for field in EXCEPTION_FIELDS
        }
        exception_id = exception["id"]
        if exception_id in seen_ids:
            raise DependencyAuditError(f"{owner} id must be unique")
        seen_ids.add(exception_id)
        if exception["ecosystem"] not in ALLOWED_ECOSYSTEMS:
            raise DependencyAuditError(f"{owner} ecosystem is unsupported")
        advisory_key = (exception["ecosystem"], exception["advisoryId"])
        if advisory_key in seen_advisories:
            raise DependencyAuditError(
                f"{owner} advisoryId must be unique within its ecosystem"
            )
        seen_advisories.add(advisory_key)
        try:
            expires_on = date.fromisoformat(exception["expiresOn"])
        except ValueError as error:
            raise DependencyAuditError(
                f"{owner} expiresOn must be an ISO calendar date"
            ) from error
        if expires_on < current_date:
            raise DependencyAuditError(f"{owner} has expired")
        evidence_url = urlparse(exception["evidenceUrl"])
        if evidence_url.scheme != "https" or not evidence_url.netloc:
            raise DependencyAuditError(
                f"{owner} evidenceUrl must be an absolute HTTPS URL"
            )
        exceptions.append(exception)
    return tuple(exceptions)


def run_dependency_audits(
    *,
    exceptions_path: Path,
    output_dir: Path,
    today: date | None = None,
) -> int:
    """Run both audit tools and retain their machine-readable evidence."""

    exceptions = load_exceptions(exceptions_path, today=today)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "vulnerability-exceptions.json").write_text(
        json.dumps(
            {"version": 1, "exceptions": exceptions},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    python_command = [
        sys.executable,
        "-m",
        "pip_audit",
        str(ROOT),
        "--strict",
        "--format=json",
        "--desc=off",
        "--aliases=off",
        "--progress-spinner=off",
        "--output",
        str(output_dir / "pip-audit.json"),
    ]
    rust_command = ["cargo", "audit", "--json"]
    for exception in exceptions:
        if exception["ecosystem"] == "python":
            python_command.extend(("--ignore-vuln", exception["advisoryId"]))
        else:
            rust_command.extend(("--ignore", exception["advisoryId"]))

    python_result = subprocess.run(python_command, cwd=ROOT, check=False)
    with (output_dir / "cargo-audit.json").open("w", encoding="utf-8") as output:
        rust_result = subprocess.run(
            rust_command,
            cwd=ROOT,
            check=False,
            stdout=output,
            text=True,
        )
    failed = []
    if python_result.returncode != 0:
        failed.append("pip-audit")
    if rust_result.returncode != 0:
        failed.append("cargo-audit")
    if failed:
        print("dependency audit failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(
        "Dependency audit passed for Python and Rust; "
        f"active exceptions: {len(exceptions)}."
    )
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTIONS_PATH,
        help="closed vulnerability exception manifest",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory for machine-readable audit evidence",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run_dependency_audits(
            exceptions_path=args.exceptions,
            output_dir=args.output_dir,
        )
    except DependencyAuditError as error:
        print(f"dependency audit configuration failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
