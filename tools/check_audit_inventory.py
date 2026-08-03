#!/usr/bin/env python3
"""Validate the digest-bound deep-audit inventory and live closure overlay."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "docs/project/audit-issue-status.yaml"
MAP_PATH = ROOT / "docs/project/audit-remediation-map.yaml"
RELEASE_BLOCKING_SEVERITIES = frozenset({"P0", "P1"})
ALLOWED_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})


class AuditInventoryError(ValueError):
    """Raised when audit inventory or closure evidence is not exact."""


def _closed_mapping(value: object, fields: set[str], owner: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise AuditInventoryError(f"{owner} must contain exactly {sorted(fields)!r}")
    return value


def _exact_text(value: object, owner: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AuditInventoryError(f"{owner} must be exact non-empty text")
    return value


def _repository_path(value: object, owner: str) -> Path:
    text = _exact_text(value, owner)
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AuditInventoryError(f"{owner} must be repository-relative")
    resolved = (ROOT / candidate).resolve()
    if ROOT not in resolved.parents:
        raise AuditInventoryError(f"{owner} escapes the repository")
    return resolved


def _load_yaml(path: Path, owner: str) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise AuditInventoryError(f"{owner} cannot be loaded") from error


def _is_ancestor(commit: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def validate_audit_inventory(
    *,
    inventory_bytes: bytes,
    status_value: object,
    remediation_map_value: object,
    is_ancestor: Callable[[str], bool],
    regression_exists: Callable[[str], bool],
) -> dict[str, object]:
    """Validate supplied audit blobs without trusting the live checkout."""

    status = _closed_mapping(
        status_value,
        {"statusVersion", "inventory", "defaultStatus", "resolved"},
        "audit issue status",
    )
    if status["statusVersion"] != 1 or status["defaultStatus"] != "open":
        raise AuditInventoryError("audit issue status version/default is unsupported")
    inventory_ref = _closed_mapping(
        status["inventory"],
        {"path", "sha256"},
        "audit inventory reference",
    )
    inventory_path = _exact_text(inventory_ref["path"], "inventory path")
    candidate_inventory_path = Path(inventory_path)
    if (
        candidate_inventory_path.is_absolute()
        or ".." in candidate_inventory_path.parts
    ):
        raise AuditInventoryError("inventory path must be repository-relative")
    try:
        inventory = json.loads(inventory_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AuditInventoryError("audit inventory cannot be loaded") from error
    expected_digest = _exact_text(inventory_ref["sha256"], "inventory sha256")
    actual_digest = hashlib.sha256(inventory_bytes).hexdigest()
    if actual_digest != expected_digest:
        raise AuditInventoryError("audit inventory digest does not match its binding")
    if type(inventory) is not dict or type(inventory.get("issues")) is not list:
        raise AuditInventoryError("audit inventory must contain an issues array")
    issues: dict[str, dict[str, object]] = {}
    severity_counts: Counter[str] = Counter()
    for index, raw_issue in enumerate(inventory["issues"]):
        if type(raw_issue) is not dict:
            raise AuditInventoryError(f"audit issue {index} must be an object")
        issue_id = _exact_text(raw_issue.get("id"), f"audit issue {index} id")
        severity = _exact_text(
            raw_issue.get("severity"),
            f"audit issue {issue_id} severity",
        )
        if issue_id in issues or severity not in ALLOWED_SEVERITIES:
            raise AuditInventoryError(f"audit issue {issue_id} identity is invalid")
        issues[issue_id] = raw_issue
        severity_counts[severity] += 1
    declared_counts = inventory.get("counts")
    if (
        type(declared_counts) is not dict
        or declared_counts.get("total") != len(issues)
        or declared_counts.get("by_severity") != dict(severity_counts)
    ):
        raise AuditInventoryError("audit inventory counts do not match its issues")

    resolved_ids: set[str] = set()
    resolved = status["resolved"]
    if type(resolved) is not list:
        raise AuditInventoryError("audit resolved entries must be an array")
    for index, raw_entry in enumerate(resolved):
        entry = _closed_mapping(
            raw_entry,
            {"id", "fixCommits", "regression"},
            f"audit resolution {index}",
        )
        issue_id = _exact_text(entry["id"], f"audit resolution {index} id")
        if issue_id not in issues or issue_id in resolved_ids:
            raise AuditInventoryError(f"audit resolution {issue_id} is not unique")
        for field_name in ("fixCommits", "regression"):
            values = entry[field_name]
            if type(values) is not list or not values:
                raise AuditInventoryError(
                    f"audit resolution {issue_id} {field_name} must be non-empty"
                )
        for raw_commit in entry["fixCommits"]:
            commit = _exact_text(raw_commit, f"audit resolution {issue_id} commit")
            if not is_ancestor(commit):
                raise AuditInventoryError(
                    f"audit resolution {issue_id} commit {commit!r} is not in HEAD"
                )
        for raw_path in entry["regression"]:
            evidence_path = _exact_text(
                raw_path, f"audit resolution {issue_id} regression"
            )
            candidate_path = Path(evidence_path)
            if candidate_path.is_absolute() or ".." in candidate_path.parts:
                raise AuditInventoryError(
                    f"audit resolution {issue_id} regression must be repository-relative"
                )
            if not regression_exists(evidence_path):
                raise AuditInventoryError(
                    f"audit resolution {issue_id} regression is missing"
                )
        resolved_ids.add(issue_id)

    remediation_map = remediation_map_value
    if type(remediation_map) is not dict:
        raise AuditInventoryError("audit remediation map must be an object")
    mapped_ids: set[str] = set()
    baseline = remediation_map.get("baselineBySeverity")
    if type(baseline) is not dict:
        raise AuditInventoryError("audit remediation baseline is missing")
    for severity in ALLOWED_SEVERITIES:
        severity_ids = baseline.get(severity)
        if type(severity_ids) is not list or any(
            type(issue_id) is not str for issue_id in severity_ids
        ):
            raise AuditInventoryError(f"audit remediation {severity} IDs are invalid")
        if set(severity_ids) != {
            issue_id
            for issue_id, issue in issues.items()
            if issue["severity"] == severity
        }:
            raise AuditInventoryError(
                f"audit remediation {severity} IDs drifted from inventory"
            )
        mapped_ids.update(severity_ids)
    if mapped_ids != set(issues):
        raise AuditInventoryError("audit remediation map does not cover all issues")

    workstreams = remediation_map.get("workstreams")
    if type(workstreams) is not list:
        raise AuditInventoryError("audit remediation workstreams are missing")
    workstream_owners: Counter[str] = Counter()
    for index, raw_workstream in enumerate(workstreams):
        if type(raw_workstream) is not dict:
            raise AuditInventoryError(f"audit remediation workstream {index} is invalid")
        workstream_id = _exact_text(
            raw_workstream.get("id"),
            f"audit remediation workstream {index} id",
        )
        finding_ids = raw_workstream.get("findings")
        if type(finding_ids) is not list or any(
            type(issue_id) is not str for issue_id in finding_ids
        ):
            raise AuditInventoryError(
                f"audit remediation workstream {workstream_id} findings are invalid"
            )
        workstream_owners.update(finding_ids)
    missing_owners = set(issues).difference(workstream_owners)
    duplicate_owners = {
        issue_id for issue_id, count in workstream_owners.items() if count != 1
    }
    unknown_owners = set(workstream_owners).difference(issues)
    if missing_owners or duplicate_owners or unknown_owners:
        raise AuditInventoryError(
            "audit remediation workstreams must own every finding exactly once"
        )

    open_by_severity = {
        severity: sum(
            issue["severity"] == severity and issue_id not in resolved_ids
            for issue_id, issue in issues.items()
        )
        for severity in sorted(ALLOWED_SEVERITIES)
    }
    blocking_open = sum(
        open_by_severity[severity] for severity in RELEASE_BLOCKING_SEVERITIES
    )
    if blocking_open:
        raise AuditInventoryError(
            f"audit inventory has {blocking_open} open P0/P1 findings"
        )
    return {
        "inventorySha256": actual_digest,
        "total": len(issues),
        "resolved": len(resolved_ids),
        "openBySeverity": open_by_severity,
    }


def check_audit_inventory() -> dict[str, object]:
    status = _load_yaml(STATUS_PATH, "audit issue status")
    if type(status) is not dict or type(status.get("inventory")) is not dict:
        raise AuditInventoryError("audit issue status is invalid")
    inventory_path = _repository_path(
        status["inventory"].get("path"),
        "inventory path",
    )
    try:
        inventory_bytes = inventory_path.read_bytes()
    except OSError as error:
        raise AuditInventoryError("audit inventory cannot be loaded") from error
    return validate_audit_inventory(
        inventory_bytes=inventory_bytes,
        status_value=status,
        remediation_map_value=_load_yaml(MAP_PATH, "audit remediation map"),
        is_ancestor=_is_ancestor,
        regression_exists=lambda path: _repository_path(
            path,
            "audit regression path",
        ).is_file(),
    )


def main() -> int:
    try:
        result = check_audit_inventory()
    except AuditInventoryError as error:
        print(f"audit inventory error: {error}", file=sys.stderr)
        return 1
    print(
        "audit inventory passed: "
        f"{result['total']} findings, {result['resolved']} resolved, "
        f"open={result['openBySeverity']}, sha256:{result['inventorySha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
