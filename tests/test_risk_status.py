from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import check_risk_status


ROOT = Path(__file__).parents[1]


def _copy_evidence(tmp_path: Path) -> dict[str, object]:
    status = check_risk_status.load_status()
    relative_paths = {status["legacyNarrative"]}
    for risk in status["risks"]:
        relative_paths.update(source["path"] for source in risk["sourceEvidence"])
        tck = risk["tckEvidence"]
        relative_paths.update((tck["path"], tck["mirrorPath"]))
    for relative in relative_paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    return status


def test_checked_in_risk_status_is_resolved_and_evidence_bound() -> None:
    report = check_risk_status.check_risk_status(check_risk_status.load_status())

    assert report["passed"] is True
    assert report["resolved"] == 1
    assert report["reopened"] == 0
    assert report["checkedRisks"] == [
        {
            "id": "ARCH-001",
            "declaredStatus": "resolved",
            "effectiveStatus": "resolved",
            "violations": [],
        }
    ]


def test_risk_status_rejects_unknown_and_duplicate_fields(tmp_path: Path) -> None:
    raw_status = json.loads(check_risk_status.STATUS_PATH.read_text(encoding="utf-8"))
    raw_status["unexpected"] = True

    with pytest.raises(check_risk_status.RiskStatusError, match="unknown"):
        check_risk_status.parse_status(raw_status)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"statusVersion":1,"statusVersion":1}',
        encoding="utf-8",
    )
    with pytest.raises(check_risk_status.RiskStatusError, match="duplicate JSON field"):
        check_risk_status.load_status(duplicate)


def test_missing_source_evidence_reopens_resolved_risk(tmp_path: Path) -> None:
    status = _copy_evidence(tmp_path)
    missing = tmp_path / status["risks"][0]["sourceEvidence"][0]["path"]
    missing.unlink()

    report = check_risk_status.check_risk_status(
        status,
        root=tmp_path,
        verify_git=False,
    )

    assert report["passed"] is False
    assert report["reopened"] == 1
    assert "missing evidence" in report["violations"][0]


def test_source_digest_drift_reopens_resolved_risk(tmp_path: Path) -> None:
    status = _copy_evidence(tmp_path)
    source = tmp_path / status["risks"][0]["sourceEvidence"][0]["path"]
    source.write_bytes(source.read_bytes() + b"\n")

    report = check_risk_status.check_risk_status(
        status,
        root=tmp_path,
        verify_git=False,
    )

    assert report["reopened"] == 1
    assert any("source digest changed" in item for item in report["violations"])


def test_tck_mirror_drift_reopens_resolved_risk(tmp_path: Path) -> None:
    status = _copy_evidence(tmp_path)
    mirror = tmp_path / status["risks"][0]["tckEvidence"]["mirrorPath"]
    mirror.write_bytes(mirror.read_bytes() + b"\n")

    report = check_risk_status.check_risk_status(
        status,
        root=tmp_path,
        verify_git=False,
    )

    assert report["reopened"] == 1
    assert any("TCK mirror drifted" in item for item in report["violations"])


def test_invalid_tck_evidence_reopens_resolved_risk(tmp_path: Path) -> None:
    status = _copy_evidence(tmp_path)
    tck_path = tmp_path / status["risks"][0]["tckEvidence"]["path"]
    mirror_path = tmp_path / status["risks"][0]["tckEvidence"]["mirrorPath"]
    tck_path.write_text("not-json", encoding="utf-8")
    mirror_path.write_text("not-json", encoding="utf-8")

    report = check_risk_status.check_risk_status(
        status,
        root=tmp_path,
        verify_git=False,
    )

    assert report["reopened"] == 1
    assert any("TCK evidence is invalid" in item for item in report["violations"])
