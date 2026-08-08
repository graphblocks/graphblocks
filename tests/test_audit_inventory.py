from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import check_audit_inventory


ROOT = Path(__file__).parents[1]
CHECKER = ROOT / "tools" / "check_audit_inventory.py"


def _copy_inventory_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "docs" / "project"
    project.mkdir(parents=True)
    for name in (
        "audit-issues.json",
        "audit-issue-status.yaml",
        "audit-remediation-map.yaml",
    ):
        (project / name).write_bytes((ROOT / "docs" / "project" / name).read_bytes())
    monkeypatch.setattr(
        check_audit_inventory,
        "STATUS_PATH",
        project / "audit-issue-status.yaml",
    )
    monkeypatch.setattr(
        check_audit_inventory,
        "MAP_PATH",
        project / "audit-remediation-map.yaml",
    )
    monkeypatch.setattr(check_audit_inventory, "_is_ancestor", lambda _commit: True)

    def repository_path(value: object, owner: str) -> Path:
        assert type(value) is str
        if owner == "inventory path":
            return tmp_path / value
        return ROOT / value

    monkeypatch.setattr(check_audit_inventory, "_repository_path", repository_path)


def test_audit_inventory_closes_every_release_blocking_finding() -> None:
    result = check_audit_inventory.check_audit_inventory()

    assert result == {
        "inventorySha256": (
            "9f98ebde8dc981b0eaee8ed795e04306ac67f707223cfe844e2365561db7eb44"
        ),
        "total": 99,
        "resolved": 83,
        "openBySeverity": {"P0": 0, "P1": 0, "P2": 8, "P3": 8},
    }


def test_audit_inventory_cli_reports_the_bound_counts() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "99 findings, 83 resolved" in completed.stdout
    assert "'P0': 0, 'P1': 0, 'P2': 8, 'P3': 8" in completed.stdout
    assert completed.stderr == ""


def test_audit_inventory_rejects_digest_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_inventory_contract(tmp_path, monkeypatch)
    inventory = tmp_path / "docs" / "project" / "audit-issues.json"
    inventory.write_bytes(inventory.read_bytes() + b"\n")

    with pytest.raises(
        check_audit_inventory.AuditInventoryError,
        match="digest does not match",
    ):
        check_audit_inventory.check_audit_inventory()


def test_audit_inventory_rejects_open_p0_or_p1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_inventory_contract(tmp_path, monkeypatch)
    status_path = tmp_path / "docs" / "project" / "audit-issue-status.yaml"
    status = yaml.safe_load(status_path.read_text(encoding="utf-8"))
    status["resolved"] = [
        entry for entry in status["resolved"] if entry["id"] != "GB-POL-001"
    ]
    status_path.write_text(yaml.safe_dump(status, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        check_audit_inventory.AuditInventoryError,
        match="1 open P0/P1",
    ):
        check_audit_inventory.check_audit_inventory()


def test_audit_inventory_rejects_non_ancestor_fix_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_audit_inventory, "_is_ancestor", lambda _commit: False)

    with pytest.raises(
        check_audit_inventory.AuditInventoryError,
        match="is not in HEAD",
    ):
        check_audit_inventory.check_audit_inventory()


def test_audit_inventory_rejects_duplicate_workstream_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _copy_inventory_contract(tmp_path, monkeypatch)
    map_path = tmp_path / "docs" / "project" / "audit-remediation-map.yaml"
    remediation_map = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    remediation_map["workstreams"][1]["findings"].append("GB-SEC-001")
    map_path.write_text(
        yaml.safe_dump(remediation_map, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(
        check_audit_inventory.AuditInventoryError,
        match="own every finding exactly once",
    ):
        check_audit_inventory.check_audit_inventory()


def test_ci_runs_the_audit_inventory_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "python tools/check_audit_inventory.py" in workflow
