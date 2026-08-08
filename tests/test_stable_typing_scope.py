from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import yaml

from tools import check_stable_typing
from tools.check_stable_typing import (
    check_repository,
    validate_preview_typing_budget,
    validate_production_typing_budget,
    validate_root_exports,
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
    production_files = [
        path
        for path in mypy["files"]
        if path.startswith("src/graphblocks/")
    ]
    assert len(production_files) >= 14

    budget = yaml.safe_load(
        (ROOT / "compatibility/python-typing-scope.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert budget == {
        "version": 1,
        "productionSourceRoot": "src/graphblocks",
        "minimumStrictModuleCount": 18,
        "maximumTypeIgnoreCommentCount": 145,
        "maximumUncodedTypeIgnoreCommentCount": 0,
    }

    preview_budget = yaml.safe_load(
        (ROOT / "compatibility/python-preview-typing-budget.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert preview_budget == {
        "version": 1,
        "mypyVersion": "1.20.2",
        "mypyMode": "strict-no-incremental-follow-imports-silent",
        "packages": [
            {
                "distribution": "graphblocks",
                "sourceRoot": "src/graphblocks",
                "minimumStrictModuleCount": 18,
                "maximumDebtModuleCount": 91,
                "maximumDiagnosticCount": 728,
                "maximumTypeIgnoreCommentCount": 145,
                "maximumUncodedTypeIgnoreCommentCount": 0,
                "rootCompatibilityMap": "src/graphblocks/_root_compat.py",
                "maximumPreviewCompatibilityAliasCount": 606,
            },
            {
                "distribution": "graphblocks-runtime",
                "sourceRoot": "packages/graphblocks-runtime/src/graphblocks_runtime",
                "minimumStrictModuleCount": 0,
                "maximumDebtModuleCount": 1,
                "maximumDiagnosticCount": 73,
                "maximumTypeIgnoreCommentCount": 0,
                "maximumUncodedTypeIgnoreCommentCount": 0,
            },
            {
                "distribution": "graphblocks-testing",
                "sourceRoot": "packages/graphblocks-testing/src/graphblocks_testing",
                "minimumStrictModuleCount": 0,
                "maximumDebtModuleCount": 13,
                "maximumDiagnosticCount": 292,
                "maximumTypeIgnoreCommentCount": 40,
                "maximumUncodedTypeIgnoreCommentCount": 0,
            },
        ],
    }


def test_production_typing_budget_rejects_scope_and_ignore_regressions() -> None:
    budget = {
        "version": 1,
        "productionSourceRoot": "src/graphblocks",
        "minimumStrictModuleCount": 14,
        "maximumTypeIgnoreCommentCount": 145,
        "maximumUncodedTypeIgnoreCommentCount": 0,
    }

    assert validate_production_typing_budget(
        budget=budget,
        strict_module_count=14,
        type_ignore_comment_count=145,
        uncoded_type_ignore_comment_count=0,
    ) == []
    assert validate_production_typing_budget(
        budget=budget,
        strict_module_count=13,
        type_ignore_comment_count=146,
        uncoded_type_ignore_comment_count=1,
    ) == [
        "production strict mypy scope regressed: expected at least 14 modules, found 13",
        "production type-ignore debt increased: maximum 145, found 146",
        "production uncoded type-ignore debt increased: maximum 0, found 1",
    ]


def test_preview_typing_budget_rejects_package_debt_regressions() -> None:
    budget = {
        "version": 1,
        "mypyVersion": "1.20.2",
        "mypyMode": "strict-no-incremental-follow-imports-silent",
        "packages": [
            {
                "distribution": "graphblocks-preview",
                "sourceRoot": "src/graphblocks_preview",
                "minimumStrictModuleCount": 2,
                "maximumDebtModuleCount": 0,
                "maximumDiagnosticCount": 3,
                "maximumTypeIgnoreCommentCount": 0,
                "maximumUncodedTypeIgnoreCommentCount": 0,
                "rootCompatibilityMap": "src/graphblocks_preview/_compat.py",
                "maximumPreviewCompatibilityAliasCount": 1,
            }
        ],
    }
    report = {
        "reportVersion": 1,
        "mypyVersion": "1.20.2",
        "mypyMode": "strict-no-incremental-follow-imports-silent",
        "packages": [
            {
                "distribution": "graphblocks-preview",
                "sourceRoot": "src/graphblocks_preview",
                "moduleCount": 2,
                "strictModuleCount": 1,
                "debtModuleCount": 1,
                "diagnosticCount": 4,
                "typeIgnoreCommentCount": 1,
                "uncodedTypeIgnoreCommentCount": 1,
                "previewCompatibilityAliasCount": 2,
                "modules": [
                    {
                        "module": "graphblocks_preview",
                        "path": "src/graphblocks_preview/__init__.py",
                        "classification": "strict",
                        "diagnosticCount": 1,
                        "typeIgnoreCommentCount": 0,
                        "uncodedTypeIgnoreCommentCount": 0,
                    },
                    {
                        "module": "graphblocks_preview.feature",
                        "path": "src/graphblocks_preview/feature.py",
                        "classification": "debt",
                        "diagnosticCount": 3,
                        "typeIgnoreCommentCount": 1,
                        "uncodedTypeIgnoreCommentCount": 1,
                    },
                ],
            }
        ],
    }

    assert validate_preview_typing_budget(budget=budget, report=report) == sorted(
        [
            "preview typing graphblocks-preview debtModuleCount increased above "
            "budget 0: found 1",
            "preview typing graphblocks-preview diagnosticCount increased above "
            "budget 3: found 4",
            "preview typing graphblocks-preview previewCompatibilityAliasCount "
            "increased above budget 1: found 2",
            "preview typing graphblocks-preview strictModuleCount regressed below "
            "budget 2: found 1",
            "preview typing graphblocks-preview typeIgnoreCommentCount increased "
            "above budget 0: found 1",
            "preview typing graphblocks-preview uncodedTypeIgnoreCommentCount "
            "increased above budget 0: found 1",
        ]
    )


def test_production_typing_scanner_distinguishes_coded_and_bare_ignores(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "graphblocks"
    source_root.mkdir()
    (source_root / "sample.py").write_text(
        "coded = value  # type: ignore[arg-type]\n"
        "bare = value  # type: ignore\n",
        encoding="utf-8",
    )

    assert check_stable_typing._production_type_ignore_comment_counts(
        source_root
    ) == (2, 1)


def test_stable_typing_scope_requires_exact_ordered_root_exports() -> None:
    stable_symbols = (
        "graphblocks.Foo",
        "graphblocks.Foo.method",
        "graphblocks.Bar",
    )

    assert validate_root_exports(
        stable_symbols=stable_symbols,
        public_exports=("Foo", "Bar"),
    ) == []
    assert validate_root_exports(
        stable_symbols=stable_symbols,
        public_exports=("Foo", "Preview"),
    ) == [
        "stable root export is missing from __all__: Bar",
        "preview or internal root export is present in __all__: Preview",
    ]
    assert validate_root_exports(
        stable_symbols=stable_symbols,
        public_exports=("Bar", "Foo"),
    ) == ["graphblocks package root __all__ must follow stable surface order"]


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
    report_path = tmp_path / "typing-debt.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/check_stable_typing.py"),
            "--report",
            str(report_path),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.startswith("OK stable Python typing ownership:")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["reportVersion"] == 1
    packages = {entry["distribution"]: entry for entry in report["packages"]}
    assert set(packages) == {
        "graphblocks",
        "graphblocks-runtime",
        "graphblocks-testing",
    }
    assert packages["graphblocks"]["moduleCount"] == len(
        packages["graphblocks"]["modules"]
    )
    assert packages["graphblocks"]["strictModuleCount"] == 18
    assert packages["graphblocks"]["previewCompatibilityAliasCount"] == 606
    assert all(package["modules"] for package in packages.values())


def test_ci_runs_stable_typing_checker_before_mypy() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    checker = "python tools/check_stable_typing.py"
    mypy = "python -m mypy"
    assert checker in workflow
    assert workflow.index(checker) < workflow.index(mypy)
    assert "--report dist/ci/python-typing-debt.json" in workflow
    assert "path: dist/ci" in workflow
