#!/usr/bin/env python3
"""Verify reviewed broad-exception and production-assert boundaries."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "compatibility" / "python-boundary-safety.json"
SOURCE_ROOT = PurePosixPath("src/graphblocks")
OPTIMIZED_TESTS = (
    "tests/test_server_lifecycle.py",
    "tests/test_server_error_contract.py",
    "tests/test_client_package.py",
    "tests/test_durable_server_http.py",
    "tests/test_mcp_schema_execution.py",
)
BROAD_CLASSIFICATIONS = frozenset(
    {
        "best-effort-cleanup",
        "external-callback-boundary",
        "input-normalization-boundary",
        "optional-dependency-boundary",
        "persistence-transaction-boundary",
        "process-isolation-boundary",
    }
)
ASSERT_CLASSIFICATIONS = frozenset(
    {
        "exhaustive-branch-invariant",
        "internal-state-invariant",
        "validated-type-narrowing",
    }
)
ROOT_FIELDS = frozenset(
    {
        "schemaVersion",
        "scope",
        "policy",
        "broadExceptionHandlers",
        "productionAsserts",
        "optimizedMode",
    }
)
GROUP_FIELDS = frozenset({"total", "files"})
ENTRY_FIELDS = frozenset(
    {"path", "count", "semanticDigest", "classifications", "rationale"}
)
POLICY_FIELDS = frozenset({"broadExceptionHandlers", "productionAsserts"})
OPTIMIZED_FIELDS = frozenset({"pythonFlag", "tests"})


class PythonBoundarySafetyError(ValueError):
    """Raised when the reviewed Python boundary contract is invalid."""


@dataclass(frozen=True)
class ReviewedFile:
    path: str
    count: int
    semantic_digest: str
    classifications: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class BoundaryBaseline:
    broad_handlers: Mapping[str, ReviewedFile]
    production_asserts: Mapping[str, ReviewedFile]


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PythonBoundarySafetyError(f"duplicate JSON field: {key!r}")
        result[key] = value
    return result


def _exact_object(
    value: object,
    *,
    path: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise PythonBoundarySafetyError(f"{path}: expected object")
    mapping = value
    actual = frozenset(mapping)
    if actual != fields:
        raise PythonBoundarySafetyError(
            f"{path}: fields do not match schema; "
            f"missing={sorted(fields - actual)}, unknown={sorted(actual - fields)}"
        )
    return mapping


def _non_empty_string(value: object, *, path: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise PythonBoundarySafetyError(f"{path}: expected stable non-empty string")
    return value


def _source_path(value: object, *, path: str, root: Path) -> str:
    raw = _non_empty_string(value, path=path)
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or tuple(candidate.parts[:2]) != tuple(SOURCE_ROOT.parts)
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.suffix != ".py"
    ):
        raise PythonBoundarySafetyError(f"{path}: invalid production Python path")
    if not (root / raw).is_file():
        raise PythonBoundarySafetyError(f"{path}: source file does not exist")
    return raw


def _parse_group(
    value: object,
    *,
    path: str,
    allowed_classifications: frozenset[str],
    root: Path,
) -> dict[str, ReviewedFile]:
    payload = _exact_object(value, path=path, fields=GROUP_FIELDS)
    raw_files = payload["files"]
    if type(raw_files) is not list or not raw_files:
        raise PythonBoundarySafetyError(f"{path}.files: expected non-empty array")
    files: dict[str, ReviewedFile] = {}
    for index, raw_entry in enumerate(raw_files):
        entry_path = f"{path}.files[{index}]"
        entry = _exact_object(raw_entry, path=entry_path, fields=ENTRY_FIELDS)
        source_path = _source_path(entry["path"], path=f"{entry_path}.path", root=root)
        if source_path in files:
            raise PythonBoundarySafetyError(f"{entry_path}.path: duplicate source path")
        count = entry["count"]
        if type(count) is not int or count <= 0:
            raise PythonBoundarySafetyError(
                f"{entry_path}.count: expected positive integer"
            )
        digest = _non_empty_string(
            entry["semanticDigest"], path=f"{entry_path}.semanticDigest"
        )
        if len(digest) != 71 or not digest.startswith("sha256:"):
            raise PythonBoundarySafetyError(
                f"{entry_path}.semanticDigest: expected sha256 digest"
            )
        raw_classifications = entry["classifications"]
        if type(raw_classifications) is not list or not raw_classifications:
            raise PythonBoundarySafetyError(
                f"{entry_path}.classifications: expected non-empty array"
            )
        classifications = tuple(
            _non_empty_string(item, path=f"{entry_path}.classifications")
            for item in raw_classifications
        )
        if len(set(classifications)) != len(classifications):
            raise PythonBoundarySafetyError(
                f"{entry_path}.classifications: duplicate classification"
            )
        unknown = set(classifications) - allowed_classifications
        if unknown:
            raise PythonBoundarySafetyError(
                f"{entry_path}.classifications: unknown={sorted(unknown)}"
            )
        rationale = _non_empty_string(
            entry["rationale"], path=f"{entry_path}.rationale"
        )
        files[source_path] = ReviewedFile(
            path=source_path,
            count=count,
            semantic_digest=digest,
            classifications=classifications,
            rationale=rationale,
        )
    declared_total = payload["total"]
    actual_total = sum(item.count for item in files.values())
    if type(declared_total) is not int or declared_total != actual_total:
        raise PythonBoundarySafetyError(
            f"{path}.total: expected exact file sum {actual_total}, got {declared_total!r}"
        )
    return dict(sorted(files.items()))


def parse_baseline(value: object, *, root: Path = ROOT) -> BoundaryBaseline:
    payload = _exact_object(value, path="$", fields=ROOT_FIELDS)
    if payload["schemaVersion"] != 1:
        raise PythonBoundarySafetyError("$.schemaVersion: expected 1")
    if payload["scope"] != "src/graphblocks/**/*.py":
        raise PythonBoundarySafetyError("$.scope: unexpected production scope")
    policy = _exact_object(payload["policy"], path="$.policy", fields=POLICY_FIELDS)
    for name in sorted(POLICY_FIELDS):
        _non_empty_string(policy[name], path=f"$.policy.{name}")
    optimized = _exact_object(
        payload["optimizedMode"], path="$.optimizedMode", fields=OPTIMIZED_FIELDS
    )
    if optimized["pythonFlag"] != "-O":
        raise PythonBoundarySafetyError("$.optimizedMode.pythonFlag: expected '-O'")
    if optimized["tests"] != list(OPTIMIZED_TESTS):
        raise PythonBoundarySafetyError(
            "$.optimizedMode.tests: expected the exact reviewed boundary shard"
        )
    broad = _parse_group(
        payload["broadExceptionHandlers"],
        path="$.broadExceptionHandlers",
        allowed_classifications=BROAD_CLASSIFICATIONS,
        root=root,
    )
    asserts = _parse_group(
        payload["productionAsserts"],
        path="$.productionAsserts",
        allowed_classifications=ASSERT_CLASSIFICATIONS,
        root=root,
    )
    return BoundaryBaseline(broad_handlers=broad, production_asserts=asserts)


def load_baseline(
    path: Path = BASELINE_PATH,
    *,
    root: Path = ROOT,
) -> BoundaryBaseline:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, json.JSONDecodeError, PythonBoundarySafetyError) as error:
        raise PythonBoundarySafetyError(f"could not load {path}: {error}") from error
    return parse_baseline(value, root=root)


def _broad_exception_type(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"Exception", "BaseException"}
    if isinstance(node, ast.Tuple):
        return any(_broad_exception_type(item) for item in node.elts)
    return False


def _semantic_digest(nodes: Sequence[ast.AST]) -> str:
    payload = "\0".join(
        ast.dump(node, annotate_fields=True, include_attributes=False) for node in nodes
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def collect_inventory(
    *,
    root: Path = ROOT,
) -> tuple[dict[str, tuple[int, str]], dict[str, tuple[int, str]]]:
    broad: dict[str, tuple[int, str]] = {}
    asserts: dict[str, tuple[int, str]] = {}
    source_root = root / SOURCE_ROOT
    for source in sorted(source_root.rglob("*.py")):
        relative = source.relative_to(root).as_posix()
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError) as error:
            raise PythonBoundarySafetyError(
                f"could not parse {relative}: {error}"
            ) from error
        broad_nodes = sorted(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler)
                and _broad_exception_type(node.type)
            ),
            key=lambda node: (node.lineno, node.col_offset),
        )
        assert_nodes = sorted(
            (node for node in ast.walk(tree) if isinstance(node, ast.Assert)),
            key=lambda node: (node.lineno, node.col_offset),
        )
        if broad_nodes:
            broad[relative] = (len(broad_nodes), _semantic_digest(broad_nodes))
        if assert_nodes:
            asserts[relative] = (len(assert_nodes), _semantic_digest(assert_nodes))
    return broad, asserts


def _evaluate_group(
    reviewed: Mapping[str, ReviewedFile],
    observed: Mapping[str, tuple[int, str]],
    *,
    label: str,
) -> list[str]:
    violations: list[str] = []
    reviewed_paths = set(reviewed)
    observed_paths = set(observed)
    for path in sorted(observed_paths - reviewed_paths):
        violations.append(f"{label}: unreviewed source {path}")
    for path in sorted(reviewed_paths - observed_paths):
        violations.append(f"{label}: stale reviewed source {path}")
    for path in sorted(reviewed_paths & observed_paths):
        item = reviewed[path]
        count, digest = observed[path]
        if count != item.count:
            violations.append(
                f"{label}: {path} observed count {count}, reviewed {item.count}"
            )
        if digest != item.semantic_digest:
            violations.append(f"{label}: {path} semantic fingerprint changed")
    return violations


def check_boundary_safety(
    baseline: BoundaryBaseline,
    *,
    root: Path = ROOT,
) -> dict[str, object]:
    broad, asserts = collect_inventory(root=root)
    violations = _evaluate_group(
        baseline.broad_handlers, broad, label="broad-exception-handler"
    )
    violations.extend(
        _evaluate_group(baseline.production_asserts, asserts, label="production-assert")
    )
    return {
        "schemaVersion": 1,
        "broadExceptionHandlers": {
            "reviewed": sum(item.count for item in baseline.broad_handlers.values()),
            "observed": sum(count for count, _digest in broad.values()),
            "files": len(broad),
        },
        "productionAsserts": {
            "reviewed": sum(
                item.count for item in baseline.production_asserts.values()
            ),
            "observed": sum(count for count, _digest in asserts.values()),
            "files": len(asserts),
        },
        "optimizedModeTests": list(OPTIMIZED_TESTS),
        "violations": violations,
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
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline()
        report = check_boundary_safety(baseline)
        if args.report is not None:
            _write_report(args.report, report)
    except PythonBoundarySafetyError as error:
        print(f"python boundary safety failed: {error}", file=sys.stderr)
        return 1
    if not report["passed"]:
        for violation in report["violations"]:
            print(violation, file=sys.stderr)
        return 1
    broad = report["broadExceptionHandlers"]
    asserts = report["productionAsserts"]
    print(
        "python boundary safety passed: "
        f"broad={broad['observed']}, asserts={asserts['observed']}, "
        f"optimized-tests={len(OPTIMIZED_TESTS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
