from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from graphblocks.canonical import canonical_hash
from graphblocks.compiler import compile_graph
from graphblocks.run_store import (
    InMemoryRunStore,
    RunRecord,
    RunTerminalStateError,
    SQLiteRunStore,
    StateConflictError,
)
from graphblocks.runtime import (
    CancellationToken,
    ExecutionJournal,
    InProcessRuntime,
    JournalRecord,
    JournalStateError,
    RunResult,
    RuntimeRegistry,
    SQLiteExecutionJournal,
    stdlib_registry,
)


TckCaseKind = Literal["compiler", "runtime"]
TckResultStatus = Literal["passed", "failed"]


@dataclass(frozen=True, slots=True)
class TckCase:
    case_id: str
    kind: TckCaseKind
    graph: dict[str, object]
    inputs: dict[str, object] = field(default_factory=dict)
    expected_hash: str | None = None
    expected_outputs: dict[str, object] | None = None
    expected_ok: bool = True
    expected_status: str = "succeeded"

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("TCK case_id must not be empty")
        if self.kind not in {"compiler", "runtime"}:
            raise ValueError(f"invalid TCK case kind {self.kind}")
        object.__setattr__(self, "graph", dict(self.graph))
        object.__setattr__(self, "inputs", dict(self.inputs))
        if self.expected_outputs is not None:
            object.__setattr__(self, "expected_outputs", dict(self.expected_outputs))

    @classmethod
    def compiler(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        expected_hash: str | None = None,
        expected_ok: bool = True,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="compiler",
            graph=graph,
            expected_hash=expected_hash,
            expected_ok=expected_ok,
        )

    @classmethod
    def runtime(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        inputs: dict[str, object],
        expected_outputs: dict[str, object] | None = None,
        expected_status: str = "succeeded",
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="runtime",
            graph=graph,
            inputs=inputs,
            expected_outputs=expected_outputs,
            expected_status=expected_status,
        )


@dataclass(frozen=True, slots=True)
class TckResult:
    case_id: str
    kind: TckCaseKind
    status: TckResultStatus
    diagnostics: tuple[dict[str, str], ...] = field(default_factory=tuple)
    observed: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(dict(diagnostic) for diagnostic in self.diagnostics))
        object.__setattr__(self, "observed", dict(self.observed))

    def result_contract(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "status": self.status,
            "diagnostics": [dict(diagnostic) for diagnostic in self.diagnostics],
            "observed": dict(self.observed),
        }


@dataclass(frozen=True, slots=True)
class TckReport:
    profile: str
    results: tuple[TckResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.status == "passed" for result in self.results)

    def report_contract(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "ok": self.ok,
            "results": [result.result_contract() for result in self.results],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.report_contract())


@dataclass(frozen=True, slots=True)
class TckRunner:
    registry: RuntimeRegistry
    profile: str = "local"

    def run_cases(self, cases: tuple[TckCase, ...]) -> TckReport:
        results: list[TckResult] = []
        for case in cases:
            if case.kind == "compiler":
                results.append(self._run_compiler_case(case))
            else:
                results.append(self._run_runtime_case(case))
        return TckReport(profile=self.profile, results=tuple(results))

    def _run_compiler_case(self, case: TckCase) -> TckResult:
        plan = compile_graph(case.graph)
        observed = {"hash": plan.graph_hash, "ok": plan.ok}
        diagnostics: list[dict[str, str]] = []
        if plan.ok != case.expected_ok:
            diagnostics.append(
                {
                    "code": "CompilerOkMismatch",
                    "message": "compiler ok value did not match expected result",
                    "path": "$.expected_ok",
                }
            )
        if case.expected_hash is not None and plan.graph_hash != case.expected_hash:
            diagnostics.append(
                {
                    "code": "HashMismatch",
                    "message": "compiler graph hash did not match expected hash",
                    "path": "$.expected_hash",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_runtime_case(self, case: TckCase) -> TckResult:
        try:
            result = InProcessRuntime(self.registry).run(case.graph, case.inputs)
            observed = {
                "status": result.status,
                "outputs": result.outputs,
                "terminal_kind": result.journal.terminal_kind,
            }
        except Exception as error:  # pragma: no cover - exercised by conformance fixtures.
            observed = {"status": "error", "error": type(error).__name__, "message": str(error)}
        diagnostics: list[dict[str, str]] = []
        if observed.get("status") != case.expected_status:
            diagnostics.append(
                {
                    "code": "StatusMismatch",
                    "message": "runtime status did not match expected status",
                    "path": "$.expected_status",
                }
            )
        if case.expected_outputs is not None and observed.get("outputs") != case.expected_outputs:
            diagnostics.append(
                {
                    "code": "OutputMismatch",
                    "message": "runtime outputs did not match expected outputs",
                    "path": "$.expected_outputs",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )


__all__ = [
    "CancellationToken",
    "ExecutionJournal",
    "InMemoryRunStore",
    "InProcessRuntime",
    "JournalRecord",
    "JournalStateError",
    "RunRecord",
    "RunTerminalStateError",
    "RunResult",
    "RuntimeRegistry",
    "SQLiteExecutionJournal",
    "SQLiteRunStore",
    "StateConflictError",
    "TckCase",
    "TckReport",
    "TckResult",
    "TckRunner",
    "compile_graph",
    "stdlib_registry",
]
