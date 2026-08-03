from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import check_audit_reproductions


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "reproductions" / "audit-reproduction-manifest.yaml"
CHECKER = ROOT / "tools" / "check_audit_reproductions.py"


def _mutated_manifest(tmp_path: Path, mutate: object) -> Path:
    payload = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(payload)
    path = tmp_path / "audit-reproduction-manifest.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_audit_reproduction_manifest_covers_all_captured_findings() -> None:
    assert check_audit_reproductions.check_audit_reproductions() == {
        "findings": 9,
        "capturedFiles": 13,
        "reconstructedHarnesses": 5,
        "currentSelectors": 9,
        "executed": False,
        "auditedSourceIdentity": "unavailable",
    }


def test_audit_reproduction_cli_reports_bound_evidence() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "9 findings, 13 captured files, 5 reconstructed harnesses" in completed.stdout
    assert "audited-source=unavailable" in completed.stdout
    assert completed.stderr == ""


def test_audit_reproduction_manifest_rejects_captured_file_substitution(
    tmp_path: Path,
) -> None:
    manifest = _mutated_manifest(
        tmp_path,
        lambda payload: payload["capturedFiles"][0].__setitem__("sha256", "0" * 64),
    )

    with pytest.raises(
        check_audit_reproductions.AuditReproductionError,
        match="was substituted",
    ):
        check_audit_reproductions.check_audit_reproductions(manifest_path=manifest)


def test_audit_reproduction_manifest_does_not_invent_source_identity(
    tmp_path: Path,
) -> None:
    manifest = _mutated_manifest(
        tmp_path,
        lambda payload: payload["auditedSource"].update(
            {"status": "identified", "gitRevision": "1" * 40}
        ),
    )

    with pytest.raises(
        check_audit_reproductions.AuditReproductionError,
        match="must stay explicit",
    ):
        check_audit_reproductions.check_audit_reproductions(manifest_path=manifest)


def test_audit_reproduction_runner_executes_harnesses_and_current_regressions() -> None:
    result = check_audit_reproductions.check_audit_reproductions(execute=True)

    assert result["executed"] is True


def test_ci_executes_the_audit_reproduction_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "python tools/check_audit_reproductions.py --execute" in workflow
