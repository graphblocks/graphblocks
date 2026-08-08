#!/usr/bin/env python3
"""Verify evidence-bound risk status and reopen resolved risks on drift."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "docs" / "project" / "risk-status.json"
ROOT_FIELDS = frozenset({"statusVersion", "authority", "legacyNarrative", "risks"})
RISK_FIELDS = frozenset(
    {
        "id",
        "title",
        "severity",
        "status",
        "owner",
        "openedAt",
        "resolvedAt",
        "resolvedCommit",
        "resolvedVersion",
        "legacyHeading",
        "resolution",
        "sourceEvidence",
        "tckEvidence",
        "reopenOn",
    }
)
SOURCE_FIELDS = frozenset({"path", "sha256"})
TCK_FIELDS = frozenset(
    {
        "path",
        "mirrorPath",
        "fileSha256",
        "caseId",
        "caseSha256",
        "semanticAssertions",
    }
)
SEMANTIC_FIELDS = frozenset(
    {
        "accumulationMode",
        "beforePaneRevision",
        "beforePaneIsFinal",
        "beforePaneOffsets",
        "paneRevision",
        "paneIsFinal",
        "paneOffsets",
        "lateError",
    }
)
REOPEN_REASONS = (
    "missing-evidence",
    "source-digest-change",
    "tck-digest-change",
    "tck-mirror-drift",
    "semantic-assertion-failure",
    "resolved-commit-not-ancestor",
)
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class RiskStatusError(ValueError):
    """Raised when the authoritative risk-status document is invalid."""


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RiskStatusError(f"duplicate JSON field: {key!r}")
        result[key] = value
    return result


def _exact_object(
    value: object,
    *,
    path: str,
    fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise RiskStatusError(f"{path}: expected object")
    mapping = value
    actual = frozenset(mapping)
    if actual != fields:
        raise RiskStatusError(
            f"{path}: fields do not match schema; "
            f"missing={sorted(fields - actual)}, unknown={sorted(actual - fields)}"
        )
    return mapping


def _stable_string(value: object, *, path: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise RiskStatusError(f"{path}: expected stable non-empty string")
    return value


def _relative_path(value: object, *, path: str) -> str:
    raw = _stable_string(value, path=path)
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise RiskStatusError(f"{path}: invalid repository-relative path")
    return raw


def _digest(value: object, *, path: str) -> str:
    digest = _stable_string(value, path=path)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise RiskStatusError(f"{path}: expected lowercase sha256 digest")
    return digest


def _integer_list(value: object, *, path: str) -> list[int]:
    if type(value) is not list or not value:
        raise RiskStatusError(f"{path}: expected non-empty integer array")
    if any(type(item) is not int or item < 0 for item in value):
        raise RiskStatusError(f"{path}: expected non-negative integers")
    return value


def parse_status(value: object) -> dict[str, Any]:
    payload = _exact_object(value, path="$", fields=ROOT_FIELDS)
    if payload["statusVersion"] != 1:
        raise RiskStatusError("$.statusVersion: expected 1")
    if payload["authority"] != "evidence-bound-risk-status":
        raise RiskStatusError("$.authority: unexpected authority")
    legacy = _relative_path(payload["legacyNarrative"], path="$.legacyNarrative")
    raw_risks = payload["risks"]
    if type(raw_risks) is not list or not raw_risks:
        raise RiskStatusError("$.risks: expected non-empty array")
    risks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_risk in enumerate(raw_risks):
        risk_path = f"$.risks[{index}]"
        risk = _exact_object(raw_risk, path=risk_path, fields=RISK_FIELDS)
        risk_id = _stable_string(risk["id"], path=f"{risk_path}.id")
        if risk_id in seen_ids:
            raise RiskStatusError(f"{risk_path}.id: duplicate risk id")
        seen_ids.add(risk_id)
        for name in ("title", "owner", "legacyHeading", "resolution"):
            _stable_string(risk[name], path=f"{risk_path}.{name}")
        if risk["severity"] not in {"low", "medium", "high", "critical"}:
            raise RiskStatusError(f"{risk_path}.severity: invalid severity")
        if risk["status"] != "resolved":
            raise RiskStatusError(f"{risk_path}.status: expected resolved")
        for name in ("openedAt", "resolvedAt"):
            date = _stable_string(risk[name], path=f"{risk_path}.{name}")
            if DATE_PATTERN.fullmatch(date) is None:
                raise RiskStatusError(f"{risk_path}.{name}: expected YYYY-MM-DD")
        commit = _stable_string(
            risk["resolvedCommit"], path=f"{risk_path}.resolvedCommit"
        )
        if COMMIT_PATTERN.fullmatch(commit) is None:
            raise RiskStatusError(
                f"{risk_path}.resolvedCommit: expected full lowercase commit"
            )
        _stable_string(risk["resolvedVersion"], path=f"{risk_path}.resolvedVersion")
        raw_sources = risk["sourceEvidence"]
        if type(raw_sources) is not list or len(raw_sources) < 2:
            raise RiskStatusError(
                f"{risk_path}.sourceEvidence: expected at least two implementations"
            )
        sources: list[dict[str, str]] = []
        source_paths: set[str] = set()
        for source_index, raw_source in enumerate(raw_sources):
            source_path = f"{risk_path}.sourceEvidence[{source_index}]"
            source = _exact_object(raw_source, path=source_path, fields=SOURCE_FIELDS)
            relative = _relative_path(source["path"], path=f"{source_path}.path")
            if relative in source_paths:
                raise RiskStatusError(f"{source_path}.path: duplicate evidence")
            source_paths.add(relative)
            sources.append(
                {
                    "path": relative,
                    "sha256": _digest(source["sha256"], path=f"{source_path}.sha256"),
                }
            )
        tck = _exact_object(
            risk["tckEvidence"], path=f"{risk_path}.tckEvidence", fields=TCK_FIELDS
        )
        semantic = _exact_object(
            tck["semanticAssertions"],
            path=f"{risk_path}.tckEvidence.semanticAssertions",
            fields=SEMANTIC_FIELDS,
        )
        if semantic["accumulationMode"] != "accumulating":
            raise RiskStatusError(f"{risk_path}: expected accumulating mode")
        for name in ("beforePaneRevision", "paneRevision"):
            if type(semantic[name]) is not int or semantic[name] < 0:
                raise RiskStatusError(
                    f"{risk_path}.{name}: expected non-negative integer"
                )
        for name in ("beforePaneIsFinal", "paneIsFinal"):
            if type(semantic[name]) is not bool:
                raise RiskStatusError(f"{risk_path}.{name}: expected boolean")
        before_offsets = _integer_list(
            semantic["beforePaneOffsets"], path=f"{risk_path}.beforePaneOffsets"
        )
        pane_offsets = _integer_list(
            semantic["paneOffsets"], path=f"{risk_path}.paneOffsets"
        )
        late_error = _stable_string(
            semantic["lateError"], path=f"{risk_path}.lateError"
        )
        reopen_on = risk["reopenOn"]
        if reopen_on != list(REOPEN_REASONS):
            raise RiskStatusError(
                f"{risk_path}.reopenOn: expected exact automatic-reopen policy"
            )
        risks.append(
            {
                **risk,
                "sourceEvidence": sources,
                "tckEvidence": {
                    "path": _relative_path(
                        tck["path"], path=f"{risk_path}.tckEvidence.path"
                    ),
                    "mirrorPath": _relative_path(
                        tck["mirrorPath"],
                        path=f"{risk_path}.tckEvidence.mirrorPath",
                    ),
                    "fileSha256": _digest(
                        tck["fileSha256"],
                        path=f"{risk_path}.tckEvidence.fileSha256",
                    ),
                    "caseId": _stable_string(
                        tck["caseId"], path=f"{risk_path}.tckEvidence.caseId"
                    ),
                    "caseSha256": _digest(
                        tck["caseSha256"],
                        path=f"{risk_path}.tckEvidence.caseSha256",
                    ),
                    "semanticAssertions": {
                        **semantic,
                        "beforePaneOffsets": before_offsets,
                        "paneOffsets": pane_offsets,
                        "lateError": late_error,
                    },
                },
            }
        )
    return {
        "statusVersion": 1,
        "authority": payload["authority"],
        "legacyNarrative": legacy,
        "risks": risks,
    }


def load_status(
    path: Path = STATUS_PATH,
) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, json.JSONDecodeError, RiskStatusError) as error:
        raise RiskStatusError(f"could not load {path}: {error}") from error
    return parse_status(value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_digest(case: Mapping[str, object]) -> str:
    encoded = json.dumps(
        case,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_cases(path: Path) -> list[dict[str, object]]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, json.JSONDecodeError, RiskStatusError) as error:
        raise RiskStatusError(f"could not load TCK evidence {path}: {error}") from error
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise RiskStatusError(f"TCK evidence {path} must be an array of objects")
    return value


def _commit_is_ancestor(commit: str, *, root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        return False
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    return ancestor.returncode == 0


def check_risk_status(
    status: Mapping[str, Any],
    *,
    root: Path = ROOT,
    verify_git: bool = True,
) -> dict[str, object]:
    violations: list[str] = []
    checked: list[dict[str, object]] = []
    legacy_path = root / status["legacyNarrative"]
    if not legacy_path.is_file():
        violations.append(f"{status['legacyNarrative']}: missing legacy narrative")
    for raw_risk in status["risks"]:
        risk = raw_risk
        risk_id = risk["id"]
        risk_violations: list[str] = []
        for source in risk["sourceEvidence"]:
            path = root / source["path"]
            if not path.is_file():
                risk_violations.append(f"{source['path']}: missing evidence")
            elif _sha256(path) != source["sha256"]:
                risk_violations.append(f"{source['path']}: source digest changed")
        tck = risk["tckEvidence"]
        tck_path = root / tck["path"]
        mirror_path = root / tck["mirrorPath"]
        if not tck_path.is_file() or not mirror_path.is_file():
            risk_violations.append("durable TCK or mirror evidence is missing")
        else:
            tck_bytes = tck_path.read_bytes()
            mirror_bytes = mirror_path.read_bytes()
            if hashlib.sha256(tck_bytes).hexdigest() != tck["fileSha256"]:
                risk_violations.append("durable TCK file digest changed")
            if mirror_bytes != tck_bytes:
                risk_violations.append("durable TCK mirror drifted")
            try:
                cases = _load_cases(tck_path)
            except RiskStatusError as error:
                risk_violations.append(f"durable TCK evidence is invalid: {error}")
            else:
                selected = [case for case in cases if case.get("name") == tck["caseId"]]
                if len(selected) != 1:
                    risk_violations.append("resolved TCK case is missing or duplicated")
                else:
                    case = selected[0]
                    if _case_digest(case) != tck["caseSha256"]:
                        risk_violations.append("resolved TCK case digest changed")
                    policy = case.get("policy")
                    expected = case.get("expected")
                    semantics = tck["semanticAssertions"]
                    if type(policy) is not dict or type(expected) is not dict:
                        risk_violations.append(
                            "resolved TCK case lost policy or expected data"
                        )
                    else:
                        observed = {
                            "accumulationMode": policy.get("accumulationMode"),
                            **{
                                key: expected.get(key)
                                for key in SEMANTIC_FIELDS
                                if key != "accumulationMode"
                            },
                        }
                        if observed != semantics:
                            risk_violations.append("resolved TCK semantics changed")
        if verify_git and not _commit_is_ancestor(risk["resolvedCommit"], root=root):
            risk_violations.append("resolved commit is not an ancestor of HEAD")
        violations.extend(f"{risk_id}: {item}" for item in risk_violations)
        checked.append(
            {
                "id": risk_id,
                "declaredStatus": risk["status"],
                "effectiveStatus": "reopened" if risk_violations else "resolved",
                "violations": risk_violations,
            }
        )
    return {
        "statusVersion": 1,
        "authority": status["authority"],
        "checkedRisks": checked,
        "resolved": sum(item["effectiveStatus"] == "resolved" for item in checked),
        "reopened": sum(item["effectiveStatus"] == "reopened" for item in checked),
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
        status = load_status()
        report = check_risk_status(status)
        if args.report is not None:
            _write_report(args.report, report)
    except RiskStatusError as error:
        print(f"risk status failed: {error}", file=sys.stderr)
        return 1
    if not report["passed"]:
        for violation in report["violations"]:
            print(violation, file=sys.stderr)
        return 1
    print(
        "risk status passed: "
        f"resolved={report['resolved']}, reopened={report['reopened']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
