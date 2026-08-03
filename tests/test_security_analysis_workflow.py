from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import re
import sys

import pytest
import yaml


ROOT = Path(__file__).parents[1]
TOOL_PATH = ROOT / "tools" / "run_dependency_audits.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("run_dependency_audits", TOOL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_vulnerability_exception_manifest_is_closed_and_current(tmp_path: Path) -> None:
    tool = _load_tool()
    manifest = tmp_path / "exceptions.yaml"
    manifest.write_text(
        """\
version: 1
exceptions:
  - id: SEC-EXC-001
    ecosystem: python
    advisoryId: PYSEC-2099-1
    package: example-package
    reason: Temporary compatibility constraint with a tracked removal plan.
    evidenceUrl: https://github.com/graphblocks/graphblocks/issues/1
    expiresOn: "2099-01-31"
""",
        encoding="utf-8",
    )

    assert tool.load_exceptions(manifest, today=date(2099, 1, 30)) == (
        {
            "id": "SEC-EXC-001",
            "ecosystem": "python",
            "advisoryId": "PYSEC-2099-1",
            "package": "example-package",
            "reason": "Temporary compatibility constraint with a tracked removal plan.",
            "evidenceUrl": "https://github.com/graphblocks/graphblocks/issues/1",
            "expiresOn": "2099-01-31",
        },
    )

    expired = manifest.read_text(encoding="utf-8").replace("2099-01-31", "2099-01-29")
    manifest.write_text(expired, encoding="utf-8")
    with pytest.raises(tool.DependencyAuditError, match="has expired"):
        tool.load_exceptions(manifest, today=date(2099, 1, 30))

    unknown = expired.replace("    expiresOn:", "    ticket: untracked\n    expiresOn:")
    manifest.write_text(unknown, encoding="utf-8")
    with pytest.raises(tool.DependencyAuditError, match="must contain exactly"):
        tool.load_exceptions(manifest, today=date(2099, 1, 28))


def test_dependency_audits_apply_only_validated_ecosystem_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    manifest = tmp_path / "exceptions.yaml"
    manifest.write_text(
        """\
version: 1
exceptions:
  - id: SEC-EXC-001
    ecosystem: python
    advisoryId: PYSEC-2099-1
    package: example-python
    reason: Temporary Python compatibility constraint.
    evidenceUrl: https://github.com/graphblocks/graphblocks/issues/1
    expiresOn: "2099-01-31"
  - id: SEC-EXC-002
    ecosystem: rust
    advisoryId: RUSTSEC-2099-0001
    package: example-rust
    reason: Temporary Rust compatibility constraint.
    evidenceUrl: https://github.com/graphblocks/graphblocks/issues/2
    expiresOn: "2099-01-31"
""",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        commands.append(command)
        return Completed()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert (
        tool.run_dependency_audits(
            exceptions_path=manifest,
            output_dir=tmp_path / "evidence",
            today=date(2099, 1, 30),
        )
        == 0
    )
    assert commands[0][-2:] == ["--ignore-vuln", "PYSEC-2099-1"]
    assert commands[1][-2:] == ["--ignore", "RUSTSEC-2099-0001"]


def test_ci_requires_dependency_audit_and_codeql() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]

    dependency_audit = jobs["dependency-audit"]
    assert dependency_audit["permissions"] == {"contents": "read"}
    dependency_steps = {step["name"]: step for step in dependency_audit["steps"]}
    assert dependency_steps["Install pinned vulnerability scanners"]["run"] == (
        "python -m pip install pip-audit==2.10.1\n"
        "cargo install cargo-audit --version 0.22.2 --locked\n"
    )
    audit_run = dependency_steps["Audit Python and Rust dependency graphs"]["run"]
    assert audit_run == (
        "python tools/run_dependency_audits.py --output-dir dist/ci/dependency-audit"
    )
    retained = dependency_steps["Retain dependency-audit evidence"]
    assert retained["if"] == "always()"
    assert retained["with"]["if-no-files-found"] == "error"

    codeql = jobs["codeql"]
    assert codeql["permissions"] == {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    }
    codeql_steps = {step["name"]: step for step in codeql["steps"]}
    action_sha = "7211b7c8077ea37d8641b6271f6a365a22a5fbfa"
    assert codeql_steps["Initialize CodeQL"]["uses"] == (
        f"github/codeql-action/init@{action_sha}"
    )
    assert codeql_steps["Initialize CodeQL"]["with"] == {
        "languages": "python",
        "queries": "security-extended",
    }
    assert codeql_steps["Analyze Python"]["uses"] == (
        f"github/codeql-action/analyze@{action_sha}"
    )
    assert codeql_steps["Analyze Python"]["with"] == {"category": "/language:python"}
    assert all(
        re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", step["uses"])
        for step in codeql["steps"]
        if "uses" in step
    )

    required = jobs["required-gates"]
    assert "dependency-audit" in required["needs"]
    assert "codeql" in required["needs"]
    required_step = required["steps"][0]
    assert required_step["env"]["DEPENDENCY_AUDIT_RESULT"] == (
        "${{ needs.dependency-audit.result }}"
    )
    assert required_step["env"]["CODEQL_RESULT"] == "${{ needs.codeql.result }}"
    assert "dependency-audit:$DEPENDENCY_AUDIT_RESULT" in required_step["run"]
    assert "codeql:$CODEQL_RESULT" in required_step["run"]
