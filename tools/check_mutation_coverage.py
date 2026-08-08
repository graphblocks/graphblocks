from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from typing import Literal

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).parents[1]
DEFAULT_MANIFEST = ROOT / "compatibility" / "stable-mutation-budget.yaml"
_REQUIRED_CATEGORIES = frozenset({"canonical", "compiler", "policy", "durable-handler"})
_SOURCE_ROOTS = (
    PurePosixPath("src/graphblocks"),
    PurePosixPath("packages/graphblocks-testing/src/graphblocks_testing"),
)


class MutationCoverageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MutationSpec:
    mutant_id: str
    category: str
    source: PurePosixPath
    description: str
    find: str
    replace: str
    tests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MutationBudget:
    path: Path
    sha256: str
    scope: Literal["stable-core-seed"]
    minimum_score_percent: int
    maximum_surviving_mutants: int
    timeout_seconds_per_mutant: int
    mutants: tuple[MutationSpec, ...]


def load_mutation_budget(path: Path = DEFAULT_MANIFEST) -> MutationBudget:
    raw_bytes = path.read_bytes()
    try:
        document = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as error:
        raise MutationCoverageError(f"cannot parse {path}: {error}") from error
    if type(document) is not dict:
        raise MutationCoverageError("mutation budget must be an object")
    root = document
    if any(type(key) is not str for key in root):
        raise MutationCoverageError("mutation budget keys must be strings")
    root_fields = frozenset({"schemaVersion", "scope", "thresholds", "mutants"})
    unknown_root_fields = sorted(set(root) - root_fields)
    missing_root_fields = sorted(root_fields - set(root))
    if unknown_root_fields:
        raise MutationCoverageError(
            "mutation budget contains unknown fields: " + ", ".join(unknown_root_fields)
        )
    if missing_root_fields:
        raise MutationCoverageError(
            "mutation budget is missing fields: " + ", ".join(missing_root_fields)
        )
    if type(root["schemaVersion"]) is not int or root["schemaVersion"] != 1:
        raise MutationCoverageError("mutation budget schemaVersion must be 1")
    scope = root["scope"]
    if type(scope) is not str or not scope or scope != scope.strip():
        raise MutationCoverageError(
            "mutation budget scope must be a non-empty string without "
            "surrounding whitespace"
        )
    if scope != "stable-core-seed":
        raise MutationCoverageError("mutation budget scope must be 'stable-core-seed'")

    thresholds = root["thresholds"]
    if type(thresholds) is not dict:
        raise MutationCoverageError("mutation budget thresholds must be an object")
    if any(type(key) is not str for key in thresholds):
        raise MutationCoverageError("mutation budget threshold keys must be strings")
    threshold_fields = frozenset(
        {
            "minimumMutationScorePercent",
            "maximumSurvivingMutants",
            "timeoutSecondsPerMutant",
        }
    )
    unknown_threshold_fields = sorted(set(thresholds) - threshold_fields)
    missing_threshold_fields = sorted(threshold_fields - set(thresholds))
    if unknown_threshold_fields:
        raise MutationCoverageError(
            "mutation budget thresholds contain unknown fields: "
            + ", ".join(unknown_threshold_fields)
        )
    if missing_threshold_fields:
        raise MutationCoverageError(
            "mutation budget thresholds are missing fields: "
            + ", ".join(missing_threshold_fields)
        )
    minimum_score = thresholds["minimumMutationScorePercent"]
    if (
        isinstance(minimum_score, bool)
        or not isinstance(minimum_score, int)
        or not 0 <= minimum_score <= 100
    ):
        raise MutationCoverageError(
            "minimumMutationScorePercent must be an integer from 0 through 100"
        )
    maximum_survivors = thresholds["maximumSurvivingMutants"]
    if (
        isinstance(maximum_survivors, bool)
        or not isinstance(maximum_survivors, int)
        or not 0 <= maximum_survivors <= 32
    ):
        raise MutationCoverageError(
            "maximumSurvivingMutants must be an integer from 0 through 32"
        )
    timeout_seconds = thresholds["timeoutSecondsPerMutant"]
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 120
    ):
        raise MutationCoverageError(
            "timeoutSecondsPerMutant must be an integer from 1 through 120"
        )

    raw_mutants = root["mutants"]
    if type(raw_mutants) is not list or not 1 <= len(raw_mutants) <= 32:
        raise MutationCoverageError("mutants must contain from 1 through 32 items")
    mutants: list[MutationSpec] = []
    for index, raw_mutant in enumerate(raw_mutants):
        owner = f"mutants[{index}]"
        if type(raw_mutant) is not dict:
            raise MutationCoverageError(f"{owner} must be an object")
        item = raw_mutant
        if any(type(key) is not str for key in item):
            raise MutationCoverageError(f"{owner} keys must be strings")
        item_fields = frozenset(
            {"id", "category", "source", "description", "find", "replace", "tests"}
        )
        unknown_item_fields = sorted(set(item) - item_fields)
        missing_item_fields = sorted(item_fields - set(item))
        if unknown_item_fields:
            raise MutationCoverageError(
                f"{owner} contains unknown fields: {', '.join(unknown_item_fields)}"
            )
        if missing_item_fields:
            raise MutationCoverageError(
                f"{owner} is missing fields: {', '.join(missing_item_fields)}"
            )
        mutant_id = item["id"]
        if (
            type(mutant_id) is not str
            or not mutant_id
            or mutant_id != mutant_id.strip()
        ):
            raise MutationCoverageError(
                f"{owner}.id must be a non-empty string without surrounding whitespace"
            )
        category = item["category"]
        if type(category) is not str or not category or category != category.strip():
            raise MutationCoverageError(
                f"{owner}.category must be a non-empty string without surrounding whitespace"
            )
        if category not in _REQUIRED_CATEGORIES:
            raise MutationCoverageError(f"{owner}.category is unsupported")
        raw_source = item["source"]
        if (
            type(raw_source) is not str
            or not raw_source
            or raw_source != raw_source.strip()
        ):
            raise MutationCoverageError(
                f"{owner}.source must be a non-empty string without surrounding whitespace"
            )
        source = PurePosixPath(raw_source)
        if (
            source.is_absolute()
            or ".." in source.parts
            or source.suffix != ".py"
            or not any(source.is_relative_to(root) for root in _SOURCE_ROOTS)
        ):
            raise MutationCoverageError(
                f"{owner}.source must be a Python file in an allowed source root"
            )
        description = item["description"]
        if (
            type(description) is not str
            or not description
            or description != description.strip()
        ):
            raise MutationCoverageError(
                f"{owner}.description must be a non-empty string without surrounding whitespace"
            )
        find = item["find"]
        replace = item["replace"]
        if type(find) is not str or not find:
            raise MutationCoverageError(f"{owner}.find must be a non-empty string")
        if type(replace) is not str or not replace or replace == find:
            raise MutationCoverageError(
                f"{owner}.replace must be non-empty and differ from find"
            )
        raw_tests = item["tests"]
        if type(raw_tests) is not list or not raw_tests:
            raise MutationCoverageError(f"{owner}.tests must be a non-empty array")
        normalized_tests: list[str] = []
        for test_index, value in enumerate(raw_tests):
            if type(value) is not str or not value or value != value.strip():
                raise MutationCoverageError(
                    f"{owner}.tests[{test_index}] must be a non-empty string "
                    "without surrounding whitespace"
                )
            normalized_tests.append(value)
        tests = tuple(normalized_tests)
        if any(
            not test.startswith("tests/") or "::" not in test or test.startswith("-")
            for test in tests
        ):
            raise MutationCoverageError(
                f"{owner}.tests must contain explicit tests/ pytest node ids"
            )
        mutants.append(
            MutationSpec(
                mutant_id=mutant_id,
                category=category,
                source=source,
                description=description,
                find=find,
                replace=replace,
                tests=tests,
            )
        )

    ids = tuple(item.mutant_id for item in mutants)
    if len(set(ids)) != len(ids):
        raise MutationCoverageError("mutation ids must be unique")
    categories = frozenset(item.category for item in mutants)
    missing_categories = sorted(_REQUIRED_CATEGORIES - categories)
    if missing_categories:
        raise MutationCoverageError(
            "mutation budget is missing stable seed categories: "
            + ", ".join(missing_categories)
        )
    return MutationBudget(
        path=path,
        sha256="sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
        scope="stable-core-seed",
        minimum_score_percent=minimum_score,
        maximum_surviving_mutants=maximum_survivors,
        timeout_seconds_per_mutant=timeout_seconds,
        mutants=tuple(mutants),
    )


def validate_mutation_sources(budget: MutationBudget, *, root: Path = ROOT) -> None:
    for mutant in budget.mutants:
        source_path = root / mutant.source
        if not source_path.is_file():
            raise MutationCoverageError(
                f"mutation {mutant.mutant_id} source does not exist: {mutant.source}"
            )
        source = source_path.read_text(encoding="utf-8")
        occurrences = source.count(mutant.find)
        if occurrences != 1:
            raise MutationCoverageError(
                f"mutation {mutant.mutant_id} find text must occur exactly once; "
                f"found {occurrences}"
            )
        mutated = source.replace(mutant.find, mutant.replace, 1)
        try:
            compile(mutated, str(mutant.source), "exec")
        except SyntaxError as error:
            raise MutationCoverageError(
                f"mutation {mutant.mutant_id} does not produce valid Python"
            ) from error
        for node_id in mutant.tests:
            test_path = root / node_id.split("::", 1)[0]
            if not test_path.is_file():
                raise MutationCoverageError(
                    f"mutation {mutant.mutant_id} test does not exist: {node_id}"
                )


def run_mutation_budget(
    budget: MutationBudget,
    *,
    root: Path = ROOT,
) -> dict[str, object]:
    validate_mutation_sources(budget, root=root)
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="graphblocks-mutation-") as raw_temp:
        temp_root = Path(raw_temp)
        for mutant in budget.mutants:
            checkout = temp_root / mutant.mutant_id
            main_destination = checkout / "src" / "graphblocks"
            testing_destination = (
                checkout
                / "packages"
                / "graphblocks-testing"
                / "src"
                / "graphblocks_testing"
            )
            shutil.copytree(root / "src" / "graphblocks", main_destination)
            shutil.copytree(
                root
                / "packages"
                / "graphblocks-testing"
                / "src"
                / "graphblocks_testing",
                testing_destination,
            )
            shutil.copytree(root / "schemas", checkout / "schemas")
            mutated_source_path = checkout / mutant.source
            original_bytes = mutated_source_path.read_bytes()
            source = original_bytes.decode("utf-8")
            mutated_source = source.replace(mutant.find, mutant.replace, 1)

            environment = os.environ.copy()
            mutation_paths = os.pathsep.join(
                (
                    str(checkout / "src"),
                    str(checkout / "packages/graphblocks-testing/src"),
                )
            )
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                mutation_paths
                if not existing_pythonpath
                else mutation_paths + os.pathsep + existing_pythonpath
            )
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                *mutant.tests,
            ]
            try:
                baseline = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=budget.timeout_seconds_per_mutant,
                )
            except subprocess.TimeoutExpired:
                results.append(
                    {
                        "id": mutant.mutant_id,
                        "category": mutant.category,
                        "description": mutant.description,
                        "source": mutant.source.as_posix(),
                        "tests": list(mutant.tests),
                        "status": "inconclusive",
                        "reason": "baseline-timeout",
                        "sourceSha256": "sha256:"
                        + hashlib.sha256(original_bytes).hexdigest(),
                        "mutatedSourceSha256": "sha256:"
                        + hashlib.sha256(mutated_source.encode("utf-8")).hexdigest(),
                    }
                )
                continue
            if baseline.returncode != 0:
                results.append(
                    {
                        "id": mutant.mutant_id,
                        "category": mutant.category,
                        "description": mutant.description,
                        "source": mutant.source.as_posix(),
                        "tests": list(mutant.tests),
                        "status": "inconclusive",
                        "reason": "baseline-tests-failed",
                        "baselineTestExitCode": baseline.returncode,
                        "sourceSha256": "sha256:"
                        + hashlib.sha256(original_bytes).hexdigest(),
                        "mutatedSourceSha256": "sha256:"
                        + hashlib.sha256(mutated_source.encode("utf-8")).hexdigest(),
                    }
                )
                continue

            mutated_source_path.write_text(mutated_source, encoding="utf-8")
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=budget.timeout_seconds_per_mutant,
                )
            except subprocess.TimeoutExpired:
                results.append(
                    {
                        "id": mutant.mutant_id,
                        "category": mutant.category,
                        "description": mutant.description,
                        "source": mutant.source.as_posix(),
                        "tests": list(mutant.tests),
                        "status": "inconclusive",
                        "reason": "mutant-timeout",
                        "baselineTestExitCode": baseline.returncode,
                        "sourceSha256": "sha256:"
                        + hashlib.sha256(original_bytes).hexdigest(),
                        "mutatedSourceSha256": "sha256:"
                        + hashlib.sha256(mutated_source.encode("utf-8")).hexdigest(),
                    }
                )
                continue
            results.append(
                {
                    "id": mutant.mutant_id,
                    "category": mutant.category,
                    "description": mutant.description,
                    "source": mutant.source.as_posix(),
                    "tests": list(mutant.tests),
                    "status": "survived" if completed.returncode == 0 else "killed",
                    "baselineTestExitCode": baseline.returncode,
                    "testExitCode": completed.returncode,
                    "sourceSha256": "sha256:"
                    + hashlib.sha256(original_bytes).hexdigest(),
                    "mutatedSourceSha256": "sha256:"
                    + hashlib.sha256(mutated_source.encode("utf-8")).hexdigest(),
                }
            )

    killed = sum(item["status"] == "killed" for item in results)
    survived = sum(item["status"] == "survived" for item in results)
    inconclusive = sum(item["status"] == "inconclusive" for item in results)
    mutation_score = round(killed * 100 / len(results), 2)
    passed = (
        inconclusive == 0
        and survived <= budget.maximum_surviving_mutants
        and mutation_score >= budget.minimum_score_percent
    )
    return {
        "schemaVersion": 1,
        "scope": budget.scope,
        "manifestSha256": budget.sha256,
        "thresholds": {
            "minimumMutationScorePercent": budget.minimum_score_percent,
            "maximumSurvivingMutants": budget.maximum_surviving_mutants,
            "timeoutSecondsPerMutant": budget.timeout_seconds_per_mutant,
        },
        "summary": {
            "total": len(results),
            "killed": killed,
            "survived": survived,
            "inconclusive": inconclusive,
            "mutationScorePercent": mutation_score,
        },
        "survivingMutants": [
            item["id"] for item in results if item["status"] == "survived"
        ],
        "inconclusiveMutants": [
            item["id"] for item in results if item["status"] == "inconclusive"
        ],
        "mutants": results,
        "passed": passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the bounded stable-core mutation testing budget."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    try:
        budget = load_mutation_budget(args.manifest)
        validate_mutation_sources(budget, root=args.root)
        if args.list:
            for mutant in budget.mutants:
                print(mutant.mutant_id)
            return 0
        if args.validate_only:
            print(f"validated {len(budget.mutants)} stable mutation seeds")
            return 0
        report = run_mutation_budget(budget, root=args.root)
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        summary = report["summary"]
        if not isinstance(summary, dict):
            raise MutationCoverageError("mutation report summary is malformed")
        print(
            "stable mutation budget: "
            f"{summary['killed']}/{summary['total']} killed, "
            f"{summary['survived']} survived, "
            f"{summary['inconclusive']} inconclusive"
        )
        return 0 if report["passed"] is True else 1
    except (MutationCoverageError, OSError) as error:
        print(f"mutation coverage check failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
