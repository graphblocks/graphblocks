#!/usr/bin/env python3
"""Validate and optionally execute the evidence-bound audit reproductions."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "reproductions/audit-reproduction-manifest.yaml"
STATUS_PATH = ROOT / "docs/project/audit-issue-status.yaml"
MAP_PATH = ROOT / "docs/project/audit-remediation-map.yaml"
SHA256 = re.compile(r"[0-9a-f]{64}")
SELECTOR = re.compile(r"tests/[A-Za-z0-9_./-]+\.py::test_[A-Za-z0-9_\[\]-]+")
REPRODUCED_IDS = frozenset(
    {
        "GB-POL-001",
        "GB-SEC-001",
        "GB-SEC-002",
        "GB-SEC-003",
        "GB-INP-001",
        "GB-INP-004",
        "GB-INP-005",
        "GB-PERF-002",
        "GB-SEC-007",
    }
)
HARNESS_STATUSES = frozenset(
    {"original-captured", "shared-original", "reconstructed-from-captured-output"}
)


class AuditReproductionError(ValueError):
    """Raised when reproduction evidence is incomplete, substituted, or fails."""


def _mapping(value: object, fields: set[str], owner: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise AuditReproductionError(f"{owner} must contain exactly {sorted(fields)!r}")
    return value


def _text(value: object, owner: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise AuditReproductionError(f"{owner} must be exact non-empty text")
    return value


def _path(value: object, owner: str) -> tuple[str, Path]:
    text = _text(value, owner)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise AuditReproductionError(f"{owner} must be repository-relative")
    resolved = (ROOT / relative).resolve()
    if ROOT not in resolved.parents:
        raise AuditReproductionError(f"{owner} escapes the repository")
    return text, resolved


def _yaml(path: Path, owner: str) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise AuditReproductionError(f"{owner} cannot be loaded") from error


def _execute(command: list[str], *, timeout: int, owner: str) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AuditReproductionError(f"{owner} could not complete") from error
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr)[-2_000:]
        raise AuditReproductionError(f"{owner} failed: {output}")


def check_audit_reproductions(
    *,
    manifest_path: Path = MANIFEST_PATH,
    execute: bool = False,
) -> dict[str, object]:
    manifest = _mapping(
        _yaml(manifest_path, "audit reproduction manifest"),
        {
            "formatVersion",
            "auditDate",
            "artifactBinding",
            "auditedSource",
            "execution",
            "capturedFiles",
            "reconstructedFiles",
            "findings",
        },
        "audit reproduction manifest",
    )
    if manifest["formatVersion"] != 1 or manifest["auditDate"] != "2026-07-27":
        raise AuditReproductionError("audit reproduction manifest version/date is invalid")

    remediation_map = _yaml(MAP_PATH, "audit remediation map")
    if type(remediation_map) is not dict:
        raise AuditReproductionError("audit remediation map must be an object")
    if (
        remediation_map.get("reproductionManifest")
        != "reproductions/audit-reproduction-manifest.yaml"
        or remediation_map.get("reproductionChecker")
        != "tools/check_audit_reproductions.py"
    ):
        raise AuditReproductionError("audit remediation reproduction authority drifted")
    artifact_binding = _mapping(
        manifest["artifactBinding"],
        {"reportSha256", "inventorySha256", "evidenceBundleSha256"},
        "audit reproduction artifact binding",
    )
    if artifact_binding != remediation_map.get("artifactDigests"):
        raise AuditReproductionError("audit reproduction artifact binding drifted")

    audited_source = _mapping(
        manifest["auditedSource"],
        {"status", "description", "gitRevision", "archiveDigest", "limitation"},
        "audit reproduction source identity",
    )
    if (
        audited_source["status"] != "unavailable"
        or audited_source["gitRevision"] is not None
        or audited_source["archiveDigest"] is not None
    ):
        raise AuditReproductionError("unknown audited source identity must stay explicit")
    _text(audited_source["description"], "audit source description")
    _text(audited_source["limitation"], "audit source limitation")

    execution = _mapping(
        manifest["execution"],
        {"supportedPython", "runner", "timeoutSeconds"},
        "audit reproduction execution",
    )
    if execution["supportedPython"] != ["3.11", "3.12"]:
        raise AuditReproductionError("audit reproduction Python support is invalid")
    runner, runner_path = _path(execution["runner"], "audit reproduction runner")
    if runner != "tools/check_audit_reproductions.py" or not runner_path.is_file():
        raise AuditReproductionError("audit reproduction runner is missing")
    timeout = execution["timeoutSeconds"]
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise AuditReproductionError("audit reproduction timeout is invalid")

    captured = manifest["capturedFiles"]
    if type(captured) is not list or not captured:
        raise AuditReproductionError("audit captured file inventory is missing")
    captured_paths: set[str] = set()
    for index, raw_record in enumerate(captured):
        record = _mapping(
            raw_record,
            {"path", "sha256", "size"},
            f"audit captured file {index}",
        )
        relative, file_path = _path(record["path"], f"audit captured file {index} path")
        digest = _text(record["sha256"], f"audit captured file {relative} sha256")
        size = record["size"]
        if relative in captured_paths or SHA256.fullmatch(digest) is None:
            raise AuditReproductionError("audit captured file identity is invalid")
        try:
            data = file_path.read_bytes()
        except OSError as error:
            raise AuditReproductionError(f"audit captured file {relative} is missing") from error
        if type(size) is not int or size != len(data) or hashlib.sha256(data).hexdigest() != digest:
            raise AuditReproductionError(f"audit captured file {relative} was substituted")
        captured_paths.add(relative)

    reconstructed = manifest["reconstructedFiles"]
    if type(reconstructed) is not list or len(reconstructed) != 5:
        raise AuditReproductionError("audit reconstructed harness inventory is incomplete")
    reconstructed_paths: set[str] = set()
    reconstructed_sources: set[str] = set()
    for index, raw_record in enumerate(reconstructed):
        record = _mapping(
            raw_record,
            {"path", "source", "sha256", "size"},
            f"audit reconstructed harness {index}",
        )
        relative, file_path = _path(
            record["path"], f"audit reconstructed harness {index} path"
        )
        source = _text(record["source"], f"audit reconstructed harness {relative} source")
        digest = _text(
            record["sha256"], f"audit reconstructed harness {relative} sha256"
        )
        size = record["size"]
        try:
            data = file_path.read_bytes()
        except OSError as error:
            raise AuditReproductionError(
                f"audit reconstructed harness {relative} is missing"
            ) from error
        if (
            relative in reconstructed_paths
            or source in reconstructed_sources
            or source not in captured_paths
            or SHA256.fullmatch(digest) is None
            or type(size) is not int
            or size != len(data)
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise AuditReproductionError("audit reconstructed harness identity is invalid")
        reconstructed_paths.add(relative)
        reconstructed_sources.add(source)

    status = _mapping(
        _yaml(STATUS_PATH, "audit issue status"),
        {"statusVersion", "inventory", "defaultStatus", "resolved"},
        "audit issue status",
    )
    resolved = status["resolved"]
    if type(resolved) is not list:
        raise AuditReproductionError("audit resolved status is invalid")
    fixes_by_id = {
        entry["id"]: entry["fixCommits"]
        for entry in resolved
        if type(entry) is dict and type(entry.get("id")) is str
    }
    mapped_reproductions = remediation_map.get("reproducedFindings")
    if type(mapped_reproductions) is not list:
        raise AuditReproductionError("audit remediation reproduction map is missing")
    mapped_ids = {entry.get("id") for entry in mapped_reproductions if type(entry) is dict}
    if mapped_ids != REPRODUCED_IDS:
        raise AuditReproductionError("audit remediation reproduction IDs drifted")
    mapped_by_id = {
        entry["id"]: entry for entry in mapped_reproductions if type(entry) is dict
    }

    findings = manifest["findings"]
    if type(findings) is not list or len(findings) != len(REPRODUCED_IDS):
        raise AuditReproductionError("audit reproduction finding inventory is incomplete")
    finding_ids: set[str] = set()
    referenced_evidence: set[str] = set()
    selectors: set[str] = set()
    executable_harnesses: set[str] = set()
    for index, raw_finding in enumerate(findings):
        finding = _mapping(
            raw_finding,
            {"id", "evidence", "harness", "fixCommits", "currentSelectors"},
            f"audit reproduction finding {index}",
        )
        finding_id = _text(finding["id"], f"audit reproduction finding {index} id")
        if finding_id not in REPRODUCED_IDS or finding_id in finding_ids:
            raise AuditReproductionError("audit reproduction finding identity is invalid")
        if finding["fixCommits"] != fixes_by_id.get(finding_id):
            raise AuditReproductionError(f"audit reproduction {finding_id} fix commits drifted")
        evidence = finding["evidence"]
        if type(evidence) is not list or not evidence or any(path not in captured_paths for path in evidence):
            raise AuditReproductionError(f"audit reproduction {finding_id} evidence is invalid")
        referenced_evidence.update(evidence)

        harness = _mapping(
            finding["harness"],
            {"status", "path", "sharedWith"},
            f"audit reproduction {finding_id} harness",
        )
        harness_status = _text(harness["status"], f"audit reproduction {finding_id} harness status")
        harness_path, resolved_harness = _path(
            harness["path"], f"audit reproduction {finding_id} harness path"
        )
        if harness_status not in HARNESS_STATUSES or not resolved_harness.is_file():
            raise AuditReproductionError(f"audit reproduction {finding_id} harness is invalid")
        if harness_status == "reconstructed-from-captured-output":
            if harness_path not in reconstructed_paths or harness["sharedWith"] is not None:
                raise AuditReproductionError(f"audit reproduction {finding_id} reconstruction is invalid")
            executable_harnesses.add(harness_path)
        elif harness_status == "shared-original":
            if harness["sharedWith"] not in REPRODUCED_IDS:
                raise AuditReproductionError(f"audit reproduction {finding_id} sharing is invalid")
        elif harness["sharedWith"] is not None or harness_path not in captured_paths:
            raise AuditReproductionError(f"audit reproduction {finding_id} original harness is invalid")

        raw_selectors = finding["currentSelectors"]
        if type(raw_selectors) is not list or not raw_selectors:
            raise AuditReproductionError(f"audit reproduction {finding_id} selectors are missing")
        for raw_selector in raw_selectors:
            selector = _text(raw_selector, f"audit reproduction {finding_id} selector")
            if SELECTOR.fullmatch(selector) is None:
                raise AuditReproductionError(f"audit reproduction {finding_id} selector is invalid")
            test_path = ROOT / selector.split("::", 1)[0]
            if not test_path.is_file():
                raise AuditReproductionError(f"audit reproduction {finding_id} selector is missing")
            selectors.add(selector)
        mapped_harness = (
            f"shared-original-with-{harness['sharedWith']}"
            if harness_status == "shared-original"
            else harness_status
        )
        if mapped_by_id[finding_id] != {
            "id": finding_id,
            "harness": mapped_harness,
            "evidence": evidence,
            "currentVerification": raw_selectors[0],
        } or len(raw_selectors) != 1:
            raise AuditReproductionError(
                f"audit reproduction {finding_id} remediation mapping drifted"
            )
        finding_ids.add(finding_id)

    if finding_ids != REPRODUCED_IDS or referenced_evidence != captured_paths:
        raise AuditReproductionError("audit reproduction coverage is incomplete")
    if executable_harnesses != reconstructed_paths:
        raise AuditReproductionError("audit reconstructed harness coverage is incomplete")

    if execute:
        for harness_path in sorted(executable_harnesses):
            _execute(
                [sys.executable, harness_path],
                timeout=timeout,
                owner=f"audit reconstructed harness {harness_path}",
            )
        _execute(
            [sys.executable, "-m", "pytest", "-q", *sorted(selectors)],
            timeout=timeout,
            owner="audit current regression selectors",
        )
    return {
        "findings": len(finding_ids),
        "capturedFiles": len(captured_paths),
        "reconstructedHarnesses": len(reconstructed_paths),
        "currentSelectors": len(selectors),
        "executed": execute,
        "auditedSourceIdentity": "unavailable",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check_audit_reproductions(
            manifest_path=args.manifest,
            execute=args.execute,
        )
    except AuditReproductionError as error:
        print(f"audit reproduction error: {error}", file=sys.stderr)
        return 1
    print(
        "audit reproductions passed: "
        f"{result['findings']} findings, {result['capturedFiles']} captured files, "
        f"{result['reconstructedHarnesses']} reconstructed harnesses, "
        f"{result['currentSelectors']} current selectors, executed={result['executed']}, "
        "audited-source=unavailable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
