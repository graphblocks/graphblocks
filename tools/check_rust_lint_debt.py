#!/usr/bin/env python3
"""Reject growth in the bounded production Rust ``expect`` debt."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tomllib
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "compatibility" / "rust-production-expect-budget.json"
LINT = "clippy::expect_used"
PRODUCTION_SCOPE = ("--workspace", "--lib", "--bins", "--locked")
ALLOW_MARKER = (
    "#![allow(clippy::expect_used)] // Guarded by "
    "compatibility/rust-production-expect-budget.json."
)
BASELINE_FIELDS = frozenset(
    {"schemaVersion", "lint", "toolchain", "scope", "total", "files"}
)


class RustLintDebtError(ValueError):
    """Raised when the production lint policy or baseline is invalid."""


@dataclass(frozen=True)
class RustLintBaseline:
    toolchain: str
    files: Mapping[str, int]

    @property
    def total(self) -> int:
        return sum(self.files.values())


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RustLintDebtError(f"duplicate JSON field: {key!r}")
        result[key] = value
    return result


def _exact_object(
    value: object, *, path: str, fields: frozenset[str]
) -> dict[str, Any]:
    if type(value) is not dict:
        raise RustLintDebtError(f"{path}: expected object")
    mapping = value
    actual = frozenset(mapping)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        raise RustLintDebtError(
            f"{path}: fields do not match schema; missing={missing}, unknown={unknown}"
        )
    return mapping


def parse_baseline(value: object, *, root: Path = ROOT) -> RustLintBaseline:
    payload = _exact_object(value, path="$", fields=BASELINE_FIELDS)
    if payload["schemaVersion"] != 1:
        raise RustLintDebtError("$.schemaVersion: expected 1")
    if payload["lint"] != LINT:
        raise RustLintDebtError(f"$.lint: expected {LINT!r}")
    if payload["scope"] != list(PRODUCTION_SCOPE):
        raise RustLintDebtError(f"$.scope: expected {list(PRODUCTION_SCOPE)!r}")

    toolchain = payload["toolchain"]
    if type(toolchain) is not str or not toolchain:
        raise RustLintDebtError("$.toolchain: expected non-empty string")

    raw_files = payload["files"]
    if type(raw_files) is not dict or not raw_files:
        raise RustLintDebtError("$.files: expected non-empty object")
    files: dict[str, int] = {}
    for raw_path, raw_count in raw_files.items():
        if type(raw_path) is not str:
            raise RustLintDebtError("$.files: every path must be a string")
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "crates"
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix != ".rs"
        ):
            raise RustLintDebtError(f"$.files[{raw_path!r}]: invalid Rust source path")
        if type(raw_count) is not int or raw_count <= 0:
            raise RustLintDebtError(f"$.files[{raw_path!r}]: expected positive integer")
        if not (root / raw_path).is_file():
            raise RustLintDebtError(
                f"$.files[{raw_path!r}]: source file does not exist"
            )
        files[raw_path] = raw_count

    declared_total = payload["total"]
    if type(declared_total) is not int or declared_total != sum(files.values()):
        raise RustLintDebtError(
            f"$.total: expected exact file sum {sum(files.values())}, got {declared_total!r}"
        )
    return RustLintBaseline(toolchain=toolchain, files=dict(sorted(files.items())))


def load_baseline(path: Path = BASELINE_PATH, *, root: Path = ROOT) -> RustLintBaseline:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, json.JSONDecodeError, RustLintDebtError) as error:
        raise RustLintDebtError(f"could not load {path}: {error}") from error
    return parse_baseline(value, root=root)


def verify_policy(baseline: RustLintBaseline, *, root: Path = ROOT) -> None:
    workspace = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    lint_level = (
        workspace.get("workspace", {})
        .get("lints", {})
        .get("clippy", {})
        .get("expect_used")
    )
    if lint_level != "deny":
        raise RustLintDebtError(
            "Cargo.toml must set workspace.lints.clippy.expect_used to 'deny'"
        )

    toolchain = tomllib.loads(
        (root / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    if toolchain.get("toolchain", {}).get("channel") != baseline.toolchain:
        raise RustLintDebtError(
            "baseline toolchain must match rust-toolchain.toml exactly"
        )

    marked_files: set[str] = set()
    for source in sorted((root / "crates").glob("*/src/**/*.rs")):
        if ALLOW_MARKER in source.read_text(encoding="utf-8"):
            marked_files.add(source.relative_to(root).as_posix())
    expected_files = set(baseline.files)
    if marked_files != expected_files:
        raise RustLintDebtError(
            "expect allow markers must exactly match baseline files; "
            f"missing={sorted(expected_files - marked_files)}, "
            f"unknown={sorted(marked_files - expected_files)}"
        )


def clippy_command() -> tuple[str, ...]:
    return (
        "cargo",
        "clippy",
        *PRODUCTION_SCOPE,
        "--message-format=json",
        "--",
        "--force-warn",
        LINT,
    )


def parse_clippy_messages(lines: Iterable[str], *, root: Path = ROOT) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise RustLintDebtError(
                f"Clippy output line {line_number} is not JSON: {error}"
            ) from error
        if message.get("reason") != "compiler-message":
            continue
        diagnostic = message.get("message", {})
        code = diagnostic.get("code")
        if type(code) is not dict or code.get("code") != LINT:
            continue
        primary_spans = [
            span
            for span in diagnostic.get("spans", [])
            if span.get("is_primary") is True
        ]
        if len(primary_spans) != 1:
            raise RustLintDebtError(
                f"{LINT} diagnostic must have exactly one primary span"
            )
        raw_path = primary_spans[0].get("file_name")
        if type(raw_path) is not str or not raw_path:
            raise RustLintDebtError(f"{LINT} diagnostic has no source path")
        source = Path(raw_path)
        if source.is_absolute():
            try:
                source = source.relative_to(root)
            except ValueError as error:
                raise RustLintDebtError(
                    f"{LINT} diagnostic is outside the repository: {raw_path}"
                ) from error
        normalized = PurePosixPath(source.as_posix())
        if normalized.is_absolute() or ".." in normalized.parts:
            raise RustLintDebtError(f"invalid diagnostic source path: {raw_path}")
        counts[normalized.as_posix()] += 1
    return counts


def evaluate_counts(
    baseline: RustLintBaseline, observed: Mapping[str, int]
) -> list[str]:
    violations: list[str] = []
    for path in sorted(set(baseline.files) | set(observed)):
        allowed = baseline.files.get(path, 0)
        actual = observed.get(path, 0)
        if actual > allowed:
            violations.append(f"{path}: observed {actual}, allowed {allowed}")
    if sum(observed.values()) > baseline.total:
        violations.append(
            f"total: observed {sum(observed.values())}, allowed {baseline.total}"
        )
    return violations


def build_report(
    baseline: RustLintBaseline,
    observed: Mapping[str, int],
    violations: Sequence[str],
) -> dict[str, object]:
    paths = sorted(set(baseline.files) | set(observed))
    return {
        "schemaVersion": 1,
        "lint": LINT,
        "toolchain": baseline.toolchain,
        "scope": list(PRODUCTION_SCOPE),
        "baselineTotal": baseline.total,
        "observedTotal": sum(observed.values()),
        "files": {
            path: {
                "baseline": baseline.files.get(path, 0),
                "observed": observed.get(path, 0),
            }
            for path in paths
        },
        "violations": list(violations),
        "passed": not violations,
    }


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    try:
        baseline = load_baseline(args.baseline)
        verify_policy(baseline)
        completed = subprocess.run(
            clippy_command(),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        if completed.returncode != 0:
            raise RustLintDebtError(f"Clippy exited with status {completed.returncode}")
        observed = parse_clippy_messages(completed.stdout.splitlines())
        violations = evaluate_counts(baseline, observed)
        report = build_report(baseline, observed, violations)
        if args.report is not None:
            _write_report(args.report, report)
        if violations:
            raise RustLintDebtError(
                "production expect debt increased:\n- " + "\n- ".join(violations)
            )
    except (OSError, RustLintDebtError) as error:
        print(f"Rust lint debt check failed: {error}", file=sys.stderr)
        return 1

    print(
        f"Rust lint debt check passed: {sum(observed.values())}/"
        f"{baseline.total} production expect calls"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
