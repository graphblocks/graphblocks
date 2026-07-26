from __future__ import annotations

from datetime import date
from pathlib import Path
import subprocess
import sys
import tomllib

from tools.check_stable_typing import (
    check_repository,
    validate_typing_coverage,
)


ROOT = Path(__file__).parents[1]


def _debt_entry(
    *symbols: str,
    module: str = "graphblocks.sample",
    review_by: str = "2026-12-31",
) -> dict[str, object]:
    return {
        "module": module,
        "issue": "TYPE-001",
        "owner": "graphblocks-maintainers",
        "reason": "The stable owner is not strict-green yet.",
        "review_by": review_by,
        "symbols": list(symbols),
    }


def test_repository_stable_typing_scope_is_fully_classified() -> None:
    errors = check_repository(ROOT, today=date.today())

    assert errors == []


def test_repository_mypy_scope_silences_only_transitive_diagnostics() -> None:
    with (ROOT / "pyproject.toml").open("rb") as pyproject_file:
        mypy = tomllib.load(pyproject_file)["tool"]["mypy"]

    assert mypy["follow_imports"] == "silent"


def test_stable_typing_scope_rejects_missing_symbol_debt() -> None:
    errors = validate_typing_coverage(
        stable_symbols=("graphblocks.Foo", "graphblocks.Bar"),
        symbol_owners={
            "graphblocks.Foo": "graphblocks.sample",
            "graphblocks.Bar": "graphblocks.sample",
        },
        strict_modules=set(),
        debt={
            "version": 1,
            "modules": [_debt_entry("graphblocks.Foo")],
        },
        today=date(2026, 7, 26),
    )

    assert "stable symbol is not covered: graphblocks.Bar" in errors


def test_stable_typing_scope_rejects_expired_debt() -> None:
    errors = validate_typing_coverage(
        stable_symbols=("graphblocks.Foo",),
        symbol_owners={"graphblocks.Foo": "graphblocks.sample"},
        strict_modules=set(),
        debt={
            "version": 1,
            "modules": [
                _debt_entry(
                    "graphblocks.Foo",
                    review_by="2026-07-25",
                )
            ],
        },
        today=date(2026, 7, 26),
    )

    assert any("debt review expired" in error for error in errors)


def test_stable_typing_scope_rejects_stale_debt_for_strict_module() -> None:
    errors = validate_typing_coverage(
        stable_symbols=("graphblocks.Foo",),
        symbol_owners={"graphblocks.Foo": "graphblocks.sample"},
        strict_modules={"graphblocks.sample"},
        debt={
            "version": 1,
            "modules": [_debt_entry("graphblocks.Foo")],
        },
        today=date(2026, 7, 26),
    )

    assert any("strict module has stale debt" in error for error in errors)


def test_stable_typing_scope_rejects_unknown_and_duplicate_registry_entries() -> None:
    entry = _debt_entry(
        "graphblocks.Foo",
        "graphblocks.Foo",
        "graphblocks.Unknown",
    )
    errors = validate_typing_coverage(
        stable_symbols=("graphblocks.Foo",),
        symbol_owners={"graphblocks.Foo": "graphblocks.sample"},
        strict_modules=set(),
        debt={
            "version": 1,
            "modules": [
                entry,
                dict(entry),
                _debt_entry(
                    "graphblocks.Foo",
                    module="graphblocks.unknown",
                ),
            ],
        },
        today=date(2026, 7, 26),
    )

    assert any("debt module is registered more than once" in error for error in errors)
    assert any("debt symbol is registered more than once" in error for error in errors)
    assert any("unknown stable symbol in debt" in error for error in errors)
    assert any("unknown stable owner module in debt" in error for error in errors)


def test_stable_typing_checker_runs_outside_repository_root(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/check_stable_typing.py")],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.startswith("OK stable Python typing ownership:")


def test_ci_runs_stable_typing_checker_before_mypy() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    checker = "python tools/check_stable_typing.py"
    mypy = "python -m mypy"
    assert checker in workflow
    assert workflow.index(checker) < workflow.index(mypy)
