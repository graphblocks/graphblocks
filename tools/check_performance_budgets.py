from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
import gc
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import platform
from statistics import median
import sys
from time import perf_counter
from typing import Literal

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).parents[1]
DEFAULT_BUDGET_PATH = ROOT / "compatibility" / "python-performance-budgets.yaml"
_BENCHMARK_IDS = (
    "canonical-decimal-scaling",
    "journal-append-scaling",
    "compiler-scaling",
    "server-retained-memory",
)
_METRICS = frozenset({"elapsedSeconds", "retainedBytes"})


class PerformanceBudgetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PerformanceProtocol:
    warmup_runs: int
    measured_runs: int
    statistic: Literal["median"]
    garbage_collection: Literal["collect-before-each-observation"]


@dataclass(frozen=True, slots=True)
class BenchmarkBudget:
    benchmark_id: str
    metric: Literal["elapsedSeconds", "retainedBytes"]
    sizes: tuple[int, ...]
    maximum_by_size: Mapping[int, float]
    maximum_normalized_growth: float
    warmup_runs: int
    measured_runs: int


@dataclass(frozen=True, slots=True)
class PerformanceBudgets:
    path: Path
    sha256: str
    platform: str
    python: str
    protocol: PerformanceProtocol
    companion_gates: tuple[str, ...]
    benchmarks: tuple[BenchmarkBudget, ...]


BenchmarkOperation = Callable[[int], object]


def _closed_mapping(
    value: object,
    *,
    owner: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise PerformanceBudgetError(f"{owner} must be an object")
    mapping = value
    if any(type(key) is not str for key in mapping):
        raise PerformanceBudgetError(f"{owner} keys must be strings")
    unknown = sorted(set(mapping) - fields)
    missing = sorted(fields - set(mapping))
    if unknown:
        raise PerformanceBudgetError(
            f"{owner} contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise PerformanceBudgetError(f"{owner} is missing fields: {', '.join(missing)}")
    return mapping


def _positive_integer(value: object, *, owner: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise PerformanceBudgetError(f"{owner} must be a {qualifier} integer")
    return value


def _positive_number(value: object, *, owner: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PerformanceBudgetError(f"{owner} must be a positive number")
    normalized = float(value)
    if normalized <= 0:
        raise PerformanceBudgetError(f"{owner} must be a positive number")
    return normalized


def _string(value: object, *, owner: str) -> str:
    if type(value) is not str or not value:
        raise PerformanceBudgetError(f"{owner} must be a non-empty string")
    return value


def load_performance_budgets(
    path: Path = DEFAULT_BUDGET_PATH,
) -> PerformanceBudgets:
    raw_bytes = path.read_bytes()
    try:
        document = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as error:
        raise PerformanceBudgetError(f"cannot parse {path}: {error}") from error
    root = _closed_mapping(
        document,
        owner="performance budgets",
        fields=frozenset(
            {"version", "environment", "protocol", "companionGates", "benchmarks"}
        ),
    )
    if root["version"] != 1 or type(root["version"]) is not int:
        raise PerformanceBudgetError("performance budget version must be 1")

    environment = _closed_mapping(
        root["environment"],
        owner="performance budget environment",
        fields=frozenset({"platform", "python"}),
    )
    protocol_document = _closed_mapping(
        root["protocol"],
        owner="performance budget protocol",
        fields=frozenset(
            {"warmupRuns", "measuredRuns", "statistic", "garbageCollection"}
        ),
    )
    statistic = _string(protocol_document["statistic"], owner="protocol statistic")
    if statistic != "median":
        raise PerformanceBudgetError("protocol statistic must be 'median'")
    garbage_collection = _string(
        protocol_document["garbageCollection"],
        owner="protocol garbageCollection",
    )
    if garbage_collection != "collect-before-each-observation":
        raise PerformanceBudgetError(
            "protocol garbageCollection must be 'collect-before-each-observation'"
        )
    protocol = PerformanceProtocol(
        warmup_runs=_positive_integer(
            protocol_document["warmupRuns"],
            owner="protocol warmupRuns",
            allow_zero=True,
        ),
        measured_runs=_positive_integer(
            protocol_document["measuredRuns"],
            owner="protocol measuredRuns",
        ),
        statistic="median",
        garbage_collection="collect-before-each-observation",
    )

    companion_document = root["companionGates"]
    if type(companion_document) is not list:
        raise PerformanceBudgetError("companionGates must be an array")
    companion_gates = tuple(
        _string(value, owner=f"companionGates[{index}]")
        for index, value in enumerate(companion_document)
    )
    if not companion_gates:
        raise PerformanceBudgetError("companionGates must not be empty")

    benchmark_documents = _closed_mapping(
        root["benchmarks"],
        owner="performance benchmarks",
        fields=frozenset(_BENCHMARK_IDS),
    )
    benchmarks: list[BenchmarkBudget] = []
    for benchmark_id in _BENCHMARK_IDS:
        raw_benchmark = benchmark_documents[benchmark_id]
        if type(raw_benchmark) is not dict:
            raise PerformanceBudgetError(f"benchmark {benchmark_id} must be an object")
        allowed_fields = frozenset(
            {
                "metric",
                "sizes",
                "maximumBySize",
                "maximumNormalizedGrowth",
                "warmupRuns",
                "measuredRuns",
            }
        )
        required_fields = frozenset(
            {"metric", "sizes", "maximumBySize", "maximumNormalizedGrowth"}
        )
        unknown = sorted(set(raw_benchmark) - allowed_fields)
        missing = sorted(required_fields - set(raw_benchmark))
        if unknown:
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} contains unknown fields: "
                f"{', '.join(unknown)}"
            )
        if missing:
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} is missing fields: {', '.join(missing)}"
            )
        metric = _string(raw_benchmark["metric"], owner=f"{benchmark_id} metric")
        if metric not in _METRICS:
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} metric is unsupported"
            )
        raw_sizes = raw_benchmark["sizes"]
        if type(raw_sizes) is not list:
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} sizes must be an array"
            )
        sizes = tuple(
            _positive_integer(value, owner=f"{benchmark_id} sizes[{index}]")
            for index, value in enumerate(raw_sizes)
        )
        if len(sizes) < 2 or tuple(sorted(set(sizes))) != sizes:
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} sizes must contain at least two "
                "strictly increasing unique integers"
            )
        raw_maximums = raw_benchmark["maximumBySize"]
        if type(raw_maximums) is not dict:
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} maximumBySize must be an object"
            )
        if any(
            isinstance(key, bool) or not isinstance(key, int) for key in raw_maximums
        ):
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} maximumBySize keys must be integers"
            )
        if set(raw_maximums) != set(sizes):
            raise PerformanceBudgetError(
                f"benchmark {benchmark_id} maximumBySize keys must equal sizes"
            )
        maximum_by_size = {
            size: _positive_number(
                raw_maximums[size],
                owner=f"{benchmark_id} maximumBySize[{size}]",
            )
            for size in sizes
        }
        warmup_runs = _positive_integer(
            raw_benchmark.get("warmupRuns", protocol.warmup_runs),
            owner=f"{benchmark_id} warmupRuns",
            allow_zero=True,
        )
        measured_runs = _positive_integer(
            raw_benchmark.get("measuredRuns", protocol.measured_runs),
            owner=f"{benchmark_id} measuredRuns",
        )
        benchmarks.append(
            BenchmarkBudget(
                benchmark_id=benchmark_id,
                metric=metric,  # type: ignore[arg-type]
                sizes=sizes,
                maximum_by_size=maximum_by_size,
                maximum_normalized_growth=_positive_number(
                    raw_benchmark["maximumNormalizedGrowth"],
                    owner=f"{benchmark_id} maximumNormalizedGrowth",
                ),
                warmup_runs=warmup_runs,
                measured_runs=measured_runs,
            )
        )

    return PerformanceBudgets(
        path=path,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        platform=_string(environment["platform"], owner="environment platform"),
        python=_string(environment["python"], owner="environment python"),
        protocol=protocol,
        companion_gates=companion_gates,
        benchmarks=tuple(benchmarks),
    )


@lru_cache(maxsize=None)
def _canonical_fixture(size: int) -> tuple[Decimal, ...]:
    return tuple(Decimal(f"{index % 997}.125") for index in range(size))


def _canonical_decimal_operation(size: int) -> object:
    from graphblocks.canonical import canonical_dumps

    return canonical_dumps(_canonical_fixture(size))


def _journal_append_operation(size: int) -> object:
    from graphblocks.runtime import ExecutionJournal

    journal = ExecutionJournal(f"benchmark-journal-{size}")
    for index in range(size):
        journal.append("node_started", {"node": index})
    return journal.records[-1].sequence


@lru_cache(maxsize=None)
def _compiler_fixture(size: int) -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": f"benchmark-{size}"},
        "spec": {
            "nodes": {
                f"node-{index:06d}": {"block": "benchmark.noop@1"}
                for index in range(size)
            }
        },
    }


def _compiler_operation(size: int) -> object:
    from graphblocks.compiler import compile_graph

    plan = compile_graph(_compiler_fixture(size), allow_unknown_blocks=True)
    if not plan.ok:
        raise PerformanceBudgetError(
            f"compiler benchmark fixture {size} produced error diagnostics"
        )
    return plan.graph_hash


def _deep_size(value: object, seen: set[int] | None = None) -> int:
    import sys as _sys

    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    total = _sys.getsizeof(value)
    if isinstance(value, Mapping):
        return total + sum(
            _deep_size(key, seen) + _deep_size(item, seen)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return total + sum(_deep_size(item, seen) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return total + sum(
            _deep_size(getattr(value, field.name), seen) for field in fields(value)
        )
    return total


def _server_retained_memory_operation(size: int) -> object:
    from graphblocks.server import GraphBlocksServerApp, ServerRequest

    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "performance-memory"},
        "spec": {"nodes": {}},
    }
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        max_in_memory_runs=max(100, size + 1),
        max_in_memory_runs_per_tenant=max(100, size + 1),
    )
    try:
        for index in range(size):
            body = json.dumps(
                {
                    "graph": graph,
                    "inputs": {},
                    "runId": f"memory-run-{index:06d}",
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            response = app.handle(
                ServerRequest(
                    method="POST",
                    path="/runs",
                    headers={},
                    query={},
                    cookies={},
                    body=body,
                )
            )
            if response.status_code != 200:
                raise PerformanceBudgetError(
                    "server memory benchmark invocation failed with status "
                    f"{response.status_code}"
                )
        retained_state = (
            app._events_by_run_id,
            app._run_authorization_by_run_id,
            app._run_ids_by_tenant,
            app._run_ids_by_owner,
            app._retired_runs_by_run_id,
            app._pending_accepted_runs_by_run_id,
            app._accepted_run_results_by_run_id,
        )
        return _deep_size(retained_state)
    finally:
        app.close(timeout=0)


BENCHMARK_OPERATIONS: Mapping[str, BenchmarkOperation] = {
    "canonical-decimal-scaling": _canonical_decimal_operation,
    "journal-append-scaling": _journal_append_operation,
    "compiler-scaling": _compiler_operation,
    "server-retained-memory": _server_retained_memory_operation,
}


def _observe_benchmark(
    budget: BenchmarkBudget,
    operation: BenchmarkOperation,
) -> tuple[dict[str, object], ...]:
    observations: list[dict[str, object]] = []
    for size in budget.sizes:
        for _warmup in range(budget.warmup_runs):
            operation(size)
        samples: list[float] = []
        for _measurement in range(budget.measured_runs):
            gc.collect()
            if budget.metric == "elapsedSeconds":
                started = perf_counter()
                operation(size)
                sample = perf_counter() - started
            else:
                raw_sample = operation(size)
                if isinstance(raw_sample, bool) or not isinstance(
                    raw_sample, (int, float)
                ):
                    raise PerformanceBudgetError(
                        f"benchmark {budget.benchmark_id} returned a non-numeric sample"
                    )
                sample = float(raw_sample)
            if sample <= 0:
                raise PerformanceBudgetError(
                    f"benchmark {budget.benchmark_id} returned a non-positive sample"
                )
            samples.append(sample)
        observations.append(
            {
                "size": size,
                "samples": [round(value, 9) for value in samples],
                "value": round(float(median(samples)), 9),
                "maximum": budget.maximum_by_size[size],
            }
        )
    return tuple(observations)


def evaluate_benchmark(
    budget: BenchmarkBudget,
    observations: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    if len(observations) != len(budget.sizes):
        raise PerformanceBudgetError(
            f"benchmark {budget.benchmark_id} observation count does not match sizes"
        )
    failures: list[str] = []
    values: list[float] = []
    for expected_size, observation in zip(budget.sizes, observations, strict=True):
        size = observation.get("size")
        value = observation.get("value")
        if size != expected_size:
            raise PerformanceBudgetError(
                f"benchmark {budget.benchmark_id} observations are out of order"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise PerformanceBudgetError(
                f"benchmark {budget.benchmark_id} observation value is invalid"
            )
        normalized = float(value)
        values.append(normalized)
        maximum = budget.maximum_by_size[expected_size]
        if normalized > maximum:
            failures.append(
                f"size {expected_size} {budget.metric} {normalized:.9f} "
                f"exceeds {maximum:.9f}"
            )

    size_growth = budget.sizes[-1] / budget.sizes[0]
    normalized_growth = (values[-1] / values[0]) / size_growth
    if normalized_growth > budget.maximum_normalized_growth:
        failures.append(
            f"normalized growth {normalized_growth:.6f} exceeds "
            f"{budget.maximum_normalized_growth:.6f}"
        )
    return tuple(failures)


def run_performance_budgets(
    budgets: PerformanceBudgets,
    *,
    operations: Mapping[str, BenchmarkOperation] = BENCHMARK_OPERATIONS,
) -> dict[str, object]:
    if set(operations) != set(_BENCHMARK_IDS):
        raise PerformanceBudgetError(
            "benchmark operation inventory does not match the closed budget inventory"
        )
    results: list[dict[str, object]] = []
    all_failures: list[str] = []
    for budget in budgets.benchmarks:
        observations = _observe_benchmark(budget, operations[budget.benchmark_id])
        failures = evaluate_benchmark(budget, observations)
        all_failures.extend(f"{budget.benchmark_id}: {failure}" for failure in failures)
        results.append(
            {
                "id": budget.benchmark_id,
                "metric": budget.metric,
                "warmupRuns": budget.warmup_runs,
                "measuredRuns": budget.measured_runs,
                "maximumNormalizedGrowth": budget.maximum_normalized_growth,
                "observations": list(observations),
                "passed": not failures,
                "failures": list(failures),
            }
        )
    try:
        budget_path = budgets.path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        budget_path = str(budgets.path.resolve())
    return {
        "schemaVersion": 1,
        "budgetPath": budget_path,
        "budgetSha256": budgets.sha256,
        "environment": {
            "platform": platform.system().lower(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "implementation": platform.python_implementation(),
        },
        "protocol": {
            "statistic": budgets.protocol.statistic,
            "garbageCollection": budgets.protocol.garbage_collection,
        },
        "companionGates": list(budgets.companion_gates),
        "benchmarks": results,
        "passed": not all_failures,
        "failures": all_failures,
    }


def _check_environment(budgets: PerformanceBudgets) -> None:
    current_platform = platform.system().lower()
    current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if current_platform != budgets.platform or current_python != budgets.python:
        raise PerformanceBudgetError(
            "performance budgets require "
            f"{budgets.platform}/Python {budgets.python}, got "
            f"{current_platform}/Python {current_python}"
        )


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enforce GraphBlocks deterministic Python performance budgets."
    )
    parser.add_argument("--budget", type=Path, default=DEFAULT_BUDGET_PATH)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--list", action="store_true", dest="list_benchmarks")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        budgets = load_performance_budgets(args.budget)
        if args.list_benchmarks:
            print("\n".join(item.benchmark_id for item in budgets.benchmarks))
            return 0
        if args.validate_only:
            print(
                "performance budget contract passed: "
                f"{len(budgets.benchmarks)} benchmarks, sha256:{budgets.sha256}"
            )
            return 0
        _check_environment(budgets)
        report = run_performance_budgets(budgets)
        if args.report is not None:
            _write_report(args.report, report)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] is True else 1
    except (OSError, PerformanceBudgetError) as error:
        print(f"performance budget error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
