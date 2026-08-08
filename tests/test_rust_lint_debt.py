from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools import check_rust_lint_debt


ROOT = Path(__file__).parents[1]


def _diagnostic(path: str) -> str:
    return json.dumps(
        {
            "reason": "compiler-message",
            "message": {
                "code": {"code": "clippy::expect_used"},
                "spans": [{"file_name": path, "is_primary": True}],
            },
        }
    )


def test_checked_in_baseline_is_closed_and_matches_workspace_policy() -> None:
    baseline = check_rust_lint_debt.load_baseline()

    assert baseline.total == 119
    assert len(baseline.files) == 16
    check_rust_lint_debt.verify_policy(baseline)


def test_baseline_rejects_unknown_fields_and_inexact_total() -> None:
    payload = json.loads(check_rust_lint_debt.BASELINE_PATH.read_text(encoding="utf-8"))
    with_unknown = deepcopy(payload)
    with_unknown["unexpected"] = True
    with pytest.raises(check_rust_lint_debt.RustLintDebtError, match="unknown"):
        check_rust_lint_debt.parse_baseline(with_unknown)

    wrong_total = deepcopy(payload)
    wrong_total["total"] += 1
    with pytest.raises(check_rust_lint_debt.RustLintDebtError, match="exact file sum"):
        check_rust_lint_debt.parse_baseline(wrong_total)


def test_baseline_loader_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"schemaVersion": 1, "schemaVersion": 1}\n', encoding="utf-8")

    with pytest.raises(check_rust_lint_debt.RustLintDebtError, match="duplicate"):
        check_rust_lint_debt.load_baseline(baseline)


def test_clippy_diagnostics_are_counted_by_repository_path() -> None:
    relative = "crates/graphblocks-compiler/src/compiler.rs"
    lines = [
        json.dumps({"reason": "build-finished", "success": True}),
        json.dumps({"reason": "compiler-message", "message": {"code": None}}),
        _diagnostic(relative),
        _diagnostic(str(ROOT / relative)),
    ]

    assert check_rust_lint_debt.parse_clippy_messages(lines) == {relative: 2}


def test_debt_gate_allows_reduction_but_rejects_growth_and_new_files() -> None:
    baseline = check_rust_lint_debt.load_baseline()
    reduced = {path: count - 1 for path, count in baseline.files.items()}
    assert check_rust_lint_debt.evaluate_counts(baseline, reduced) == []

    grown = dict(baseline.files)
    first_path = next(iter(grown))
    grown[first_path] += 1
    grown["crates/new-production-module/src/lib.rs"] = 1
    violations = check_rust_lint_debt.evaluate_counts(baseline, grown)

    assert any(first_path in violation for violation in violations)
    assert any("new-production-module" in violation for violation in violations)
    assert any(violation.startswith("total:") for violation in violations)


def test_clippy_command_uses_the_exact_production_scope_and_force_warn() -> None:
    assert check_rust_lint_debt.clippy_command() == (
        "cargo",
        "clippy",
        "--workspace",
        "--lib",
        "--bins",
        "--locked",
        "--message-format=json",
        "--",
        "--force-warn",
        "clippy::expect_used",
    )
