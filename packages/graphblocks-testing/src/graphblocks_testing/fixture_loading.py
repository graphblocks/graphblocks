"""TCK fixture loaders, suite discovery, and bundled inventories."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
from pathlib import Path


from graphblocks.canonical import (
    canonical_hash_reference as canonical_hash,
)
from graphblocks.compiler import MAX_NODE_RETRY_ATTEMPTS
from graphblocks.duration import parse_duration_milliseconds
from graphblocks.runtime import (
    core_stdlib_registry,
    RuntimeRegistry,
    stdlib_registry,
)

from .durable_contracts import DURABLE_CASE_KINDS
from .models import (
    TckCase,
    _BUNDLED_TCK_SUITES,
    _first_mapping_value,
    _load_tck_cases_json,
)
from .reports import TckSuiteManifest


def load_compiler_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "compiler")
    if not isinstance(raw_cases, list):
        raise ValueError("compiler TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"compiler TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"compiler TCK case {index} requires name")
        graph = _first_mapping_value(raw_case, "document", "graph")
        if not isinstance(graph, dict):
            raise ValueError(f"compiler TCK case {case_id} requires document")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"compiler TCK case {case_id} requires expected result")
        expected_hash = _first_mapping_value(expected, "graph_hash", "graphHash")
        if not isinstance(expected_hash, str) or not expected_hash.strip():
            raise ValueError(
                f"compiler TCK case {case_id} requires expected graph_hash"
            )
        raw_error_codes = _first_mapping_value(expected, "error_codes", "errorCodes")
        if not isinstance(raw_error_codes, list) or not all(
            isinstance(code, str) for code in raw_error_codes
        ):
            raise ValueError(f"compiler TCK case {case_id} requires string error_codes")
        raw_warning_codes = _first_mapping_value(
            expected, "warning_codes", "warningCodes", default=[]
        )
        if not isinstance(raw_warning_codes, list) or not all(
            isinstance(code, str) for code in raw_warning_codes
        ):
            raise ValueError(
                f"compiler TCK case {case_id} requires string warning_codes"
            )
        raw_block_catalog = _first_mapping_value(
            raw_case, "block_catalog", "blockCatalog", default=[]
        )
        if not isinstance(raw_block_catalog, list) or not all(
            isinstance(block, dict) for block in raw_block_catalog
        ):
            raise ValueError(
                f"compiler TCK case {case_id} block_catalog must be a list of mappings"
            )
        allow_unknown_blocks = (
            "block_catalog" not in raw_case and "blockCatalog" not in raw_case
        )
        cases.append(
            TckCase.compiler(
                case_id=case_id,
                graph=graph,
                expected_hash=expected_hash,
                expected_error_codes=tuple(raw_error_codes),
                expected_warning_codes=tuple(raw_warning_codes),
                expected_ok=not raw_error_codes,
                block_catalog=tuple(raw_block_catalog),
                allow_unknown_blocks=allow_unknown_blocks,
            )
        )
    return tuple(cases)


def load_runtime_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "runtime")
    if not isinstance(raw_cases, list):
        raise ValueError("runtime TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"runtime TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"runtime TCK case {index} requires name")
        graph = _first_mapping_value(raw_case, "document", "graph")
        if not isinstance(graph, dict):
            raise ValueError(f"runtime TCK case {case_id} requires document")
        inputs = raw_case.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ValueError(f"runtime TCK case {case_id} inputs must be a mapping")
        native_node_outputs = _first_mapping_value(
            raw_case, "native_node_outputs", "nativeNodeOutputs", default={}
        )
        if not isinstance(native_node_outputs, dict):
            raise ValueError(
                f"runtime TCK case {case_id} nativeNodeOutputs must be a mapping"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"runtime TCK case {case_id} requires expected result")
        expected_status = _first_mapping_value(
            expected,
            "status",
            "expected_status",
            "expectedStatus",
            default="succeeded",
        )
        if not isinstance(expected_status, str) or not expected_status.strip():
            raise ValueError(f"runtime TCK case {case_id} requires expected status")
        expected_outputs = _first_mapping_value(
            expected,
            "outputs",
            "expected_outputs",
            "expectedOutputs",
        )
        if expected_outputs is not None and not isinstance(expected_outputs, dict):
            raise ValueError(
                f"runtime TCK case {case_id} expected outputs must be a mapping"
            )
        expected_terminal_kind = _first_mapping_value(
            expected,
            "terminal_kind",
            "terminalKind",
            "expected_terminal_kind",
            "expectedTerminalKind",
        )
        if expected_terminal_kind is not None and (
            not isinstance(expected_terminal_kind, str)
            or not expected_terminal_kind.strip()
        ):
            raise ValueError(
                f"runtime TCK case {case_id} expected terminal_kind must be a string"
            )
        expected_journal_kinds = _first_mapping_value(
            expected,
            "journal_kinds",
            "journalKinds",
            "expected_journal_kinds",
            "expectedJournalKinds",
            default=[],
        )
        if not isinstance(expected_journal_kinds, list) or any(
            not isinstance(kind, str) or not kind.strip()
            for kind in expected_journal_kinds
        ):
            raise ValueError(
                f"runtime TCK case {case_id} expected journal_kinds must be a list of strings"
            )
        cases.append(
            TckCase.runtime(
                case_id=case_id,
                graph=graph,
                inputs=inputs,
                native_node_outputs=native_node_outputs,
                expected_outputs=expected_outputs,
                expected_status=expected_status,
                expected_terminal_kind=expected_terminal_kind,
                expected_journal_kinds=tuple(expected_journal_kinds),
            )
        )
    return tuple(cases)


def load_migration_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "migration")
    if not isinstance(raw_cases, list):
        raise ValueError("migration TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"migration TCK case {index} must be a mapping")
        case_id = raw_case.get("name")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"migration TCK case {index} requires name")
        document = raw_case.get("document")
        expected = raw_case.get("expected")
        if not isinstance(document, dict):
            raise ValueError(f"migration TCK case {case_id} requires document")
        if not isinstance(expected, Mapping):
            raise ValueError(f"migration TCK case {case_id} requires expected result")
        expected_document = expected.get("document")
        expected_error = expected.get("error")
        if not isinstance(expected_document, dict) and not isinstance(
            expected_error, Mapping
        ):
            raise ValueError(
                f"migration TCK case {case_id} requires expected document or error"
            )
        cases.append(TckCase.migration(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_application_event_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "application-events")
    if not isinstance(raw_cases, list):
        raise ValueError("application-events TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"application-events TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"application-events TCK case {index} requires name")
        operations = raw_case.get("operations")
        if not isinstance(operations, list) or not all(
            isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(
                f"application-events TCK case {case_id} operations must be a list of mappings"
            )
        expected = raw_case.get("expectedAcceptedKinds")
        if not isinstance(expected, list) or not all(
            isinstance(kind, str) for kind in expected
        ):
            raise ValueError(
                f"application-events TCK case {case_id} expectedAcceptedKinds must be strings"
            )
        expected_diagnostics = raw_case.get("expectedDiagnostics", [])
        if not isinstance(expected_diagnostics, list):
            raise ValueError(
                f"application-events TCK case {case_id} expectedDiagnostics must be a list"
            )
        decoded_diagnostics: list[dict[str, str]] = []
        for diagnostic_index, diagnostic in enumerate(expected_diagnostics):
            if not isinstance(diagnostic, Mapping) or set(diagnostic) != {
                "code",
                "message",
                "path",
            }:
                raise ValueError(
                    f"application-events TCK case {case_id} expectedDiagnostics[{diagnostic_index}] "
                    "must contain exactly code, message, and path"
                )
            if not all(
                type(diagnostic[key]) is str and bool(diagnostic[key])
                for key in ("code", "message", "path")
            ):
                raise ValueError(
                    f"application-events TCK case {case_id} expectedDiagnostics[{diagnostic_index}] "
                    "values must be non-empty strings"
                )
            decoded_diagnostics.append(
                {
                    "code": diagnostic["code"],
                    "message": diagnostic["message"],
                    "path": diagnostic["path"],
                }
            )
        operations_with_defaults = []
        for operation in operations:
            operation_with_defaults = dict(operation)
            for key in (
                "runId",
                "responseId",
                "turnId",
                "releaseId",
                "policySnapshotId",
                "streamId",
            ):
                if key in raw_case and key not in operation_with_defaults:
                    operation_with_defaults[key] = raw_case[key]
            operations_with_defaults.append(operation_with_defaults)
        cases.append(
            TckCase.application_events(
                case_id=case_id,
                operations=tuple(operations_with_defaults),
                expected_accepted_kinds=tuple(expected),
                expected_diagnostics=tuple(decoded_diagnostics),
            )
        )
    return tuple(cases)


def load_application_protocol_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "application-protocol")
    if not isinstance(raw_cases, list):
        raise ValueError("application-protocol TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"application-protocol TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"application-protocol TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "kind_sets",
            "command_envelope",
            "command_envelope_error",
            "event_envelope",
            "event_envelope_error",
            "capability_negotiation",
            "capability_negotiation_error",
            "protocol_log",
            "stream_cutoff",
        }:
            raise ValueError(
                f"application-protocol TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"application-protocol TCK case {case_id} requires expected result"
            )
        cases.append(
            TckCase.application_protocol(case_id=case_id, fixture=dict(raw_case))
        )
    return tuple(cases)


def load_approval_review_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "approval-review")
    if not isinstance(raw_cases, list):
        raise ValueError("approval-review TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"approval-review TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"approval-review TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "review_digest",
            "review_record",
            "review_changed_subject",
            "review_invalidated",
            "review_missing_credential",
        }:
            raise ValueError(
                f"approval-review TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"approval-review TCK case {case_id} requires expected result"
            )
        cases.append(TckCase.approval_review(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_exhaustion_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "exhaustion")
    if not isinstance(raw_cases, list):
        raise ValueError("exhaustion TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"exhaustion TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"exhaustion TCK case {index} requires name")
        policy = raw_case.get("policy")
        if not isinstance(policy, Mapping):
            raise ValueError(f"exhaustion TCK case {case_id} requires policy")
        cases.append(TckCase.exhaustion(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_budget_race_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "budget-race")
    if not isinstance(raw_cases, list):
        raise ValueError("budget-race TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"budget-race TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"budget-race TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {"reservation_race", "completion_reserve_race"}:
            raise ValueError(
                f"budget-race TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        cases.append(TckCase.budget_race(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_conversation_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "conversation")
    if not isinstance(raw_cases, list):
        raise ValueError("conversation TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"conversation TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"conversation TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "turn_commit",
            "abort_turn",
            "policy_stop_turn",
            "commit_conflict",
            "branch_regenerate",
            "branch_attachments",
            "attachment_resolution",
            "archive_conversation",
            "compaction_record",
            "delete_retention",
        }:
            raise ValueError(
                f"conversation TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"conversation TCK case {case_id} requires expected result"
            )
        cases.append(TckCase.conversation(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_documents_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "documents")
    if not isinstance(raw_cases, list):
        raise ValueError("documents TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"documents TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"documents TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "plain_text_parse",
            "line_chunks",
            "invalid_chunk_size",
            "parser_selection_lock",
            "parser_locked_parse",
        }:
            raise ValueError(
                f"documents TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"documents TCK case {case_id} requires expected result")
        cases.append(TckCase.documents(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_deployment_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "deployment")
    if not isinstance(raw_cases, list):
        raise ValueError("deployment TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"deployment TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"deployment TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "deployment_revision_digest",
            "release_pins",
            "upgrade_policy",
            "rollout_gate",
            "slo_condition",
        }:
            raise ValueError(
                f"deployment TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"deployment TCK case {case_id} requires expected result")
        cases.append(TckCase.deployment(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_durable_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "durable")
    if not isinstance(raw_cases, list):
        raise ValueError("durable TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"durable TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"durable TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in DURABLE_CASE_KINDS:
            raise ValueError(
                f"durable TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"durable TCK case {case_id} requires expected result")
        cases.append(TckCase.durable(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_orchestration_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "orchestration")
    if not isinstance(raw_cases, list):
        raise ValueError("orchestration TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"orchestration TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"orchestration TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "task_plan_patch",
            "task_plan_errors",
            "context_access",
            "model_pool",
            "lease_pool",
            "child_budget_delegation",
        }:
            raise ValueError(
                f"orchestration TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"orchestration TCK case {case_id} requires expected result"
            )
        cases.append(TckCase.orchestration(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_rag_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "rag")
    if not isinstance(raw_cases, list):
        raise ValueError("rag TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"rag TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"rag TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {"grounding", "freshness"}:
            raise ValueError(
                f"rag TCK case {case_id} has unsupported kind {raw_case.get('kind')!r}"
            )
        if case_kind == "grounding":
            context = raw_case.get("context")
            if not isinstance(context, Mapping):
                raise ValueError(f"rag TCK case {case_id} requires context")
            answer = raw_case.get("answer")
            if not isinstance(answer, Mapping):
                raise ValueError(f"rag TCK case {case_id} requires answer")
        if case_kind == "freshness":
            retrieval = raw_case.get("retrieval")
            if not isinstance(retrieval, Mapping):
                raise ValueError(f"rag TCK case {case_id} requires retrieval")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"rag TCK case {case_id} requires expected result")
        cases.append(TckCase.rag(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_retry_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "retry")
    if not isinstance(raw_cases, list):
        raise ValueError("retry TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"retry TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"retry TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "node_retry",
            "cancelled_before_retry",
            "cancelled_before_commit",
            "cancelled_before_start",
            "cancelled_after_terminal",
            "timeout_retry",
            "timeout_exhaustion",
        }:
            raise ValueError(
                f"retry TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        max_attempts = raw_case.get("maxAttempts", raw_case.get("max_attempts"))
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts <= 0
        ):
            raise ValueError(
                f"retry TCK case {case_id} requires positive integer maxAttempts"
            )
        failures_before_success = raw_case.get(
            "failuresBeforeSuccess",
            raw_case.get("failures_before_success"),
        )
        if (
            isinstance(failures_before_success, bool)
            or not isinstance(failures_before_success, int)
            or failures_before_success < 0
        ):
            raise ValueError(
                f"retry TCK case {case_id} requires non-negative integer failuresBeforeSuccess"
            )
        for field_name in ("cancelBeforeStart", "cancelAfterTerminal"):
            if field_name in raw_case and type(raw_case[field_name]) is not bool:
                raise ValueError(
                    f"retry TCK case {case_id} {field_name} must be a boolean"
                )
        if case_kind in {"timeout_retry", "timeout_exhaustion"}:
            allowed_fields = {
                "contractVersion",
                "name",
                "kind",
                "block",
                "nodeId",
                "maxAttempts",
                "failuresBeforeSuccess",
                "timeout",
                "attemptDurationsMs",
                "attemptOutputValues",
                "idempotencyKey",
                "expected",
            }
            unknown_fields = sorted(set(raw_case) - allowed_fields)
            if unknown_fields:
                raise ValueError(
                    f"retry TCK case {case_id} has unknown field {unknown_fields[0]}"
                )
            if raw_case.get("contractVersion") != "graphblocks.retry-flow.tck.v1":
                raise ValueError(
                    f"retry TCK case {case_id} requires contractVersion graphblocks.retry-flow.tck.v1"
                )
            for field_name, max_bytes in (
                ("name", 256),
                ("block", 256),
                ("nodeId", 256),
                ("idempotencyKey", 1_024),
            ):
                value = raw_case.get(field_name)
                if (
                    not isinstance(value, str)
                    or not value
                    or value != value.strip()
                    or len(value.encode("utf-8")) > max_bytes
                ):
                    raise ValueError(
                        f"retry TCK case {case_id} {field_name} must be an exact non-empty bounded string"
                    )
            if max_attempts > MAX_NODE_RETRY_ATTEMPTS:
                raise ValueError(
                    f"retry TCK case {case_id} maxAttempts exceeds {MAX_NODE_RETRY_ATTEMPTS}"
                )
            if failures_before_success != 0:
                raise ValueError(
                    f"retry TCK case {case_id} failuresBeforeSuccess must be zero"
                )
            timeout = raw_case.get("timeout")
            if not isinstance(timeout, str):
                raise ValueError(
                    f"retry TCK case {case_id} timeout must be a duration string"
                )
            timeout_ms = parse_duration_milliseconds(timeout)
            if timeout_ms is None or timeout_ms > 1_000:
                raise ValueError(
                    f"retry TCK case {case_id} requires a timeout of at most 1000 milliseconds"
                )
            attempt_durations_ms = raw_case.get("attemptDurationsMs")
            if (
                not isinstance(attempt_durations_ms, list)
                or len(attempt_durations_ms) != max_attempts
                or any(
                    isinstance(duration, bool)
                    or not isinstance(duration, int)
                    or duration < 0
                    or duration > 1_000
                    for duration in attempt_durations_ms
                )
            ):
                raise ValueError(
                    f"retry TCK case {case_id} attemptDurationsMs must match maxAttempts and contain 0..1000 millisecond integers"
                )
            if sum(attempt_durations_ms) > 2_000:
                raise ValueError(
                    f"retry TCK case {case_id} total attempt duration exceeds 2000 milliseconds"
                )
            attempt_output_values = raw_case.get("attemptOutputValues")
            if (
                not isinstance(attempt_output_values, list)
                or len(attempt_output_values) != max_attempts
                or any(
                    not isinstance(output, str)
                    or not output
                    or len(output.encode("utf-8")) > 4_096
                    for output in attempt_output_values
                )
            ):
                raise ValueError(
                    f"retry TCK case {case_id} attemptOutputValues must match maxAttempts and contain bounded strings"
                )
            if case_kind == "timeout_retry":
                if (
                    any(duration < timeout_ms for duration in attempt_durations_ms[:-1])
                    or attempt_durations_ms[-1] >= timeout_ms
                    or attempt_output_values[0] == attempt_output_values[-1]
                ):
                    raise ValueError(
                        f"retry TCK case {case_id} must time out stale attempts before a distinct final output"
                    )
            elif any(duration < timeout_ms for duration in attempt_durations_ms):
                raise ValueError(
                    f"retry TCK case {case_id} must time out every attempt"
                )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"retry TCK case {case_id} requires expected result")
        cases.append(TckCase.retry(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_tool_lifecycle_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "tool-lifecycle")
    if not isinstance(raw_cases, list):
        raise ValueError("tool-lifecycle TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"tool-lifecycle TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"tool-lifecycle TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "incremental_arguments",
            "admission_invalid_arguments",
            "admission_missing_schema",
            "admission_resolved_tool_mismatch",
            "admission_tool_name_mismatch",
            "admission_arguments_digest_mismatch",
            "admission_policy_stopped_response",
            "admission_expired_policy_decision",
            "admission_expired_resolved_tool",
            "admission_policy_input_digest_mismatch",
            "admission_policy_input_digest_missing",
            "admission_policy_denied",
            "admission_policy_deferred",
            "admission_missing_approval",
            "admission_expired_approval",
            "admission_missing_required_idempotency_key",
            "admission_blank_idempotency_key",
            "approval_argument_mutation",
        }:
            raise ValueError(
                f"tool-lifecycle TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"tool-lifecycle TCK case {case_id} requires expected result"
            )
        cases.append(TckCase.tool_lifecycle(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_tool_execution_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "tool-execution")
    if not isinstance(raw_cases, list):
        raise ValueError("tool-execution TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"tool-execution TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"tool-execution TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind != "execution_plan":
            raise ValueError(
                f"tool-execution TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        calls = raw_case.get("calls")
        if not isinstance(calls, list) or not all(
            isinstance(call, dict) for call in calls
        ):
            raise ValueError(
                f"tool-execution TCK case {case_id} calls must be a list of mappings"
            )
        operations = raw_case.get("operations", [])
        if not isinstance(operations, list) or not all(
            isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(
                f"tool-execution TCK case {case_id} operations must be a list of mappings"
            )
        expected_states = raw_case.get("expectedStates", {})
        if not isinstance(expected_states, Mapping):
            raise ValueError(
                f"tool-execution TCK case {case_id} expectedStates must be a mapping"
            )
        cases.append(TckCase.tool_execution(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_tool_result_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "tool-result")
    if not isinstance(raw_cases, list):
        raise ValueError("tool-result TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"tool-result TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"tool-result TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {"prepare_for_model", "stream_state"}:
            raise ValueError(
                f"tool-result TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        if case_kind == "prepare_for_model":
            tool = raw_case.get("tool")
            if not isinstance(tool, Mapping):
                raise ValueError(
                    f"tool-result TCK case {case_id} tool must be a mapping"
                )
            result = raw_case.get("result")
            if not isinstance(result, Mapping):
                raise ValueError(
                    f"tool-result TCK case {case_id} result must be a mapping"
                )
        else:
            operations = raw_case.get("operations")
            if not isinstance(operations, list) or not all(
                isinstance(operation, Mapping) for operation in operations
            ):
                raise ValueError(
                    f"tool-result TCK case {case_id} operations must be a list of mappings"
                )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"tool-result TCK case {case_id} requires expected result")
        cases.append(TckCase.tool_result(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_typed_ports_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "typed-ports")
    if not isinstance(raw_cases, list):
        raise ValueError("typed-ports TCK root must be a list")
    cases: list[TckCase] = []
    supported_scenarios = {
        "compile_stdlib_model_generate",
        "run_stdlib_model_generate",
        "reject_cross_builder_port",
        "reject_noncanonical_schema",
        "reject_catalog_type_mismatch",
    }
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"typed-ports TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"typed-ports TCK case {index} requires name")
        scenario = raw_case.get("scenario")
        if scenario not in supported_scenarios:
            raise ValueError(
                f"typed-ports TCK case {case_id} has unsupported scenario {scenario!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"typed-ports TCK case {case_id} requires expected result")
        cases.append(TckCase.typed_ports(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_outcome_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "outcome")
    if not isinstance(raw_cases, list):
        raise ValueError("outcome TCK root must be a list")
    cases: list[TckCase] = []
    required_fields_by_scenario = {
        "normalize_outcome": {"name", "scenario", "outcome", "expected"},
        "evaluate_readiness": {
            "name",
            "scenario",
            "signals",
            "dependencies",
            "expected",
        },
        "execute_local_terminal": {"name", "scenario", "outcome", "expected"},
        "execute_output_projection": {
            "name",
            "scenario",
            "projection",
            "expected",
        },
    }
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"outcome TCK case {index} must be a mapping")
        case_id = raw_case.get("name")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"outcome TCK case {index} requires name")
        if "request" in raw_case:
            if set(raw_case) != {"name", "request", "expected"}:
                raise ValueError(
                    f"outcome TCK case {case_id} wrapper must contain exactly "
                    "expected, name, request"
                )
            expected = raw_case.get("expected")
            if not isinstance(expected, Mapping):
                raise ValueError(
                    f"outcome TCK case {case_id} requires expected result"
                )
            cases.append(TckCase.outcome(case_id=case_id, fixture=dict(raw_case)))
            continue
        scenario = raw_case.get("scenario")
        if not isinstance(scenario, str):
            raise ValueError(
                f"outcome TCK case {case_id} has unsupported scenario {scenario!r}"
            )
        required_fields = required_fields_by_scenario.get(scenario)
        if required_fields is None:
            raise ValueError(
                f"outcome TCK case {case_id} has unsupported scenario {scenario!r}"
            )
        if set(raw_case) != required_fields:
            raise ValueError(
                f"outcome TCK case {case_id} must contain exactly "
                + ", ".join(sorted(required_fields))
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"outcome TCK case {case_id} requires expected result")
        cases.append(TckCase.outcome(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_usage_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "usage")
    if not isinstance(raw_cases, list):
        raise ValueError("usage TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"usage TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"usage TCK case {index} requires name")
        operations = raw_case.get("operations")
        if not isinstance(operations, list) or not all(
            isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(
                f"usage TCK case {case_id} operations must be a list of mappings"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"usage TCK case {case_id} requires expected result")
        cases.append(TckCase.usage(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_voice_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "voice")
    if not isinstance(raw_cases, list):
        raise ValueError("voice TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"voice TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"voice TCK case {index} requires name")
        case_kind = raw_case.get("kind")
        if case_kind not in {
            "session_request",
            "vad_interruption",
            "playback_interrupt",
            "validation_errors",
        }:
            raise ValueError(
                f"voice TCK case {case_id} has unsupported kind {case_kind!r}"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"voice TCK case {case_id} requires expected result")
        cases.append(TckCase.voice(case_id=case_id, fixture=dict(raw_case)))
    return tuple(cases)


def load_policy_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "policy")
    if not isinstance(raw_cases, list):
        raise ValueError("policy TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"policy TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"policy TCK case {index} requires name")
        delivery = raw_case.get("delivery", {})
        if not isinstance(delivery, dict):
            raise ValueError(f"policy TCK case {case_id} delivery must be a mapping")
        operations = raw_case.get("operations")
        if not isinstance(operations, list) or not all(
            isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(
                f"policy TCK case {case_id} operations must be a list of mappings"
            )
        expected = raw_case.get("expected", {})
        if not isinstance(expected, dict):
            raise ValueError(
                f"policy TCK case {case_id} expected result must be a mapping"
            )
        cases.append(
            TckCase.policy(
                case_id=case_id,
                delivery=delivery,
                operations=tuple(operations),
                expected=expected,
                stream_id=str(
                    raw_case.get("streamId", raw_case.get("stream_id", "stream-1"))
                ),
                response_id=str(
                    raw_case.get(
                        "responseId", raw_case.get("response_id", "response-1")
                    )
                ),
            )
        )
    return tuple(cases)


def load_sequence_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "sequence")
    if not isinstance(raw_cases, list):
        raise ValueError("sequence TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"sequence TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"sequence TCK case {index} requires name")
        capacity = raw_case.get("capacity")
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise ValueError(f"sequence TCK case {case_id} requires integer capacity")
        operations = raw_case.get("operations", [])
        if not isinstance(operations, list) or not all(
            isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(
                f"sequence TCK case {case_id} operations must be a list of mappings"
            )
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"sequence TCK case {case_id} requires expected result")
        expected_state = expected.get("state")
        if expected_state is not None and (
            not isinstance(expected_state, str) or not expected_state.strip()
        ):
            raise ValueError(
                f"sequence TCK case {case_id} expected state must be a string"
            )
        expected_creation_error = expected.get("creation_error")
        if expected_creation_error is not None and (
            not isinstance(expected_creation_error, str)
            or not expected_creation_error.strip()
        ):
            raise ValueError(
                f"sequence TCK case {case_id} expected creation_error must be a string"
            )
        cases.append(
            TckCase.sequence(
                case_id=case_id,
                capacity=capacity,
                operations=tuple(operations),
                expected_state=expected_state,
                expected_creation_error=expected_creation_error,
            )
        )
    return tuple(cases)


def load_schema_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "schema")
    if not isinstance(raw_cases, list):
        raise ValueError("schema TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"schema TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"schema TCK case {index} requires name")
        schema_id = _first_mapping_value(raw_case, "schema_id", "schemaId", "id")
        if not isinstance(schema_id, str) or not schema_id.strip():
            raise ValueError(f"schema TCK case {case_id} requires schema_id")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"schema TCK case {case_id} requires expected result")
        expected_ok = _first_mapping_value(
            expected, "valid", "expected_ok", "expectedOk"
        )
        if not isinstance(expected_ok, bool):
            raise ValueError(
                f"schema TCK case {case_id} requires boolean expected valid"
            )
        expected_canonical = _first_mapping_value(
            expected,
            "canonical",
            "canonical_schema_id",
            "canonicalSchemaId",
            "schema_id",
            "schemaId",
        )
        if expected_canonical is not None and not isinstance(expected_canonical, str):
            raise ValueError(
                f"schema TCK case {case_id} canonical schema id must be a string"
            )
        expected_schema_name = _first_mapping_value(
            expected, "name", "schema_name", "schemaName"
        )
        if expected_schema_name is not None and not isinstance(
            expected_schema_name, str
        ):
            raise ValueError(
                f"schema TCK case {case_id} expected name must be a string"
            )
        expected_major_version = _first_mapping_value(
            expected, "major_version", "majorVersion"
        )
        if expected_major_version is not None:
            if isinstance(expected_major_version, bool) or not isinstance(
                expected_major_version, int
            ):
                raise ValueError(
                    f"schema TCK case {case_id} expected major_version must be an integer"
                )
            if expected_major_version <= 0:
                raise ValueError(
                    f"schema TCK case {case_id} expected major_version must be positive"
                )
        expected_error = _first_mapping_value(
            expected, "error", "error_type", "errorType"
        )
        if expected_error is not None and not isinstance(expected_error, str):
            raise ValueError(
                f"schema TCK case {case_id} expected error must be a string"
            )
        cases.append(
            TckCase.schema(
                case_id=case_id,
                schema_id=schema_id,
                expected_ok=expected_ok,
                expected_canonical_schema_id=expected_canonical,
                expected_schema_name=expected_schema_name,
                expected_major_version=expected_major_version,
                expected_error=expected_error,
            )
        )
    return tuple(cases)


def load_schema_typed_value_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "typed value schema")
    if not isinstance(raw_cases, list):
        raise ValueError("typed value schema TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"typed value schema TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"typed value schema TCK case {index} requires name")
        schema_id = _first_mapping_value(raw_case, "schema", "schema_id", "schemaId")
        if not isinstance(schema_id, str) or not schema_id.strip():
            raise ValueError(f"typed value schema TCK case {case_id} requires schema")
        if "value" not in raw_case:
            raise ValueError(f"typed value schema TCK case {case_id} requires value")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"typed value schema TCK case {case_id} requires expected result"
            )
        expected_error = _first_mapping_value(
            expected, "error", "error_type", "errorType"
        )
        if expected_error is not None and not isinstance(expected_error, str):
            raise ValueError(
                f"typed value schema TCK case {case_id} expected error must be a string"
            )
        expected_ok = expected_error is None
        expected_canonical_value = _first_mapping_value(
            expected, "canonical_value", "canonicalValue"
        )
        if expected_ok and not isinstance(expected_canonical_value, Mapping):
            raise ValueError(
                f"typed value schema TCK case {case_id} requires expected canonical_value"
            )
        expected_canonical_json = _first_mapping_value(
            expected, "canonical_json", "canonicalJson"
        )
        if expected_ok and not isinstance(expected_canonical_json, str):
            raise ValueError(
                f"typed value schema TCK case {case_id} requires expected canonical_json"
            )
        canonical_value = (
            dict(expected_canonical_value)
            if isinstance(expected_canonical_value, Mapping)
            else None
        )
        cases.append(
            TckCase.schema(
                case_id=case_id,
                schema_id=schema_id,
                schema_case_type="typed_value",
                schema_value=raw_case["value"],
                expected_ok=expected_ok,
                expected_canonical_value=canonical_value,
                expected_canonical_json=expected_canonical_json
                if isinstance(expected_canonical_json, str)
                else None,
                expected_error=expected_error,
            )
        )
    return tuple(cases)


def load_schema_resource_tck_cases(path: str | Path) -> tuple[TckCase, ...]:
    raw_cases = _load_tck_cases_json(path, "resource schema")
    if not isinstance(raw_cases, list):
        raise ValueError("resource schema TCK root must be a list")
    cases: list[TckCase] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"resource schema TCK case {index} must be a mapping")
        case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"resource schema TCK case {index} requires name")
        if "document" not in raw_case:
            raise ValueError(f"resource schema TCK case {case_id} requires document")
        expected = raw_case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(
                f"resource schema TCK case {case_id} requires expected result"
            )
        expected_ok = expected.get("valid")
        if not isinstance(expected_ok, bool):
            raise ValueError(
                f"resource schema TCK case {case_id} requires boolean expected valid"
            )
        raw_errors = expected.get("errors", [])
        if not isinstance(raw_errors, list):
            raise ValueError(
                f"resource schema TCK case {case_id} expected errors must be a list"
            )
        expected_errors: list[dict[str, str]] = []
        for error_index, error in enumerate(raw_errors):
            if not isinstance(error, Mapping):
                raise ValueError(
                    f"resource schema TCK case {case_id} expected error {error_index} "
                    "must be a mapping"
                )
            normalized_error: dict[str, str] = {}
            for field_name in ("code", "path", "keyword"):
                value = error.get(field_name)
                if not isinstance(value, str) or not value:
                    raise ValueError(
                        f"resource schema TCK case {case_id} expected error {error_index} "
                        f"requires string {field_name}"
                    )
                normalized_error[field_name] = value
            expected_errors.append(normalized_error)
        if expected_ok and expected_errors:
            raise ValueError(
                f"resource schema TCK case {case_id} cannot expect errors when valid"
            )
        if not expected_ok and not expected_errors:
            raise ValueError(
                f"resource schema TCK case {case_id} requires expected errors when invalid"
            )
        cases.append(
            TckCase.schema(
                case_id=case_id,
                schema_id=None,
                schema_case_type="resource",
                schema_value=deepcopy(raw_case["document"]),
                expected_ok=expected_ok,
                expected_resource_errors=tuple(expected_errors),
            )
        )
    return tuple(cases)


def _tck_fixture_paths(suite: str, path: Path) -> tuple[Path, ...]:
    paths = [path]
    if suite == "schema":
        paths.extend(
            candidate
            for candidate in (
                path.with_name("resources.json"),
                path.with_name("typed-values.json"),
            )
            if candidate.is_file()
        )
    return tuple(paths)


def _tck_fixture_digest(suite: str, path: Path) -> str:
    return canonical_hash(
        {
            fixture.name: "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()
            for fixture in _tck_fixture_paths(suite, path)
        }
    )


def _tck_registry(suite: str) -> RuntimeRegistry:
    if suite == "runtime":
        return stdlib_registry()
    return core_stdlib_registry()


def load_tck_suite_manifests(root: str | Path) -> tuple[TckSuiteManifest, ...]:
    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError("TCK root must be a directory")
    manifests: list[TckSuiteManifest] = []
    for path in sorted(
        root_path.glob("*/cases.json"), key=lambda item: item.parent.name
    ):
        suite_id = path.parent.name
        raw_cases = _load_tck_cases_json(path, suite_id)
        if not isinstance(raw_cases, list):
            raise ValueError(f"TCK suite {suite_id} root must be a list")
        case_ids: list[str] = []
        seen: set[str] = set()
        for index, raw_case in enumerate(raw_cases):
            if not isinstance(raw_case, Mapping):
                raise ValueError(f"TCK suite {suite_id} case {index} must be a mapping")
            case_id = _first_mapping_value(raw_case, "name", "case_id", "caseId")
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError(f"TCK suite {suite_id} case {index} requires name")
            if case_id in seen:
                raise ValueError(
                    f"TCK suite {suite_id} has duplicate case id {case_id!r}"
                )
            seen.add(case_id)
            case_ids.append(case_id)
        auxiliary_files = _tck_fixture_paths(suite_id, path)[1:]
        if suite_id == "schema":
            auxiliary_schema_loaders = (
                (path.parent / "resources.json", load_schema_resource_tck_cases),
                (path.parent / "typed-values.json", load_schema_typed_value_tck_cases),
            )
            for auxiliary_path, loader in auxiliary_schema_loaders:
                if not auxiliary_path.is_file():
                    continue
                for case in loader(auxiliary_path):
                    if case.case_id in seen:
                        raise ValueError(
                            f"TCK suite {suite_id} has duplicate case id {case.case_id!r}"
                        )
                    seen.add(case.case_id)
                    case_ids.append(case.case_id)
        manifests.append(
            TckSuiteManifest(
                suite_id=suite_id,
                path=path.relative_to(root_path).as_posix(),
                case_ids=tuple(case_ids),
                fixture_digest=_tck_fixture_digest(suite_id, path),
                auxiliary_paths=tuple(
                    auxiliary_path.relative_to(root_path).as_posix()
                    for auxiliary_path in auxiliary_files
                ),
            )
        )
    if not manifests:
        raise ValueError("TCK root must contain at least one nonempty suite")
    return tuple(manifests)


def load_tck_cases_for_suite(suite: str, path: str | Path) -> tuple[TckCase, ...]:
    if suite == "application-events":
        return load_application_event_tck_cases(path)
    if suite == "application-protocol":
        return load_application_protocol_tck_cases(path)
    if suite == "approval-review":
        return load_approval_review_tck_cases(path)
    if suite == "budget-race":
        return load_budget_race_tck_cases(path)
    if suite == "compiler":
        return load_compiler_tck_cases(path)
    if suite == "conversation":
        return load_conversation_tck_cases(path)
    if suite == "deployment":
        return load_deployment_tck_cases(path)
    if suite == "durable":
        return load_durable_tck_cases(path)
    if suite == "migration":
        return load_migration_tck_cases(path)
    if suite == "documents":
        return load_documents_tck_cases(path)
    if suite == "exhaustion":
        return load_exhaustion_tck_cases(path)
    if suite == "orchestration":
        return load_orchestration_tck_cases(path)
    if suite == "outcome":
        return load_outcome_tck_cases(path)
    if suite == "policy":
        return load_policy_tck_cases(path)
    if suite == "rag":
        return load_rag_tck_cases(path)
    if suite == "retry":
        return load_retry_tck_cases(path)
    if suite == "runtime":
        return load_runtime_tck_cases(path)
    if suite == "schema":
        primary_cases = load_schema_tck_cases(path)
        resource_cases_path = Path(path).with_name("resources.json")
        typed_values_path = Path(path).with_name("typed-values.json")
        resource_cases = (
            load_schema_resource_tck_cases(resource_cases_path)
            if resource_cases_path.is_file()
            else ()
        )
        typed_value_cases = (
            load_schema_typed_value_tck_cases(typed_values_path)
            if typed_values_path.is_file()
            else ()
        )
        return primary_cases + resource_cases + typed_value_cases
    if suite == "sequence":
        return load_sequence_tck_cases(path)
    if suite == "tool-lifecycle":
        return load_tool_lifecycle_tck_cases(path)
    if suite == "tool-execution":
        return load_tool_execution_tck_cases(path)
    if suite == "tool-result":
        return load_tool_result_tck_cases(path)
    if suite == "typed-ports":
        return load_typed_ports_tck_cases(path)
    if suite == "usage":
        return load_usage_tck_cases(path)
    if suite == "voice":
        return load_voice_tck_cases(path)
    raise ValueError(f"unsupported TCK suite {suite!r}")


def bundled_tck_root() -> Path:
    """Return the installed C0/C1 fixture root shipped with graphblocks-testing."""

    root = Path(__file__).resolve().parent / "fixtures" / "tck"
    if not root.is_dir():
        raise RuntimeError("bundled graphblocks-testing TCK fixtures are missing")
    return root


def load_bundled_tck_suite_manifests() -> tuple[TckSuiteManifest, ...]:
    """Load the exact C0/C1 suite manifests bundled in the distribution."""

    manifests = load_tck_suite_manifests(bundled_tck_root())
    suite_ids = tuple(manifest.suite_id for manifest in manifests)
    if suite_ids != _BUNDLED_TCK_SUITES:
        raise RuntimeError("bundled graphblocks-testing TCK suite set is incomplete")
    return manifests


def load_bundled_tck_cases_for_suite(suite: str) -> tuple[TckCase, ...]:
    """Load one bundled C0/C1 suite without an external fixture path."""

    if suite not in _BUNDLED_TCK_SUITES:
        raise ValueError(f"TCK suite {suite!r} is not bundled with graphblocks-testing")
    return load_tck_cases_for_suite(suite, bundled_tck_root() / suite / "cases.json")
