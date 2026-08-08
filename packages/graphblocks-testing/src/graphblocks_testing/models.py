"""Core TCK cases, reports, and evidence models."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import math
from pathlib import Path
from types import MappingProxyType
from typing import Literal, get_args


from graphblocks.canonical import (
    canonical_loads_reference as canonical_loads,
)
from graphblocks.tools import (
    ToolExecutionPlanError,
)

_STABLE_TCK_SUITE_DIAGNOSTIC_CODES = {
    "compiler": "GB3001",
    "schema": "GB3002",
    "runtime": "GB3003",
    "application-events": "GB3004",
    "retry": "GB3005",
    "sequence": "GB3006",
    "tool-execution": "GB3007",
    "tool-lifecycle": "GB3008",
    "tool-result": "GB3009",
}


def run_native_test_graph(
    graph: dict[str, object],
    inputs: dict[str, object],
    node_outputs: dict[str, object],
    *,
    run_id: str | None = None,
    run_store_path: str | None = None,
    journal_store_path: str | None = None,
) -> dict[str, object]:
    from graphblocks_runtime import run_test_graph

    options: dict[str, object] = {}
    if run_id is not None:
        options["run_id"] = run_id
    if run_store_path is not None:
        options["run_store_path"] = run_store_path
    if journal_store_path is not None:
        options["journal_store_path"] = journal_store_path
    return run_test_graph(graph, inputs, node_outputs, **options)


TckCaseKind = Literal[
    "compiler",
    "runtime",
    "schema",
    "policy",
    "approval-review",
    "application-events",
    "application-protocol",
    "sequence",
    "exhaustion",
    "budget-race",
    "conversation",
    "documents",
    "deployment",
    "durable",
    "migration",
    "orchestration",
    "rag",
    "retry",
    "tool-lifecycle",
    "tool-execution",
    "tool-result",
    "usage",
    "voice",
]
TckResultStatus = Literal["passed", "failed"]
_TCK_CASE_KINDS = frozenset(get_args(TckCaseKind))
_TCK_RESULT_STATUSES = frozenset(get_args(TckResultStatus))
PerformanceThresholdOperator = Literal["at_most", "at_least"]
MigrationDirection = Literal["upgrade", "downgrade"]
FaultKind = Literal[
    "telemetry_outage",
    "provider_timeout",
    "worker_crash",
    "budget_race",
    "storage_conflict",
    "network_partition",
]
ReleaseCandidateGateStatus = Literal["passed", "failed"]

_BUNDLED_TCK_SUITES = (
    "application-events",
    "compiler",
    "retry",
    "runtime",
    "schema",
    "sequence",
    "tool-execution",
    "tool-lifecycle",
    "tool-result",
)
_STABLE_RELEASE_PROFILES = ("GB-C0-SCHEMA", "GB-C1-LOCAL-RUNTIME")


def _first_mapping_value(
    mapping: Mapping[str, object], *keys: str, default: object = None
) -> object:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _load_tck_cases_json(path: str | Path, suite_label: str) -> object:
    try:
        return canonical_loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(
            f"{suite_label} TCK cases must be valid strict JSON"
        ) from error


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value or ())


def _tool_execution_error_code(error: ToolExecutionPlanError) -> str:
    message = str(error)
    if "requires an effect key" in message or "share effect key" in message:
        return "unsafe_parallel_effects"
    if (
        "duplicate dependency" in message
        or "dependency ids must not contain duplicates" in message
    ):
        return "duplicate_dependency"
    if "not pending" in message:
        return "tool_call_not_pending"
    if "not running" in message:
        return "tool_call_not_running"
    if "already running" in message:
        return "effect_conflict"
    if "maximum parallelism" in message:
        return "parallelism_exhausted"
    if "dependencies are not ready" in message:
        return "dependencies_not_ready"
    return type(error).__name__


_MAX_TCK_EVIDENCE_DEPTH = 64


# Base ``dict`` descriptors bypass subclass mutator overrides, so these values
# are disposable public views; canonical evidence is stored in mapping proxies.
class _FrozenEvidenceDict(dict[str, object]):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("TCK evidence is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __ior__(self, _other: object) -> _FrozenEvidenceDict:
        self._immutable()
        return self

    def __copy__(self) -> _FrozenEvidenceDict:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, object]:
        return deepcopy(dict(self), memo)

    def __reduce__(self) -> tuple[object, tuple[dict[str, object]]]:
        return (_FrozenEvidenceDict, (dict(self),))


class _FrozenEvidenceList(tuple[object, ...]):
    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = None  # type: ignore[assignment]


def _freeze_tck_evidence(
    value: object,
    *,
    _active_containers: set[int] | None = None,
    _depth: int = 0,
) -> object:
    if _depth > _MAX_TCK_EVIDENCE_DEPTH:
        raise ValueError(
            f"TCK evidence nesting must not exceed {_MAX_TCK_EVIDENCE_DEPTH} levels"
        )
    active_containers = set() if _active_containers is None else _active_containers
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("TCK evidence must not contain cycles")
        active_containers.add(identity)
        try:
            frozen: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("TCK evidence mappings require string keys")
                frozen[key] = _freeze_tck_evidence(
                    item,
                    _active_containers=active_containers,
                    _depth=_depth + 1,
                )
            return MappingProxyType(frozen)
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("TCK evidence must not contain cycles")
        active_containers.add(identity)
        try:
            return _FrozenEvidenceList(
                _freeze_tck_evidence(
                    item,
                    _active_containers=active_containers,
                    _depth=_depth + 1,
                )
                for item in value
            )
        finally:
            active_containers.remove(identity)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("TCK evidence numbers must be finite")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("TCK evidence numbers must be finite")
    if value is None or isinstance(value, (bool, int, float, str, Decimal)):
        return value
    raise ValueError(
        f"TCK evidence contains unsupported value type {type(value).__name__}"
    )


def _materialize_tck_evidence(value: object, *, mutable: bool) -> object:
    if isinstance(value, Mapping):
        mapping = {
            key: _materialize_tck_evidence(item, mutable=mutable)
            for key, item in value.items()
        }
        return mapping if mutable else _FrozenEvidenceDict(mapping)
    if isinstance(value, tuple):
        items = tuple(
            _materialize_tck_evidence(item, mutable=mutable) for item in value
        )
        return list(items) if mutable else _FrozenEvidenceList(items)
    return value


class _FrozenCaseEvidenceList(list[object]):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("TCK case evidence is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __copy__(self) -> _FrozenCaseEvidenceList:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> list[object]:
        return deepcopy(list(self), memo)

    def __reduce__(self) -> tuple[object, tuple[list[object]]]:
        return (_FrozenCaseEvidenceList, (list(self),))


def _materialize_tck_case_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenEvidenceDict(
            {key: _materialize_tck_case_evidence(item) for key, item in value.items()}
        )
    if isinstance(value, tuple):
        return _FrozenCaseEvidenceList(
            _materialize_tck_case_evidence(item) for item in value
        )
    return value


_TCK_CASE_EVIDENCE_FIELDS = frozenset(
    {
        "graph",
        "inputs",
        "native_node_outputs",
        "expected_outputs",
        "block_catalog",
        "schema_value",
        "expected_canonical_value",
        "expected_resource_errors",
        "policy_delivery",
        "policy_operations",
        "expected_gate_state",
        "application_event_operations",
        "expected_application_event_diagnostics",
        "application_protocol_fixture",
        "sequence_operations",
        "exhaustion_fixture",
        "budget_race_fixture",
        "conversation_fixture",
        "documents_fixture",
        "deployment_fixture",
        "durable_fixture",
        "migration_fixture",
        "orchestration_fixture",
        "rag_fixture",
        "retry_fixture",
        "tool_lifecycle_fixture",
        "tool_execution_fixture",
        "tool_result_fixture",
        "usage_fixture",
        "voice_fixture",
        "approval_review_fixture",
    }
)


@dataclass(frozen=True, slots=True)
class TckCase:
    case_id: str
    kind: TckCaseKind
    graph: dict[str, object] = field(default_factory=dict)
    inputs: dict[str, object] = field(default_factory=dict)
    native_node_outputs: dict[str, object] = field(default_factory=dict)
    expected_hash: str | None = None
    expected_error_codes: tuple[str, ...] = field(default_factory=tuple)
    expected_warning_codes: tuple[str, ...] = field(default_factory=tuple)
    expected_outputs: dict[str, object] | None = None
    expected_ok: bool = True
    expected_status: str = "succeeded"
    expected_terminal_kind: str | None = None
    block_catalog: tuple[dict[str, object], ...] = field(default_factory=tuple)
    allow_unknown_blocks: bool = False
    schema_id: str | None = None
    schema_case_type: str = "schema_id"
    schema_value: object | None = None
    expected_canonical_schema_id: str | None = None
    expected_schema_name: str | None = None
    expected_major_version: int | None = None
    expected_canonical_value: dict[str, object] | None = None
    expected_canonical_json: str | None = None
    expected_error: str | None = None
    expected_resource_errors: tuple[dict[str, str], ...] = field(default_factory=tuple)
    policy_delivery: dict[str, object] = field(default_factory=dict)
    policy_operations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    expected_gate_state: dict[str, object] = field(default_factory=dict)
    policy_stream_id: str = "stream-1"
    policy_response_id: str = "response-1"
    application_event_operations: tuple[dict[str, object], ...] = field(
        default_factory=tuple
    )
    expected_accepted_event_kinds: tuple[str, ...] = field(default_factory=tuple)
    expected_application_event_diagnostics: tuple[dict[str, str], ...] = field(
        default_factory=tuple
    )
    application_protocol_fixture: dict[str, object] = field(default_factory=dict)
    sequence_capacity: int | None = None
    sequence_operations: tuple[dict[str, object], ...] = field(default_factory=tuple)
    expected_sequence_state: str | None = None
    expected_sequence_creation_error: str | None = None
    exhaustion_fixture: dict[str, object] = field(default_factory=dict)
    budget_race_fixture: dict[str, object] = field(default_factory=dict)
    conversation_fixture: dict[str, object] = field(default_factory=dict)
    documents_fixture: dict[str, object] = field(default_factory=dict)
    deployment_fixture: dict[str, object] = field(default_factory=dict)
    durable_fixture: dict[str, object] = field(default_factory=dict)
    migration_fixture: dict[str, object] = field(default_factory=dict)
    orchestration_fixture: dict[str, object] = field(default_factory=dict)
    rag_fixture: dict[str, object] = field(default_factory=dict)
    retry_fixture: dict[str, object] = field(default_factory=dict)
    tool_lifecycle_fixture: dict[str, object] = field(default_factory=dict)
    tool_execution_fixture: dict[str, object] = field(default_factory=dict)
    tool_result_fixture: dict[str, object] = field(default_factory=dict)
    usage_fixture: dict[str, object] = field(default_factory=dict)
    voice_fixture: dict[str, object] = field(default_factory=dict)
    approval_review_fixture: dict[str, object] = field(default_factory=dict)
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    def __getattribute__(self, name: str) -> object:
        value = object.__getattribute__(self, name)
        if name in _TCK_CASE_EVIDENCE_FIELDS and object.__getattribute__(
            self, "_sealed"
        ):
            frozen = _freeze_tck_evidence(value)
            if isinstance(value, tuple):
                return tuple(_materialize_tck_case_evidence(item) for item in frozen)
            return _materialize_tck_case_evidence(frozen)
        return value

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("TCK case_id must not be empty")
        if self.kind not in {
            "compiler",
            "runtime",
            "schema",
            "policy",
            "approval-review",
            "application-events",
            "application-protocol",
            "sequence",
            "exhaustion",
            "budget-race",
            "conversation",
            "documents",
            "deployment",
            "durable",
            "migration",
            "orchestration",
            "rag",
            "retry",
            "tool-lifecycle",
            "tool-execution",
            "tool-result",
            "usage",
            "voice",
        }:
            raise ValueError(f"invalid TCK case kind {self.kind}")
        object.__setattr__(self, "graph", deepcopy(dict(self.graph)))
        object.__setattr__(self, "inputs", deepcopy(dict(self.inputs)))
        object.__setattr__(
            self,
            "native_node_outputs",
            deepcopy(dict(self.native_node_outputs)),
        )
        object.__setattr__(
            self, "expected_error_codes", tuple(self.expected_error_codes)
        )
        object.__setattr__(
            self, "expected_warning_codes", tuple(self.expected_warning_codes)
        )
        object.__setattr__(
            self,
            "block_catalog",
            tuple(deepcopy(dict(block)) for block in self.block_catalog),
        )
        if not isinstance(self.allow_unknown_blocks, bool):
            raise TypeError("TCK allow_unknown_blocks must be a boolean")
        object.__setattr__(
            self,
            "policy_delivery",
            deepcopy(dict(self.policy_delivery)),
        )
        object.__setattr__(
            self,
            "policy_operations",
            tuple(deepcopy(dict(operation)) for operation in self.policy_operations),
        )
        object.__setattr__(
            self,
            "expected_gate_state",
            deepcopy(dict(self.expected_gate_state)),
        )
        object.__setattr__(
            self,
            "application_event_operations",
            tuple(
                deepcopy(dict(operation))
                for operation in self.application_event_operations
            ),
        )
        object.__setattr__(
            self,
            "expected_accepted_event_kinds",
            tuple(self.expected_accepted_event_kinds),
        )
        decoded_application_event_diagnostics: list[dict[str, str]] = []
        for diagnostic_index, diagnostic in enumerate(
            self.expected_application_event_diagnostics
        ):
            if not isinstance(diagnostic, Mapping) or set(diagnostic) != {
                "code",
                "message",
                "path",
            }:
                raise TypeError(
                    "TCK expected application event diagnostic "
                    f"{diagnostic_index} must contain exactly code, message, and path"
                )
            if not all(
                type(diagnostic[key]) is str and bool(diagnostic[key])
                for key in ("code", "message", "path")
            ):
                raise TypeError(
                    "TCK expected application event diagnostic "
                    f"{diagnostic_index} values must be non-empty strings"
                )
            decoded_application_event_diagnostics.append(
                {
                    "code": diagnostic["code"],
                    "message": diagnostic["message"],
                    "path": diagnostic["path"],
                }
            )
        object.__setattr__(
            self,
            "expected_application_event_diagnostics",
            tuple(decoded_application_event_diagnostics),
        )
        object.__setattr__(
            self,
            "application_protocol_fixture",
            deepcopy(dict(self.application_protocol_fixture)),
        )
        object.__setattr__(
            self,
            "sequence_operations",
            tuple(deepcopy(dict(operation)) for operation in self.sequence_operations),
        )
        for field_name in (
            "exhaustion_fixture",
            "budget_race_fixture",
            "conversation_fixture",
            "documents_fixture",
            "deployment_fixture",
            "durable_fixture",
            "migration_fixture",
            "orchestration_fixture",
            "rag_fixture",
            "retry_fixture",
            "tool_lifecycle_fixture",
            "tool_execution_fixture",
            "tool_result_fixture",
            "usage_fixture",
            "voice_fixture",
            "approval_review_fixture",
        ):
            object.__setattr__(
                self,
                field_name,
                deepcopy(dict(object.__getattribute__(self, field_name))),
            )
        if self.kind == "policy":
            if not self.policy_stream_id.strip():
                raise ValueError("policy TCK stream_id must not be empty")
            if not self.policy_response_id.strip():
                raise ValueError("policy TCK response_id must not be empty")
        if self.kind == "sequence":
            if self.sequence_capacity is None or isinstance(
                self.sequence_capacity, bool
            ):
                raise ValueError("sequence TCK case requires integer capacity")
            if not isinstance(self.sequence_capacity, int):
                raise ValueError("sequence TCK case requires integer capacity")
            if (
                self.expected_sequence_state is None
                and self.expected_sequence_creation_error is None
            ):
                raise ValueError(
                    "sequence TCK case requires expected state or creation error"
                )
        if (
            self.kind == "application-protocol"
            and not self.application_protocol_fixture
        ):
            raise ValueError("application-protocol TCK case requires fixture")
        if self.kind == "exhaustion" and not self.exhaustion_fixture:
            raise ValueError("exhaustion TCK case requires fixture")
        if self.kind == "budget-race" and not self.budget_race_fixture:
            raise ValueError("budget-race TCK case requires fixture")
        if self.kind == "conversation" and not self.conversation_fixture:
            raise ValueError("conversation TCK case requires fixture")
        if self.kind == "documents" and not self.documents_fixture:
            raise ValueError("documents TCK case requires fixture")
        if self.kind == "deployment" and not self.deployment_fixture:
            raise ValueError("deployment TCK case requires fixture")
        if self.kind == "durable" and not self.durable_fixture:
            raise ValueError("durable TCK case requires fixture")
        if self.kind == "migration" and not self.migration_fixture:
            raise ValueError("migration TCK case requires fixture")
        if self.kind == "orchestration" and not self.orchestration_fixture:
            raise ValueError("orchestration TCK case requires fixture")
        if self.kind == "rag" and not self.rag_fixture:
            raise ValueError("rag TCK case requires fixture")
        if self.kind == "retry" and not self.retry_fixture:
            raise ValueError("retry TCK case requires fixture")
        if self.kind == "tool-lifecycle" and not self.tool_lifecycle_fixture:
            raise ValueError("tool-lifecycle TCK case requires fixture")
        if self.kind == "tool-execution" and not self.tool_execution_fixture:
            raise ValueError("tool-execution TCK case requires fixture")
        if self.kind == "tool-result" and not self.tool_result_fixture:
            raise ValueError("tool-result TCK case requires fixture")
        if self.kind == "usage" and not self.usage_fixture:
            raise ValueError("usage TCK case requires fixture")
        if self.kind == "voice" and not self.voice_fixture:
            raise ValueError("voice TCK case requires fixture")
        if self.kind == "approval-review" and not self.approval_review_fixture:
            raise ValueError("approval-review TCK case requires fixture")
        if self.expected_outputs is not None:
            object.__setattr__(
                self,
                "expected_outputs",
                deepcopy(dict(self.expected_outputs)),
            )
        if (
            self.expected_terminal_kind is not None
            and not self.expected_terminal_kind.strip()
        ):
            raise ValueError("TCK expected_terminal_kind must not be empty")
        object.__setattr__(
            self,
            "expected_resource_errors",
            tuple(deepcopy(dict(error)) for error in self.expected_resource_errors),
        )
        object.__setattr__(self, "schema_value", deepcopy(self.schema_value))
        if self.expected_canonical_value is not None:
            object.__setattr__(
                self,
                "expected_canonical_value",
                deepcopy(dict(self.expected_canonical_value)),
            )
        if self.kind == "schema":
            if self.schema_case_type not in {"schema_id", "typed_value", "resource"}:
                raise ValueError(
                    "schema TCK case_type must be schema_id, typed_value, or resource"
                )
            if self.schema_case_type != "resource" and (
                not isinstance(self.schema_id, str) or not self.schema_id.strip()
            ):
                raise ValueError("schema TCK case requires schema_id")
            if self.schema_case_type == "typed_value" and self.expected_ok:
                if self.expected_canonical_value is None:
                    raise ValueError(
                        "typed value schema TCK case requires expected_canonical_value"
                    )
                if (
                    not isinstance(self.expected_canonical_json, str)
                    or not self.expected_canonical_json
                ):
                    raise ValueError(
                        "typed value schema TCK case requires expected_canonical_json"
                    )
            if (
                self.expected_major_version is not None
                and self.expected_major_version <= 0
            ):
                raise ValueError("schema TCK expected_major_version must be positive")
        object.__setattr__(self, "_sealed", True)

    @classmethod
    def compiler(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        expected_hash: str | None = None,
        expected_error_codes: tuple[str, ...] = (),
        expected_warning_codes: tuple[str, ...] = (),
        expected_ok: bool = True,
        block_catalog: tuple[dict[str, object], ...] = (),
        allow_unknown_blocks: bool | None = None,
    ) -> TckCase:
        if allow_unknown_blocks is None:
            allow_unknown_blocks = not block_catalog
        return cls(
            case_id=case_id,
            kind="compiler",
            graph=graph,
            expected_hash=expected_hash,
            expected_error_codes=expected_error_codes,
            expected_warning_codes=expected_warning_codes,
            expected_ok=expected_ok,
            block_catalog=block_catalog,
            allow_unknown_blocks=allow_unknown_blocks,
        )

    @classmethod
    def runtime(
        cls,
        *,
        case_id: str,
        graph: dict[str, object],
        inputs: dict[str, object],
        native_node_outputs: dict[str, object] | None = None,
        expected_outputs: dict[str, object] | None = None,
        expected_status: str = "succeeded",
        expected_terminal_kind: str | None = None,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="runtime",
            graph=graph,
            inputs=inputs,
            native_node_outputs={}
            if native_node_outputs is None
            else native_node_outputs,
            expected_outputs=expected_outputs,
            expected_status=expected_status,
            expected_terminal_kind=expected_terminal_kind,
        )

    @classmethod
    def schema(
        cls,
        *,
        case_id: str,
        schema_id: str | None,
        expected_ok: bool,
        schema_case_type: str = "schema_id",
        schema_value: object | None = None,
        expected_canonical_schema_id: str | None = None,
        expected_schema_name: str | None = None,
        expected_major_version: int | None = None,
        expected_canonical_value: dict[str, object] | None = None,
        expected_canonical_json: str | None = None,
        expected_error: str | None = None,
        expected_resource_errors: tuple[dict[str, str], ...] = (),
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="schema",
            schema_id=schema_id,
            schema_case_type=schema_case_type,
            schema_value=schema_value,
            expected_ok=expected_ok,
            expected_canonical_schema_id=expected_canonical_schema_id,
            expected_schema_name=expected_schema_name,
            expected_major_version=expected_major_version,
            expected_canonical_value=expected_canonical_value,
            expected_canonical_json=expected_canonical_json,
            expected_error=expected_error,
            expected_resource_errors=expected_resource_errors,
        )

    @classmethod
    def policy(
        cls,
        *,
        case_id: str,
        delivery: dict[str, object],
        operations: tuple[dict[str, object], ...],
        expected: dict[str, object],
        stream_id: str = "stream-1",
        response_id: str = "response-1",
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="policy",
            policy_delivery=delivery,
            policy_operations=operations,
            expected_gate_state=expected,
            policy_stream_id=stream_id,
            policy_response_id=response_id,
        )

    @classmethod
    def application_events(
        cls,
        *,
        case_id: str,
        operations: tuple[dict[str, object], ...],
        expected_accepted_kinds: tuple[str, ...],
        expected_diagnostics: tuple[dict[str, str], ...] = (),
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="application-events",
            application_event_operations=operations,
            expected_accepted_event_kinds=expected_accepted_kinds,
            expected_application_event_diagnostics=expected_diagnostics,
        )

    @classmethod
    def application_protocol(
        cls, *, case_id: str, fixture: dict[str, object]
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="application-protocol",
            application_protocol_fixture=fixture,
        )

    @classmethod
    def sequence(
        cls,
        *,
        case_id: str,
        capacity: int,
        operations: tuple[dict[str, object], ...],
        expected_state: str | None = None,
        expected_creation_error: str | None = None,
    ) -> TckCase:
        return cls(
            case_id=case_id,
            kind="sequence",
            sequence_capacity=capacity,
            sequence_operations=operations,
            expected_sequence_state=expected_state,
            expected_sequence_creation_error=expected_creation_error,
        )

    @classmethod
    def exhaustion(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="exhaustion", exhaustion_fixture=fixture)

    @classmethod
    def budget_race(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="budget-race", budget_race_fixture=fixture)

    @classmethod
    def conversation(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="conversation", conversation_fixture=fixture)

    @classmethod
    def documents(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="documents", documents_fixture=fixture)

    @classmethod
    def deployment(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="deployment", deployment_fixture=fixture)

    @classmethod
    def durable(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="durable", durable_fixture=fixture)

    @classmethod
    def migration(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="migration", migration_fixture=fixture)

    @classmethod
    def orchestration(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="orchestration", orchestration_fixture=fixture)

    @classmethod
    def rag(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="rag", rag_fixture=fixture)

    @classmethod
    def retry(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="retry", retry_fixture=fixture)

    @classmethod
    def tool_lifecycle(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(
            case_id=case_id, kind="tool-lifecycle", tool_lifecycle_fixture=fixture
        )

    @classmethod
    def tool_execution(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(
            case_id=case_id, kind="tool-execution", tool_execution_fixture=fixture
        )

    @classmethod
    def tool_result(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="tool-result", tool_result_fixture=fixture)

    @classmethod
    def usage(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="usage", usage_fixture=fixture)

    @classmethod
    def voice(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(case_id=case_id, kind="voice", voice_fixture=fixture)

    @classmethod
    def approval_review(cls, *, case_id: str, fixture: dict[str, object]) -> TckCase:
        return cls(
            case_id=case_id, kind="approval-review", approval_review_fixture=fixture
        )
