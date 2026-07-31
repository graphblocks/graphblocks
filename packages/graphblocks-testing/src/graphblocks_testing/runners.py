"""TCK suite execution and reference runtime runners."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import hashlib
import json
from pathlib import Path


from graphblocks.application_event import (
    APPLICATION_COMMAND_KINDS,
    APPLICATION_PROTOCOL_EVENT_KINDS,
    ApplicationCommand,
    ApplicationCommandMetadata,
    ApplicationEvent,
    ApplicationEventMetadata,
    ApplicationEventStreamState,
    ApplicationProtocolEvent,
    ApplicationProtocolEventMetadata,
    ApplicationProtocolLog,
    ApplicationProtocolStreamState,
)
from graphblocks.canonical import (
    normalize_graph,
)
from graphblocks._version import __version__ as GRAPHBLOCKS_VERSION
from graphblocks.compiler import (
    Plan,
    compile_graph as _compile_graph_normative,
    compile_graph_reference as compile_graph,
)
from graphblocks.conversation import (
    BranchRequest,
    CompactionRecord,
    ContentPart,
    Conversation,
    ConversationArchivedError,
    ConversationConflictError,
    ConversationNotFoundError,
    FileAttachment,
    InMemoryConversationStore,
    Message,
    RegenerateRequest,
    TurnConflictError,
)
from graphblocks.deployment import (
    DeploymentRevision,
    DeploymentSloProfile,
    GraphRelease,
    GraphReleaseGraph,
    GraphReleaseMutableReferencesError,
    ImageRef,
    KnowledgeBinding,
    PromptLock,
    ReleaseLockRef,
    RolloutAnalysisResult,
    RolloutPlan,
    RolloutStep,
    SupplyChainLock,
    UpgradePolicy,
)
from graphblocks.document_parsers import (
    DocumentParserRegistry,
    ParserDescriptor,
    plain_text_parser_descriptor,
)
from graphblocks.documents import (
    ArtifactRef,
    SourceRef,
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)
from graphblocks.evaluation import (
    ResourceSnapshotRef,
    SloReport,
)
from graphblocks.budget import (
    BudgetCompletionReserveStateError,
    BudgetExceededError,
    BudgetPermit,
    InMemoryBudgetLedger,
    UsageAmount,
)
from graphblocks.exhaustion import (
    ContinuationEnvelope,
    ExhaustionController,
    ExhaustionPolicy,
    MissingExhaustionBoundaryError,
    validate_exhaustion_policy,
)
from graphblocks.migration import MigrationError, migrate_document
from graphblocks.output_policy import (
    GenerationChunk,
    OutputDeliveryGate,
    OutputDeliveryPolicy,
    OutputGateError,
    OutputCutoff,
    OutputPolicyDecision,
)
from graphblocks.orchestration import (
    ChildBudgetDelegation,
    LeaseEpochMismatchError,
    LeasePool,
    LeasePoolExhaustedError,
    LeaseRequest,
    ModelPool,
    ModelProfile,
    ModelSelectionRequest,
    ModelToolNotAllowedError,
    TaskContextAccess,
    TaskPlan,
    TaskPlanContextAccessError,
    TaskPlanCycleError,
    TaskPlanDependencyError,
    TaskPlanPatch,
    TaskStep,
    WorkerProfile,
)
from graphblocks.policy import (
    PolicyDecision,
    PrincipalRef,
    ResourceRef as PolicyResourceRef,
)
from graphblocks.plugins import BlockCatalog
from graphblocks.rag import (
    Answer,
    Citation,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    KnowledgeItemRef,
    RetrievalResult,
    SearchHit,
    SearchRequest,
    build_context_pack,
    evaluate_retrieval_metrics,
    validate_answer_grounding,
)
from graphblocks.review import (
    InMemoryReviewerCredentialProvider,
    ReviewCredentialMissingError,
    ReviewRequest,
    ReviewScopeNotRequestedError,
    ReviewSubjectChangedError,
    ReviewWorkflow,
    ReviewerCredential,
)
from graphblocks.schema import (
    SchemaId,
    SchemaIdError,
    TypedValue,
    resource_schema_errors,
)
from graphblocks.server import (
    ApplicationProtocolCapabilities,
)
from graphblocks.runtime import (
    _mutable_json_like as _runtime_mutable_json_like,
    CancellationToken,
    InProcessRuntime,
    LocalExecutionJournal,
    LocalRunResult,
    LocalRuntime,
    RuntimeRegistry,
)
from graphblocks.tools import (
    BlockToolImplementation,
    JsonSchema,
    JsonSchemaNode,
    ResolvedTool,
    ToolApprovalRecord,
    ToolApprovalRequest,
    ToolBinding,
    ToolCallDraft,
    ToolCallError,
    ToolCatalog,
    ToolDefinition,
    ToolExecutionPlan,
    ToolExecutionPlanError,
    ToolPlanCall,
    ToolResult,
    ToolResultEvent,
    ToolResultStreamError,
    ToolResultStreamState,
    ToolResolutionScope,
    ToolSchemaRegistry,
    admit_tool_call,
    validate_tool_result_for_model,
)
from graphblocks.usage import InMemoryUsageLedger, UsageRecord

from ._facade import facade_dependency
from .models import (
    TckCase,
    _STABLE_TCK_SUITE_DIAGNOSTIC_CODES,
    _first_mapping_value,
    _string_tuple,
    _tool_execution_error_code,
    run_native_test_graph,
)
from .reports import TckReport, TckResult


@dataclass(frozen=True, slots=True)
class TckRunner:
    registry: RuntimeRegistry
    profile: str = "local"
    evidence_dir: Path | None = None
    suite: str | None = None
    fixture_digest: str | None = None
    implementation: str = "graphblocks-python"
    implementation_version: str = GRAPHBLOCKS_VERSION

    def run_cases(self, cases: tuple[TckCase, ...]) -> TckReport:
        suite = self.suite or (cases[0].kind if cases else "")
        if self.profile == "native" and suite != "runtime":
            raise ValueError(
                "native TCK profile is supported only for the runtime suite"
            )
        results: list[TckResult] = []
        for case in cases:
            if case.kind == "compiler":
                results.append(self._run_compiler_case(case))
            elif case.kind == "runtime":
                results.append(self._run_runtime_case(case))
            elif case.kind == "policy":
                results.append(self._run_policy_case(case))
            elif case.kind == "application-events":
                results.append(self._run_application_event_case(case))
            elif case.kind == "application-protocol":
                results.append(self._run_application_protocol_case(case))
            elif case.kind == "approval-review":
                results.append(self._run_approval_review_case(case))
            elif case.kind == "sequence":
                results.append(self._run_sequence_case(case))
            elif case.kind == "exhaustion":
                results.append(self._run_exhaustion_case(case))
            elif case.kind == "budget-race":
                results.append(self._run_budget_race_case(case))
            elif case.kind == "conversation":
                results.append(self._run_conversation_case(case))
            elif case.kind == "deployment":
                results.append(self._run_deployment_case(case))
            elif case.kind == "durable":
                results.append(self._run_durable_case(case))
            elif case.kind == "migration":
                results.append(self._run_migration_case(case))
            elif case.kind == "documents":
                results.append(self._run_documents_case(case))
            elif case.kind == "orchestration":
                results.append(self._run_orchestration_case(case))
            elif case.kind == "rag":
                results.append(self._run_rag_case(case))
            elif case.kind == "retry":
                results.append(self._run_retry_case(case))
            elif case.kind == "tool-execution":
                results.append(self._run_tool_execution_case(case))
            elif case.kind == "tool-lifecycle":
                results.append(self._run_tool_lifecycle_case(case))
            elif case.kind == "tool-result":
                results.append(self._run_tool_result_case(case))
            elif case.kind == "usage":
                results.append(self._run_usage_case(case))
            elif case.kind == "voice":
                results.append(self._run_voice_case(case))
            else:
                results.append(self._run_schema_case(case))
        fixture_digest = self.fixture_digest or (
            "sha256:" + hashlib.sha256(repr(cases).encode("utf-8")).hexdigest()
        )
        implementation = (
            f"{self.implementation}-native"
            if self.profile == "native" and suite == "runtime"
            else self.implementation
        )
        results = [
            replace(
                result,
                diagnostics=tuple(
                    {
                        **diagnostic,
                        "code": _STABLE_TCK_SUITE_DIAGNOSTIC_CODES[result.kind],
                    }
                    for diagnostic in result.diagnostics
                ),
            )
            if result.kind in _STABLE_TCK_SUITE_DIAGNOSTIC_CODES and result.diagnostics
            else result
            for result in results
        ]
        return TckReport(
            profile=self.profile,
            results=tuple(results),
            suite=suite,
            implementation=implementation,
            implementation_version=self.implementation_version,
            fixture_digest=fixture_digest,
        )

    def _compile_compiler_graph(
        self,
        document: dict[str, object],
        block_catalog: BlockCatalog,
        *,
        allow_unknown_blocks: bool,
    ) -> Plan:
        reference_compile = facade_dependency("compile_graph", compile_graph)
        return reference_compile(
            document,
            block_catalog=block_catalog,
            allow_unknown_blocks=allow_unknown_blocks,
        )

    def _run_compiler_case(self, case: TckCase) -> TckResult:
        block_catalog = BlockCatalog.from_blocks(case.block_catalog)
        plan = self._compile_compiler_graph(
            case.graph,
            block_catalog=block_catalog,
            allow_unknown_blocks=case.allow_unknown_blocks,
        )
        error_codes = tuple(
            diagnostic.code
            for diagnostic in plan.diagnostics.diagnostics
            if diagnostic.severity == "error"
        )
        warning_codes = tuple(
            diagnostic.code
            for diagnostic in plan.diagnostics.diagnostics
            if diagnostic.severity == "warning"
        )
        observed = {
            "hash": plan.graph_hash,
            "ok": plan.ok,
            "error_codes": list(error_codes),
            "warning_codes": list(warning_codes),
            "compiler_mode": "discovery" if case.allow_unknown_blocks else "closed",
        }
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
        if error_codes != case.expected_error_codes:
            diagnostics.append(
                {
                    "code": "ErrorCodesMismatch",
                    "message": "compiler error codes did not match expected diagnostics",
                    "path": "$.expected_error_codes",
                }
            )
        if warning_codes != case.expected_warning_codes:
            diagnostics.append(
                {
                    "code": "WarningCodesMismatch",
                    "message": "compiler warning codes did not match expected diagnostics",
                    "path": "$.expected_warning_codes",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_migration_case(self, case: TckCase) -> TckResult:
        fixture = case.migration_fixture
        document = fixture.get("document")
        expected = fixture.get("expected")
        if not isinstance(document, dict) or not isinstance(expected, dict):
            raise ValueError(f"migration TCK case {case.case_id} is malformed")
        source = deepcopy(document)
        diagnostics: list[dict[str, str]] = []
        observed: dict[str, object]
        try:
            migrated = migrate_document(document)
        except MigrationError as error:
            observed = {
                "error": {"code": error.code, "path": error.path},
                "source_mutated": document != source,
            }
            expected_error = expected.get("error")
            if not isinstance(expected_error, dict) or observed["error"] != {
                "code": expected_error.get("code"),
                "path": expected_error.get("path"),
            }:
                diagnostics.append(
                    {
                        "code": "MigrationErrorMismatch",
                        "message": "migration error did not match expected result",
                        "path": "$.expected.error",
                    }
                )
        else:
            observed = {
                "document": migrated,
                "source_mutated": document != source,
            }
            expected_document = expected.get("document")
            if migrated != expected_document:
                diagnostics.append(
                    {
                        "code": "MigrationDocumentMismatch",
                        "message": "migrated document did not match expected result",
                        "path": "$.expected.document",
                    }
                )
            if "normalized" in expected:
                normalized = normalize_graph(document)
                observed["normalized"] = normalized
                if normalized != expected.get("normalized"):
                    diagnostics.append(
                        {
                            "code": "MigrationNormalizationMismatch",
                            "message": "normalized migration did not match expected result",
                            "path": "$.expected.normalized",
                        }
                    )
            if "error" in expected:
                diagnostics.append(
                    {
                        "code": "MigrationExpectedErrorMissing",
                        "message": "migration succeeded when an error was expected",
                        "path": "$.expected.error",
                    }
                )
        if observed["source_mutated"]:
            diagnostics.append(
                {
                    "code": "MigrationMutatedSource",
                    "message": "migration mutated the source document",
                    "path": "$.document",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_schema_case(self, case: TckCase) -> TckResult:
        if case.schema_case_type == "resource":
            return self._run_schema_resource_case(case)
        if case.schema_case_type == "typed_value":
            return self._run_schema_typed_value_case(case)
        try:
            schema_id = SchemaId.parse(case.schema_id or "")
            observed = {
                "valid": True,
                "canonical": schema_id.as_str(),
                "name": schema_id.name,
                "major_version": schema_id.major_version,
            }
        except SchemaIdError as error:
            observed = {
                "valid": False,
                "error": type(error).__name__,
                "message": str(error),
            }
        diagnostics: list[dict[str, str]] = []
        if observed["valid"] != case.expected_ok:
            diagnostics.append(
                {
                    "code": "SchemaValidityMismatch",
                    "message": "schema id validity did not match expected result",
                    "path": "$.expected_ok",
                }
            )
        if (
            case.expected_canonical_schema_id is not None
            and observed.get("canonical") != case.expected_canonical_schema_id
        ):
            diagnostics.append(
                {
                    "code": "SchemaCanonicalMismatch",
                    "message": "schema id canonical value did not match expected value",
                    "path": "$.expected_canonical_schema_id",
                }
            )
        if (
            case.expected_schema_name is not None
            and observed.get("name") != case.expected_schema_name
        ):
            diagnostics.append(
                {
                    "code": "SchemaNameMismatch",
                    "message": "schema id name did not match expected value",
                    "path": "$.expected_schema_name",
                }
            )
        if (
            case.expected_major_version is not None
            and observed.get("major_version") != case.expected_major_version
        ):
            diagnostics.append(
                {
                    "code": "SchemaMajorVersionMismatch",
                    "message": "schema id major version did not match expected value",
                    "path": "$.expected_major_version",
                }
            )
        if (
            case.expected_error is not None
            and observed.get("error") != case.expected_error
        ):
            diagnostics.append(
                {
                    "code": "SchemaErrorMismatch",
                    "message": "schema id error type did not match expected error",
                    "path": "$.expected_error",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_schema_resource_case(self, case: TckCase) -> TckResult:
        violations = resource_schema_errors(case.schema_value)
        observed_errors = [
            {
                "code": violation.code,
                "path": violation.path,
                "keyword": violation.keyword,
                "message": violation.message,
                "schema_path": violation.schema_path,
            }
            for violation in violations
        ]
        expected_errors = [dict(error) for error in case.expected_resource_errors]
        observed_error_contracts = [
            {
                "code": error["code"],
                "path": error["path"],
                "keyword": error["keyword"],
            }
            for error in observed_errors
        ]
        observed: dict[str, object] = {
            "valid": not violations,
            "errors": observed_errors,
        }
        diagnostics: list[dict[str, str]] = []
        if observed["valid"] != case.expected_ok:
            diagnostics.append(
                {
                    "code": "ResourceSchemaValidityMismatch",
                    "message": "resource schema validity did not match expected result",
                    "path": "$.expected_ok",
                }
            )
        if observed_error_contracts != expected_errors:
            diagnostics.append(
                {
                    "code": "ResourceSchemaErrorsMismatch",
                    "message": "resource schema violations did not match expected errors",
                    "path": "$.expected_resource_errors",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_schema_typed_value_case(self, case: TckCase) -> TckResult:
        try:
            typed_value = TypedValue.new(case.schema_id or "", case.schema_value)
            observed = {
                "valid": True,
                "canonical_value": typed_value.canonical_value(),
                "canonical_json": typed_value.to_json(),
            }
        except SchemaIdError as error:
            observed = {
                "valid": False,
                "error": type(error).__name__,
                "message": str(error),
            }
        diagnostics: list[dict[str, str]] = []
        if observed["valid"] != case.expected_ok:
            diagnostics.append(
                {
                    "code": "SchemaTypedValueValidityMismatch",
                    "message": "typed value schema validity did not match expected result",
                    "path": "$.expected_ok",
                }
            )
        if (
            case.expected_canonical_value is not None
            and observed.get("canonical_value") != case.expected_canonical_value
        ):
            diagnostics.append(
                {
                    "code": "SchemaTypedValueCanonicalValueMismatch",
                    "message": "typed value canonical envelope did not match expected value",
                    "path": "$.expected_canonical_value",
                }
            )
        if (
            case.expected_canonical_json is not None
            and observed.get("canonical_json") != case.expected_canonical_json
        ):
            diagnostics.append(
                {
                    "code": "SchemaTypedValueCanonicalJsonMismatch",
                    "message": "typed value canonical JSON did not match expected value",
                    "path": "$.expected_canonical_json",
                }
            )
        if (
            case.expected_error is not None
            and observed.get("error") != case.expected_error
        ):
            diagnostics.append(
                {
                    "code": "SchemaTypedValueErrorMismatch",
                    "message": "typed value schema error type did not match expected error",
                    "path": "$.expected_error",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_application_event_case(self, case: TckCase) -> TckResult:
        state = ApplicationEventStreamState()
        diagnostics: list[dict[str, str]] = []
        for sequence, operation in enumerate(
            case.application_event_operations, start=1
        ):
            response_id = str(operation.get("responseId", "response-1"))
            metadata = ApplicationEventMetadata(
                event_id=str(operation.get("eventId", f"{case.case_id}:{sequence}")),
                run_id=str(operation.get("runId", "run-1")),
                response_id=response_id,
                turn_id=str(operation["turnId"])
                if operation.get("turnId") is not None
                else None,
                sequence=operation.get("eventSequence", sequence),
                cursor=(
                    str(operation.get("eventCursor", operation.get("cursor")))
                    if operation.get("eventCursor", operation.get("cursor")) is not None
                    else None
                ),
                release_id=str(operation.get("releaseId", "release-1")),
                policy_snapshot_id=str(operation.get("policySnapshotId", "policy-1")),
                occurred_at=str(operation.get("occurredAt", "2026-06-23T00:00:00Z")),
                graph_id=(
                    str(operation.get("graphId", operation.get("graph_id")))
                    if operation.get("graphId", operation.get("graph_id")) is not None
                    else None
                ),
                node_id=(
                    str(operation.get("nodeId", operation.get("node_id")))
                    if operation.get("nodeId", operation.get("node_id")) is not None
                    else None
                ),
                operation_id=(
                    str(operation.get("operationId", operation.get("operation_id")))
                    if operation.get("operationId", operation.get("operation_id"))
                    is not None
                    else None
                ),
                visibility=str(operation.get("visibility", "client")),
            )
            if operation.get("op") == "output_policy_evaluation_started":
                raw_generation_sequence = operation.get(
                    "sequence", operation.get("chunkSequence", 0)
                )
                if isinstance(raw_generation_sequence, bool) or not isinstance(
                    raw_generation_sequence, int
                ):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventGenerationSequenceInvalid",
                            "message": "generation chunk sequence must be an integer",
                            "path": f"$.operations[{sequence - 1}].sequence",
                        }
                    )
                    continue
                chunk = GenerationChunk.text(
                    str(operation.get("streamId", "stream-1")),
                    response_id,
                    raw_generation_sequence,
                    str(operation.get("text", "")),
                )
                event = ApplicationEvent.output_policy_evaluation_started(
                    metadata,
                    chunk,
                    input_digest=str(operation.get("inputDigest", "")),
                )
                accepted = state.accept(event)
                if (accepted is not None) is not bool(
                    operation.get("expectAccepted", True)
                ):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            elif operation.get("op") == "output_policy_decision":
                accepted_through = operation.get(
                    "acceptedThrough", operation.get("acceptedThroughSequence")
                )
                if accepted_through is not None and (
                    isinstance(accepted_through, bool)
                    or not isinstance(accepted_through, int)
                ):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventPolicyAcceptedThroughInvalid",
                            "message": "output policy acceptedThrough must be an integer when present",
                            "path": f"$.operations[{sequence - 1}].acceptedThrough",
                        }
                    )
                    continue
                accepted_sequence = accepted_through
                disposition = str(operation.get("disposition", "allow"))
                decision_id = str(operation.get("decisionId", ""))
                input_digest = str(operation.get("inputDigest", ""))
                if disposition == "allow":
                    decision = OutputPolicyDecision.allow(
                        decision_id,
                        accepted_through_sequence=accepted_sequence,
                        input_digest=input_digest,
                    )
                elif disposition == "hold":
                    decision = OutputPolicyDecision.hold(
                        decision_id, input_digest=input_digest
                    )
                elif disposition == "redact":
                    decision = OutputPolicyDecision.redact(
                        decision_id,
                        accepted_through_sequence=accepted_sequence,
                        input_digest=input_digest,
                    )
                elif disposition == "replace":
                    replacement_parts = []
                    for raw_part in operation.get("replacementParts", []):
                        if isinstance(raw_part, Mapping):
                            replacement_parts.append(
                                ContentPart(
                                    kind="text", text=str(raw_part.get("text", ""))
                                )
                            )
                    decision = OutputPolicyDecision.replace(
                        decision_id,
                        accepted_through_sequence=accepted_sequence,
                        replacement_parts=tuple(replacement_parts),
                        input_digest=input_digest,
                    )
                elif disposition == "abort_turn":
                    decision = OutputPolicyDecision.abort_turn(
                        decision_id, input_digest=input_digest
                    )
                elif disposition == "deny_commit":
                    decision = OutputPolicyDecision.deny_commit(
                        decision_id, input_digest=input_digest
                    )
                else:
                    decision = OutputPolicyDecision.abort_response(
                        decision_id, input_digest=input_digest
                    )
                reason_codes = operation.get(
                    "reasonCodes", operation.get("reason_codes")
                )
                if isinstance(reason_codes, list):
                    decision = decision.with_reason_codes(
                        tuple(str(reason_code) for reason_code in reason_codes)
                    )
                policy_refs = operation.get("policyRefs", operation.get("policy_refs"))
                if isinstance(policy_refs, list):
                    decision = decision.with_policy_refs(
                        tuple(str(policy_ref) for policy_ref in policy_refs)
                    )
                provider_cancellation = operation.get("providerCancellation")
                if isinstance(provider_cancellation, str):
                    decision = decision.with_provider_cancellation(
                        provider_cancellation
                    )
                draft_disposition = operation.get("draftDisposition")
                if isinstance(draft_disposition, str):
                    decision = decision.with_draft_disposition(draft_disposition)
                pending_tool_calls = operation.get("pendingToolCalls")
                if isinstance(pending_tool_calls, str):
                    decision = decision.with_pending_tool_calls(pending_tool_calls)
                evaluated_at = operation.get("evaluatedAt")
                if isinstance(evaluated_at, str):
                    decision = decision.evaluated_at_time(evaluated_at)
                event = ApplicationEvent.output_policy_decision(metadata, decision)
                accepted = state.accept(event)
                if (accepted is not None) is not bool(
                    operation.get("expectAccepted", True)
                ):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            elif operation.get("op") == "output_cutoff":
                try:
                    cutoff = OutputCutoff(
                        stream_id=str(operation.get("streamId", "stream-1")),
                        response_id=response_id,
                        turn_id=str(operation["turnId"])
                        if operation.get("turnId") is not None
                        else None,
                        last_generated_sequence=operation.get(
                            "lastGeneratedSequence", 0
                        ),  # type: ignore[arg-type]
                        last_policy_accepted_sequence=operation.get(  # type: ignore[arg-type]
                            "lastPolicyAcceptedSequence",
                            0,
                        ),
                        last_client_delivered_sequence=operation.get(  # type: ignore[arg-type]
                            "lastClientDeliveredSequence",
                            0,
                        ),
                        terminal_reason=str(
                            operation.get("terminalReason", "policy_denied")
                        ),
                        draft_disposition=str(
                            operation.get("draftDisposition", "retract")
                        ),
                        durable_result=str(operation.get("durableResult", "none")),
                        policy_decision_id=(
                            str(operation["policyDecisionId"])
                            if operation.get("policyDecisionId") is not None
                            else None
                        ),
                        occurred_at=str(
                            operation.get("occurredAt", "2026-06-23T00:00:00Z")
                        ),
                    )
                except ValueError as error:
                    diagnostics.append(
                        {
                            "code": "ApplicationEventOutputCutoffInvalid",
                            "message": str(error),
                            "path": f"$.operations[{sequence - 1}]",
                        }
                    )
                    continue
                for event in ApplicationEvent.output_cutoff(metadata, cutoff):
                    if state.accept(event) is None:
                        diagnostics.append(
                            {
                                "code": "ApplicationEventUnexpectedRejection",
                                "message": "application event TCK output cutoff event was rejected",
                                "path": f"$.operations[{sequence - 1}]",
                            }
                        )
            elif operation.get("op") == "run_succeeded":
                event = ApplicationEvent.new(
                    "RunSucceeded",
                    metadata,
                    payload={
                        "status": "succeeded",
                        "outputs": operation.get("outputs", {}),
                    },
                )
                accepted = state.accept(event)
                if (accepted is not None) is not bool(
                    operation.get("expectAccepted", True)
                ):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            elif operation.get("op") == "tool_call_state":
                arguments = operation.get("arguments", {})
                draft = ToolCallDraft.proposed(
                    response_id,
                    str(operation.get("toolCallId", "")),
                    str(operation.get("toolName", "")),
                )
                draft = draft.append_argument_fragment(
                    json.dumps(arguments, sort_keys=True, separators=(",", ":"))
                ).complete_arguments()
                call = draft.into_tool_call(
                    str(operation.get("resolvedToolId", "")),
                    created_at=str(operation.get("createdAt", "2026-06-23T00:00:00Z")),
                )
                status = str(operation.get("status", "validated"))
                admitted_at = str(
                    operation.get(
                        "admittedAt", operation.get("createdAt", "2026-06-23T00:00:01Z")
                    )
                )
                completed_at = str(operation.get("completedAt", admitted_at))
                try:
                    if status == "policy_pending":
                        call = call.transition_status("policy_pending", at=admitted_at)
                    elif status == "approval_pending":
                        call = call.transition_status(
                            "approval_pending", at=admitted_at
                        )
                    elif status == "admitted":
                        call = call.transition_status("admitted", at=admitted_at)
                    elif status == "running":
                        call = call.transition_status(
                            "admitted", at=admitted_at
                        ).transition_status(
                            "running",
                            at=admitted_at,
                        )
                    elif status == "completed":
                        call = (
                            call.transition_status("admitted", at=admitted_at)
                            .transition_status("running", at=admitted_at)
                            .transition_status("completed", at=completed_at)
                        )
                    elif status in {
                        "failed",
                        "denied",
                        "cancelled",
                        "policy_stopped",
                        "expired",
                    }:
                        call = call.transition_status(status, at=completed_at)
                    elif status != "validated":
                        raise ValueError(f"unknown tool call status {status!r}")
                except (ToolCallError, ValueError) as error:
                    diagnostics.append(
                        {
                            "code": "ApplicationEventToolCallInvalid",
                            "message": str(error),
                            "path": f"$.operations[{sequence - 1}]",
                        }
                    )
                    continue
                event = ApplicationEvent.tool_call_state(metadata, call)
                accepted = (
                    state.accept(event) is not None if event is not None else False
                )
                if accepted is not bool(operation.get("expectAccepted", True)):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            elif operation.get("op") in {
                "tool_result_started",
                "tool_result_delta",
                "tool_result_artifact_ready",
                "tool_result_completed",
                "tool_result_failed",
                "tool_result_denied",
                "tool_result_cancelled",
                "tool_result_policy_stopped",
                "tool_result_incomplete",
            }:
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                raw_tool_result_sequence = operation.get(
                    "toolResultSequence",
                    operation.get("tool_result_sequence", sequence),
                )
                if isinstance(raw_tool_result_sequence, bool) or not isinstance(
                    raw_tool_result_sequence, int
                ):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventToolResultSequenceInvalid",
                            "message": "tool result sequence must be an integer",
                            "path": f"$.operations[{sequence - 1}].toolResultSequence",
                        }
                    )
                    continue
                tool_result_sequence = raw_tool_result_sequence
                op = str(operation["op"])
                if op == "tool_result_started":
                    result_event = ToolResultEvent.started(
                        tool_call_id,
                        tool_result_sequence,
                        started_at=str(
                            operation.get("startedAt", "2026-06-23T00:00:00Z")
                        ),
                    )
                elif op == "tool_result_artifact_ready":
                    artifact = operation.get("artifact", {})
                    if not isinstance(artifact, Mapping):
                        diagnostics.append(
                            {
                                "code": "ApplicationEventToolResultArtifactInvalid",
                                "message": "tool result artifact must be a mapping",
                                "path": f"$.operations[{sequence - 1}].artifact",
                            }
                        )
                        continue
                    artifact_id = artifact.get("artifactId")
                    uri = artifact.get("uri")
                    if not isinstance(artifact_id, str) or not isinstance(uri, str):
                        diagnostics.append(
                            {
                                "code": "ApplicationEventToolResultArtifactInvalid",
                                "message": "tool result artifact requires artifactId and uri strings",
                                "path": f"$.operations[{sequence - 1}].artifact",
                            }
                        )
                        continue
                    checksum = artifact.get("checksum")
                    media_type = artifact.get("mediaType")
                    result_event = ToolResultEvent.artifact_ready(
                        tool_call_id,
                        tool_result_sequence,
                        ArtifactRef(
                            artifact_id=artifact_id,
                            uri=uri,
                            checksum=checksum if isinstance(checksum, str) else None,
                            media_type=media_type
                            if isinstance(media_type, str)
                            else None,
                        ),
                    )
                elif op in {
                    "tool_result_failed",
                    "tool_result_denied",
                    "tool_result_cancelled",
                    "tool_result_policy_stopped",
                    "tool_result_incomplete",
                }:
                    if op in {
                        "tool_result_failed",
                        "tool_result_denied",
                        "tool_result_policy_stopped",
                    }:
                        raw_error = operation.get(
                            "error",
                            {
                                "code": "policy.tool_output_denied",
                                "message": "tool output was stopped by policy",
                            },
                        )
                        if not isinstance(raw_error, Mapping):
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultErrorInvalid",
                                    "message": "terminal tool result error must be a mapping",
                                    "path": f"$.operations[{sequence - 1}].error",
                                }
                            )
                            continue
                    if op == "tool_result_failed":
                        result = ToolResult.failed(
                            tool_call_id,
                            error=dict(raw_error),
                            started_at=str(
                                operation.get("startedAt", "2026-06-23T00:00:00Z")
                            ),
                            completed_at=str(
                                operation.get("completedAt", "2026-06-23T00:00:00Z")
                            ),
                        )
                    elif op == "tool_result_denied":
                        result = ToolResult.denied(
                            tool_call_id,
                            error=dict(raw_error),
                            completed_at=str(
                                operation.get("completedAt", "2026-06-23T00:00:00Z")
                            ),
                        )
                    elif op == "tool_result_cancelled":
                        result = ToolResult.cancelled(
                            tool_call_id,
                            started_at=str(
                                operation.get("startedAt", "2026-06-23T00:00:00Z")
                            ),
                            completed_at=str(
                                operation.get("completedAt", "2026-06-23T00:00:00Z")
                            ),
                        )
                    elif op == "tool_result_policy_stopped":
                        result = ToolResult.policy_stopped(
                            tool_call_id,
                            error=dict(raw_error),
                            started_at=str(
                                operation.get("startedAt", "2026-06-23T00:00:00Z")
                            ),
                            completed_at=str(
                                operation.get("completedAt", "2026-06-23T00:00:00Z")
                            ),
                        )
                    else:
                        result = ToolResult.incomplete(
                            tool_call_id,
                            started_at=str(
                                operation.get("startedAt", "2026-06-23T00:00:00Z")
                            ),
                            completed_at=str(
                                operation.get("completedAt", "2026-06-23T00:00:00Z")
                            ),
                        )
                    if operation.get("effectOutcome") is not None:
                        result = result.with_effect_outcome(
                            str(operation["effectOutcome"])
                        )
                    if op == "tool_result_failed":
                        result_event = ToolResultEvent.failed(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                    elif op == "tool_result_denied":
                        result_event = ToolResultEvent.denied(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                    elif op == "tool_result_cancelled":
                        result_event = ToolResultEvent.cancelled(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                    elif op == "tool_result_policy_stopped":
                        result_event = ToolResultEvent.policy_stopped(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                    else:
                        result_event = ToolResultEvent.incomplete(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                else:
                    raw_output = operation.get("output", [])
                    if not isinstance(raw_output, list):
                        diagnostics.append(
                            {
                                "code": "ApplicationEventToolResultOutputInvalid",
                                "message": "tool result output must be a list",
                                "path": f"$.operations[{sequence - 1}].output",
                            }
                        )
                        continue
                    output_parts: list[ContentPart] = []
                    invalid_output = False
                    for part_index, raw_part in enumerate(raw_output):
                        if not isinstance(raw_part, Mapping):
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultOutputInvalid",
                                    "message": "tool result output part must be a mapping",
                                    "path": f"$.operations[{sequence - 1}].output[{part_index}]",
                                }
                            )
                            invalid_output = True
                            break
                        metadata_value = raw_part.get("metadata", {})
                        if not isinstance(metadata_value, dict):
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultOutputInvalid",
                                    "message": "tool result output metadata must be a mapping",
                                    "path": f"$.operations[{sequence - 1}].output[{part_index}].metadata",
                                }
                            )
                            invalid_output = True
                            break
                        part_kind = str(raw_part.get("kind", "text"))
                        if part_kind == "text":
                            text = raw_part.get("text")
                            if not isinstance(text, str):
                                diagnostics.append(
                                    {
                                        "code": "ApplicationEventToolResultOutputInvalid",
                                        "message": "text tool result output part requires text",
                                        "path": f"$.operations[{sequence - 1}].output[{part_index}].text",
                                    }
                                )
                                invalid_output = True
                                break
                            output_parts.append(
                                ContentPart(
                                    kind="text",
                                    text=text,
                                    metadata=dict(metadata_value),
                                )
                            )
                        elif part_kind in {"json", "artifact_ref"}:
                            data = raw_part.get("data")
                            if not isinstance(data, dict):
                                diagnostics.append(
                                    {
                                        "code": "ApplicationEventToolResultOutputInvalid",
                                        "message": f"{part_kind} tool result output part requires object data",
                                        "path": f"$.operations[{sequence - 1}].output[{part_index}].data",
                                    }
                                )
                                invalid_output = True
                                break
                            output_parts.append(
                                ContentPart(
                                    kind=part_kind,
                                    data=dict(data),
                                    metadata=dict(metadata_value),
                                )
                            )
                        else:
                            diagnostics.append(
                                {
                                    "code": "ApplicationEventToolResultOutputInvalid",
                                    "message": f"unsupported tool result output kind {part_kind!r}",
                                    "path": f"$.operations[{sequence - 1}].output[{part_index}].kind",
                                }
                            )
                            invalid_output = True
                            break
                    if invalid_output:
                        continue
                    if op == "tool_result_delta":
                        result_event = ToolResultEvent.delta(
                            tool_call_id,
                            tool_result_sequence,
                            tuple(output_parts),
                        )
                    else:
                        result = ToolResult.completed(
                            tool_call_id,
                            tuple(output_parts),
                            started_at=str(
                                operation.get("startedAt", "2026-06-23T00:00:00Z")
                            ),
                            completed_at=str(
                                operation.get("completedAt", "2026-06-23T00:00:00Z")
                            ),
                        )
                        if operation.get("effectOutcome") is not None:
                            result = result.with_effect_outcome(
                                str(operation["effectOutcome"])
                            )
                        result_event = ToolResultEvent.completed(
                            tool_call_id,
                            tool_result_sequence,
                            result,
                        )
                event = ApplicationEvent.tool_result_event(metadata, result_event)
                accepted = event is not None and state.accept(event) is not None
                if accepted is not bool(operation.get("expectAccepted", True)):
                    diagnostics.append(
                        {
                            "code": "ApplicationEventAcceptanceMismatch",
                            "message": "application event acceptance did not match expected result",
                            "path": f"$.operations[{sequence - 1}].expectAccepted",
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "code": "ApplicationEventOperationUnknown",
                        "message": f"application event TCK operation {operation.get('op')!r} is not supported",
                        "path": f"$.operations[{sequence - 1}].op",
                    }
                )

        accepted_kinds = [event.kind for event in state.accepted_events]
        if accepted_kinds != list(case.expected_accepted_event_kinds):
            diagnostics.append(
                {
                    "code": "ApplicationEventAcceptedKindsMismatch",
                    "message": "accepted application event kinds did not match expected kinds",
                    "path": "$.expectedAcceptedKinds",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed={
                "accepted_kinds": accepted_kinds,
                "accepted_metadata": [
                    {
                        "event_id": event.metadata.event_id,
                        "run_id": event.metadata.run_id,
                        "response_id": event.metadata.response_id,
                        "turn_id": event.metadata.turn_id,
                        "sequence": event.metadata.sequence,
                        "cursor": event.metadata.cursor,
                        "release_id": event.metadata.release_id,
                        "policy_snapshot_id": event.metadata.policy_snapshot_id,
                        "occurred_at": event.metadata.occurred_at,
                        "graph_id": event.metadata.graph_id,
                        "node_id": event.metadata.node_id,
                        "operation_id": event.metadata.operation_id,
                        "visibility": event.metadata.visibility,
                    }
                    for event in state.accepted_events
                ],
            },
        )

    def _run_application_protocol_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.application_protocol_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "ApplicationProtocolExpectedInvalid",
                    "message": "application-protocol TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        observed: dict[str, object] = {}
        try:
            if kind == "kind_sets":
                observed = {
                    "commands": list(APPLICATION_COMMAND_KINDS),
                    "events": list(APPLICATION_PROTOCOL_EVENT_KINDS),
                }
            elif kind in {"command_envelope", "command_envelope_error"}:
                raw_metadata = fixture.get("metadata", {})
                if not isinstance(raw_metadata, Mapping):
                    raise ValueError(
                        "application-protocol command metadata must be a mapping"
                    )
                raw_payload = fixture.get("payload", {})
                if kind == "command_envelope" and not isinstance(raw_payload, Mapping):
                    raise ValueError(
                        "application-protocol command payload must be a mapping"
                    )
                try:
                    command = ApplicationCommand.new(
                        str(
                            fixture.get(
                                "commandKind",
                                fixture.get("command_kind", "InvokeGraph"),
                            )
                        ),
                        ApplicationCommandMetadata(
                            command_id=str(
                                raw_metadata.get(
                                    "commandId", raw_metadata.get("command_id", "")
                                )
                            ),
                            protocol_version=str(
                                raw_metadata.get(
                                    "protocolVersion",
                                    raw_metadata.get("protocol_version", ""),
                                )
                            ),
                            run_id=str(
                                raw_metadata.get(
                                    "runId", raw_metadata.get("run_id", "")
                                )
                            ),
                            turn_id=(
                                str(raw_metadata["turnId"])
                                if raw_metadata.get("turnId") is not None
                                else (
                                    str(raw_metadata["turn_id"])
                                    if raw_metadata.get("turn_id") is not None
                                    else None
                                )
                            ),
                            sequence=raw_metadata.get("sequence", 0),  # type: ignore[arg-type]
                            idempotency_key=(
                                str(raw_metadata["idempotencyKey"])
                                if raw_metadata.get("idempotencyKey") is not None
                                else (
                                    str(raw_metadata["idempotency_key"])
                                    if raw_metadata.get("idempotency_key") is not None
                                    else None
                                )
                            ),
                            issued_at_unix_ms=raw_metadata.get(
                                "issuedAtUnixMs",
                                raw_metadata.get("issued_at_unix_ms", 0),
                            ),  # type: ignore[arg-type]
                        ),
                        payload=dict(raw_payload)
                        if isinstance(raw_payload, Mapping)
                        else raw_payload,  # type: ignore[arg-type]
                    )
                except Exception as error:
                    if kind != "command_envelope_error":
                        raise
                    message = str(error)
                    observed = {
                        "error": "invalid_payload" if "payload" in message else message
                    }
                else:
                    if kind == "command_envelope_error":
                        observed = {"error": "none"}
                    else:
                        observed = {
                            "kind": command.kind,
                            "commandId": command.metadata.command_id,
                            "protocolVersion": command.metadata.protocol_version,
                            "runId": command.metadata.run_id,
                            "turnId": command.metadata.turn_id,
                            "sequence": command.metadata.sequence,
                            "idempotencyKey": command.metadata.idempotency_key,
                            "payload": dict(command.payload),
                        }
            elif kind in {"event_envelope", "event_envelope_error"}:
                raw_metadata = fixture.get("metadata", {})
                if not isinstance(raw_metadata, Mapping):
                    raise ValueError(
                        "application-protocol event metadata must be a mapping"
                    )
                raw_payload = fixture.get("payload", {})
                if kind == "event_envelope" and not isinstance(raw_payload, Mapping):
                    raise ValueError(
                        "application-protocol event payload must be a mapping"
                    )
                try:
                    event = ApplicationProtocolEvent.new(
                        str(
                            fixture.get(
                                "eventKind", fixture.get("event_kind", "RunStarted")
                            )
                        ),
                        ApplicationProtocolEventMetadata(
                            event_id=str(
                                raw_metadata.get(
                                    "eventId", raw_metadata.get("event_id", "")
                                )
                            ),
                            protocol_version=str(
                                raw_metadata.get(
                                    "protocolVersion",
                                    raw_metadata.get("protocol_version", ""),
                                )
                            ),
                            run_id=str(
                                raw_metadata.get(
                                    "runId", raw_metadata.get("run_id", "")
                                )
                            ),
                            release_id=raw_metadata.get(  # type: ignore[arg-type]
                                "releaseId",
                                raw_metadata.get("release_id", "local"),
                            ),
                            turn_id=(
                                str(raw_metadata["turnId"])
                                if raw_metadata.get("turnId") is not None
                                else (
                                    str(raw_metadata["turn_id"])
                                    if raw_metadata.get("turn_id") is not None
                                    else None
                                )
                            ),
                            operation_id=(
                                str(raw_metadata["operationId"])
                                if raw_metadata.get("operationId") is not None
                                else (
                                    str(raw_metadata["operation_id"])
                                    if raw_metadata.get("operation_id") is not None
                                    else None
                                )
                            ),
                            sequence=raw_metadata.get("sequence", 0),  # type: ignore[arg-type]
                            cursor=(
                                str(raw_metadata["cursor"])
                                if raw_metadata.get("cursor") is not None
                                else None
                            ),
                            occurred_at_unix_ms=raw_metadata.get(
                                "occurredAtUnixMs",
                                raw_metadata.get("occurred_at_unix_ms", 0),
                            ),  # type: ignore[arg-type]
                        ),
                        payload=dict(raw_payload)
                        if isinstance(raw_payload, Mapping)
                        else raw_payload,  # type: ignore[arg-type]
                    )
                except Exception as error:
                    if kind != "event_envelope_error":
                        raise
                    message = str(error)
                    observed = {
                        "error": "invalid_payload" if "payload" in message else message
                    }
                else:
                    if kind == "event_envelope_error":
                        observed = {"error": "none"}
                    else:
                        observed = {
                            "kind": event.kind,
                            "eventId": event.metadata.event_id,
                            "protocolVersion": event.metadata.protocol_version,
                            "runId": event.metadata.run_id,
                            "releaseId": event.metadata.release_id,
                            "turnId": event.metadata.turn_id,
                            "operationId": event.metadata.operation_id,
                            "sequence": event.metadata.sequence,
                            "cursor": event.metadata.cursor,
                            "payload": dict(event.payload),
                        }
                        if observed["operationId"] is None:
                            del observed["operationId"]
            elif kind == "protocol_log":
                raw_operations = fixture.get("operations", [])
                if not isinstance(raw_operations, list):
                    raise ValueError(
                        "application-protocol protocol_log operations must be a list"
                    )
                log = ApplicationProtocolLog()
                append_results: list[bool] = []
                append_errors: list[str] = []
                for operation_index, raw_operation in enumerate(raw_operations):
                    if not isinstance(raw_operation, Mapping):
                        raise ValueError(
                            "application-protocol protocol_log operation must be a mapping"
                        )
                    raw_metadata = raw_operation.get("metadata", {})
                    if not isinstance(raw_metadata, Mapping):
                        raise ValueError(
                            "application-protocol protocol_log operation metadata must be a mapping"
                        )
                    raw_payload = raw_operation.get("payload", {})
                    if not isinstance(raw_payload, Mapping):
                        raise ValueError(
                            "application-protocol protocol_log operation payload must be a mapping"
                        )
                    expected_error = raw_operation.get(
                        "expectError", raw_operation.get("expect_error")
                    )
                    try:
                        event = ApplicationProtocolEvent.new(
                            str(
                                raw_operation.get(
                                    "eventKind",
                                    raw_operation.get("event_kind", "RunStarted"),
                                )
                            ),
                            ApplicationProtocolEventMetadata(
                                event_id=str(
                                    raw_metadata.get(
                                        "eventId", raw_metadata.get("event_id", "")
                                    )
                                ),
                                protocol_version=str(
                                    raw_metadata.get(
                                        "protocolVersion",
                                        raw_metadata.get("protocol_version", ""),
                                    )
                                ),
                                run_id=str(
                                    raw_metadata.get(
                                        "runId", raw_metadata.get("run_id", "")
                                    )
                                ),
                                release_id=raw_metadata.get(  # type: ignore[arg-type]
                                    "releaseId",
                                    raw_metadata.get("release_id", "local"),
                                ),
                                turn_id=(
                                    str(raw_metadata["turnId"])
                                    if raw_metadata.get("turnId") is not None
                                    else (
                                        str(raw_metadata["turn_id"])
                                        if raw_metadata.get("turn_id") is not None
                                        else None
                                    )
                                ),
                                operation_id=(
                                    str(raw_metadata["operationId"])
                                    if raw_metadata.get("operationId") is not None
                                    else (
                                        str(raw_metadata["operation_id"])
                                        if raw_metadata.get("operation_id") is not None
                                        else None
                                    )
                                ),
                                sequence=raw_metadata.get("sequence", 0),  # type: ignore[arg-type]
                                cursor=(
                                    str(raw_metadata["cursor"])
                                    if raw_metadata.get("cursor") is not None
                                    else None
                                ),
                                occurred_at_unix_ms=raw_metadata.get(
                                    "occurredAtUnixMs",
                                    raw_metadata.get("occurred_at_unix_ms", 0),
                                ),  # type: ignore[arg-type]
                            ),
                            payload=dict(raw_payload),
                        )
                        appended = log.append(event)
                    except Exception as error:
                        if (
                            expected_error == "run_mismatch"
                            and "run_id must match first event" in str(error)
                        ):
                            appended = False
                            append_errors.append("run_mismatch")
                        elif (
                            expected_error == "duplicate_event_id_conflict"
                            and "event_id conflict" in str(error)
                        ):
                            appended = False
                            append_errors.append("duplicate_event_id_conflict")
                        elif expected_error is not None and str(error) == str(
                            expected_error
                        ):
                            appended = False
                            append_errors.append(str(expected_error))
                        else:
                            raise
                    append_results.append(appended)
                    if expected_error is None and appended is not bool(
                        raw_operation.get("expectAppended", True)
                    ):
                        diagnostics.append(
                            {
                                "code": "ApplicationProtocolAppendMismatch",
                                "message": "application protocol log append result did not match expected result",
                                "path": f"$.operations[{operation_index}].expectAppended",
                            }
                        )
                replay_cursor = fixture.get("replayAfter", fixture.get("replay_after"))
                replay_limit = fixture.get(
                    "replayLimit", fixture.get("replay_limit", 100)
                )
                replay_error: str | None = None
                try:
                    replay = log.replay_after(
                        str(replay_cursor) if replay_cursor is not None else None,
                        limit=replay_limit,  # type: ignore[arg-type]
                    )
                except Exception as error:
                    expected_replay_error = _first_mapping_value(
                        expected,
                        "replayError",
                        "replay_error",
                    )
                    if expected_replay_error is not None and str(error) == str(
                        expected_replay_error
                    ):
                        replay = ()
                        replay_error = str(error)
                    else:
                        raise
                observed = {
                    "eventIds": [event.metadata.event_id for event in log.events],
                    "appendResults": append_results,
                    "appendErrors": append_errors,
                    "replayEventIds": [event.metadata.event_id for event in replay],
                    "replayError": replay_error,
                    "length": len(log),
                }
            elif kind == "stream_cutoff":
                raw_operations = fixture.get("operations", [])
                if not isinstance(raw_operations, list):
                    raise ValueError(
                        "application-protocol stream_cutoff operations must be a list"
                    )
                state = ApplicationProtocolStreamState()
                stream_errors: list[str] = []
                for operation_index, raw_operation in enumerate(raw_operations):
                    if not isinstance(raw_operation, Mapping):
                        raise ValueError(
                            "application-protocol stream_cutoff operation must be a mapping"
                        )
                    raw_metadata = raw_operation.get("metadata", {})
                    if not isinstance(raw_metadata, Mapping):
                        raise ValueError(
                            "application-protocol stream_cutoff operation metadata must be a mapping"
                        )
                    raw_payload = raw_operation.get("payload", {})
                    if not isinstance(raw_payload, Mapping):
                        raise ValueError(
                            "application-protocol stream_cutoff operation payload must be a mapping"
                        )
                    expected_error = raw_operation.get(
                        "expectError", raw_operation.get("expect_error")
                    )
                    try:
                        event = ApplicationProtocolEvent.new(
                            str(
                                raw_operation.get(
                                    "eventKind",
                                    raw_operation.get("event_kind", "RunStarted"),
                                )
                            ),
                            ApplicationProtocolEventMetadata(
                                event_id=str(
                                    raw_metadata.get(
                                        "eventId", raw_metadata.get("event_id", "")
                                    )
                                ),
                                protocol_version=str(
                                    raw_metadata.get(
                                        "protocolVersion",
                                        raw_metadata.get("protocol_version", ""),
                                    )
                                ),
                                run_id=str(
                                    raw_metadata.get(
                                        "runId", raw_metadata.get("run_id", "")
                                    )
                                ),
                                release_id=raw_metadata.get(  # type: ignore[arg-type]
                                    "releaseId",
                                    raw_metadata.get("release_id", "local"),
                                ),
                                turn_id=(
                                    str(raw_metadata["turnId"])
                                    if raw_metadata.get("turnId") is not None
                                    else (
                                        str(raw_metadata["turn_id"])
                                        if raw_metadata.get("turn_id") is not None
                                        else None
                                    )
                                ),
                                operation_id=(
                                    str(raw_metadata["operationId"])
                                    if raw_metadata.get("operationId") is not None
                                    else (
                                        str(raw_metadata["operation_id"])
                                        if raw_metadata.get("operation_id") is not None
                                        else None
                                    )
                                ),
                                sequence=raw_metadata.get("sequence", 0),  # type: ignore[arg-type]
                                cursor=(
                                    str(raw_metadata["cursor"])
                                    if raw_metadata.get("cursor") is not None
                                    else None
                                ),
                                occurred_at_unix_ms=raw_metadata.get(
                                    "occurredAtUnixMs",
                                    raw_metadata.get("occurred_at_unix_ms", 0),
                                ),  # type: ignore[arg-type]
                            ),
                            payload=dict(raw_payload),
                        )
                    except Exception as error:
                        if expected_error is not None and str(error) == str(
                            expected_error
                        ):
                            stream_errors.append(str(expected_error))
                            continue
                        raise
                    accepted = state.accept(event) is not None
                    if accepted is not bool(raw_operation.get("expectAccepted", True)):
                        diagnostics.append(
                            {
                                "code": "ApplicationProtocolAcceptanceMismatch",
                                "message": "application protocol event acceptance did not match expected result",
                                "path": f"$.operations[{operation_index}].expectAccepted",
                            }
                        )
                cutoff_response_id = next(iter(state.cutoffs), None)
                observed = {
                    "acceptedKinds": [event.kind for event in state.accepted_events],
                    "cutoffResponseId": cutoff_response_id,
                    "cutoffLastClientDeliveredSequence": (
                        state.cutoff_for_response(cutoff_response_id)
                        if cutoff_response_id is not None
                        else None
                    ),
                    "errors": stream_errors,
                }
            elif kind in {"capability_negotiation", "capability_negotiation_error"}:
                raw_server = fixture.get("server", {})
                raw_client = fixture.get("client", {})
                if not isinstance(raw_server, Mapping) or not isinstance(
                    raw_client, Mapping
                ):
                    raise ValueError(
                        "application-protocol capabilities must be mappings"
                    )
                try:
                    server = ApplicationProtocolCapabilities(
                        protocol_version=str(
                            raw_server.get(
                                "protocolVersion",
                                raw_server.get("protocol_version", ""),
                            )
                        ),
                        commands=_string_tuple(raw_server.get("commands")),
                        events=_string_tuple(raw_server.get("events")),
                    )
                    client = ApplicationProtocolCapabilities(
                        protocol_version=str(
                            raw_client.get(
                                "protocolVersion",
                                raw_client.get("protocol_version", ""),
                            )
                        ),
                        commands=_string_tuple(raw_client.get("commands")),
                        events=_string_tuple(raw_client.get("events")),
                    )
                    negotiated = server.negotiate(client)
                except Exception as error:
                    if kind != "capability_negotiation_error":
                        raise
                    message = str(error)
                    if "protocol_version must not be empty" in message:
                        observed = {"error": "empty_protocol_version"}
                    elif "version mismatch" in message:
                        observed = {"error": "protocol_version_mismatch"}
                    else:
                        observed = {"error": message}
                else:
                    if kind == "capability_negotiation_error":
                        observed = {"error": "none"}
                    else:
                        observed = {
                            "protocolVersion": negotiated.protocol_version,
                            "commands": list(negotiated.commands),
                            "events": list(negotiated.events),
                        }
            else:
                diagnostics.append(
                    {
                        "code": "ApplicationProtocolKindUnknown",
                        "message": f"application-protocol TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "ApplicationProtocolExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "ApplicationProtocolExpectedMismatch",
                        "message": f"application-protocol observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_approval_review_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.approval_review_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "ApprovalReviewExpectedInvalid",
                    "message": "approval-review TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        observed: dict[str, object] = {}
        try:
            subject_mapping = fixture.get("subject")
            if not isinstance(subject_mapping, Mapping):
                raise ValueError("approval-review TCK case requires subject")
            subject = ResourceSnapshotRef(
                resource_id=str(
                    _first_mapping_value(subject_mapping, "resourceId", "resource_id")
                ),
                digest=str(subject_mapping.get("digest", "")),
                resource_kind=(
                    str(
                        _first_mapping_value(
                            subject_mapping, "resourceKind", "resource_kind"
                        )
                    )
                    if _first_mapping_value(
                        subject_mapping, "resourceKind", "resource_kind"
                    )
                    is not None
                    else None
                ),
                uri=str(subject_mapping["uri"])
                if subject_mapping.get("uri") is not None
                else None,
                metadata=dict(subject_mapping.get("metadata", {}))
                if isinstance(subject_mapping.get("metadata", {}), Mapping)
                else {},
            )

            requested_by_mapping = fixture.get(
                "requestedBy", fixture.get("requested_by", {})
            )
            if not isinstance(requested_by_mapping, Mapping):
                raise ValueError("approval-review TCK case requires requestedBy")
            requested_by = PrincipalRef(
                principal_id=str(
                    _first_mapping_value(
                        requested_by_mapping, "principalId", "principal_id"
                    )
                ),
                tenant_id=(
                    str(
                        _first_mapping_value(
                            requested_by_mapping, "tenantId", "tenant_id"
                        )
                    )
                    if _first_mapping_value(
                        requested_by_mapping, "tenantId", "tenant_id"
                    )
                    is not None
                    else None
                ),
                groups=_string_tuple(requested_by_mapping.get("groups")),
                roles=_string_tuple(requested_by_mapping.get("roles")),
                attributes=dict(requested_by_mapping.get("attributes", {}))
                if isinstance(requested_by_mapping.get("attributes", {}), Mapping)
                else {},
            )
            required_scopes = _string_tuple(
                fixture.get("requiredScopes", fixture.get("required_scopes", ()))
            )
            request = ReviewRequest(
                request_id=str(
                    fixture.get("requestId", fixture.get("request_id", "request-1"))
                ),
                subject=subject,
                requested_by=requested_by,
                required_scopes=required_scopes,
                created_at=str(fixture.get("createdAt", fixture.get("created_at", ""))),
            )

            if kind == "review_digest":
                reordered = _string_tuple(
                    fixture.get("reorderedScopes", fixture.get("reordered_scopes", ()))
                )
                reordered_request = ReviewRequest(
                    request_id="request-reordered",
                    subject=subject,
                    requested_by=requested_by,
                    required_scopes=reordered,
                    created_at=str(
                        fixture.get("createdAt", fixture.get("created_at", ""))
                    ),
                )
                changed_subject_mapping = fixture.get(
                    "changedSubject", fixture.get("changed_subject", {})
                )
                if not isinstance(changed_subject_mapping, Mapping):
                    raise ValueError(
                        "approval-review digest TCK case requires changedSubject"
                    )
                changed_subject = ResourceSnapshotRef(
                    resource_id=str(
                        _first_mapping_value(
                            changed_subject_mapping, "resourceId", "resource_id"
                        )
                    ),
                    digest=str(changed_subject_mapping.get("digest", "")),
                    resource_kind=(
                        str(
                            _first_mapping_value(
                                changed_subject_mapping, "resourceKind", "resource_kind"
                            )
                        )
                        if _first_mapping_value(
                            changed_subject_mapping, "resourceKind", "resource_kind"
                        )
                        is not None
                        else None
                    ),
                    uri=(
                        str(changed_subject_mapping["uri"])
                        if changed_subject_mapping.get("uri") is not None
                        else None
                    ),
                    metadata=dict(changed_subject_mapping.get("metadata", {}))
                    if isinstance(changed_subject_mapping.get("metadata", {}), Mapping)
                    else {},
                )
                changed_request = ReviewRequest(
                    request_id="request-changed",
                    subject=changed_subject,
                    requested_by=requested_by,
                    required_scopes=reordered,
                    created_at=str(
                        fixture.get("createdAt", fixture.get("created_at", ""))
                    ),
                )
                observed = {
                    "sameDigest": request.content_digest()
                    == reordered_request.content_digest(),
                    "changedDigestDifferent": request.content_digest()
                    != changed_request.content_digest(),
                    "requiredScopes": list(request.required_scopes),
                }
            else:
                reviewer_mapping = fixture.get("reviewer", {})
                if not isinstance(reviewer_mapping, Mapping):
                    raise ValueError("approval-review TCK case requires reviewer")
                reviewer = PrincipalRef(
                    principal_id=str(
                        _first_mapping_value(
                            reviewer_mapping, "principalId", "principal_id"
                        )
                    ),
                    tenant_id=(
                        str(
                            _first_mapping_value(
                                reviewer_mapping, "tenantId", "tenant_id"
                            )
                        )
                        if _first_mapping_value(
                            reviewer_mapping, "tenantId", "tenant_id"
                        )
                        is not None
                        else None
                    ),
                    groups=_string_tuple(reviewer_mapping.get("groups")),
                    roles=_string_tuple(reviewer_mapping.get("roles")),
                    attributes=dict(reviewer_mapping.get("attributes", {}))
                    if isinstance(reviewer_mapping.get("attributes", {}), Mapping)
                    else {},
                )
                credentials = []
                raw_credentials = fixture.get("credentials", [])
                if not isinstance(raw_credentials, list):
                    raise ValueError("approval-review TCK credentials must be a list")
                for credential_index, raw_credential in enumerate(raw_credentials):
                    if not isinstance(raw_credential, Mapping):
                        raise ValueError(
                            f"approval-review credential {credential_index} must be a mapping"
                        )
                    credential_reviewer_mapping = raw_credential.get(
                        "reviewer", reviewer_mapping
                    )
                    if not isinstance(credential_reviewer_mapping, Mapping):
                        raise ValueError(
                            f"approval-review credential {credential_index} reviewer must be a mapping"
                        )
                    credential_reviewer = PrincipalRef(
                        principal_id=str(
                            _first_mapping_value(
                                credential_reviewer_mapping,
                                "principalId",
                                "principal_id",
                            )
                        ),
                        tenant_id=(
                            str(
                                _first_mapping_value(
                                    credential_reviewer_mapping, "tenantId", "tenant_id"
                                )
                            )
                            if _first_mapping_value(
                                credential_reviewer_mapping, "tenantId", "tenant_id"
                            )
                            is not None
                            else None
                        ),
                        groups=_string_tuple(credential_reviewer_mapping.get("groups")),
                        roles=_string_tuple(credential_reviewer_mapping.get("roles")),
                        attributes=dict(
                            credential_reviewer_mapping.get("attributes", {})
                        )
                        if isinstance(
                            credential_reviewer_mapping.get("attributes", {}), Mapping
                        )
                        else {},
                    )
                    credentials.append(
                        ReviewerCredential(
                            credential_ref=str(
                                raw_credential.get(
                                    "credentialRef",
                                    raw_credential.get(
                                        "credential_ref",
                                        f"credential-{credential_index + 1}",
                                    ),
                                )
                            ),
                            reviewer=credential_reviewer,
                            scopes=_string_tuple(raw_credential.get("scopes")),
                            issued_at=str(
                                raw_credential.get(
                                    "issuedAt", raw_credential.get("issued_at", "")
                                )
                            ),
                            expires_at=(
                                str(
                                    raw_credential.get(
                                        "expiresAt", raw_credential.get("expires_at")
                                    )
                                )
                                if raw_credential.get(
                                    "expiresAt", raw_credential.get("expires_at")
                                )
                                is not None
                                else None
                            ),
                            metadata=dict(raw_credential.get("metadata", {}))
                            if isinstance(raw_credential.get("metadata", {}), Mapping)
                            else {},
                        )
                    )
                review_mapping = fixture.get("review", {})
                if not isinstance(review_mapping, Mapping):
                    raise ValueError("approval-review TCK case requires review")
                workflow = ReviewWorkflow(
                    request, InMemoryReviewerCredentialProvider(credentials)
                )
                review_id = str(
                    review_mapping.get(
                        "reviewId", review_mapping.get("review_id", "review-1")
                    )
                )
                scope = str(review_mapping.get("scope", ""))
                decision = str(review_mapping.get("decision", "accept"))
                created_at = str(
                    review_mapping.get(
                        "createdAt", review_mapping.get("created_at", "")
                    )
                )
                comments = [
                    str(comment) for comment in review_mapping.get("comments", []) or []
                ]
                if kind == "review_record":
                    review = workflow.record_review(
                        review_id=review_id,
                        reviewer=reviewer,
                        scope=scope,
                        decision=decision,
                        created_at=created_at,
                        comments=comments,
                    )
                    observed = {
                        "credentialRefs": list(review.credential_refs),
                        "completedScopes": list(workflow.completed_scopes()),
                        "complete": workflow.is_complete(),
                        "validForSubject": review.is_valid_for(subject),
                    }
                elif kind == "review_changed_subject":
                    changed_subject_mapping = fixture.get(
                        "changedSubject", fixture.get("changed_subject", {})
                    )
                    if not isinstance(changed_subject_mapping, Mapping):
                        raise ValueError(
                            "approval-review changed-subject TCK case requires changedSubject"
                        )
                    changed_subject = ResourceSnapshotRef(
                        resource_id=str(
                            _first_mapping_value(
                                changed_subject_mapping, "resourceId", "resource_id"
                            )
                        ),
                        digest=str(changed_subject_mapping.get("digest", "")),
                        resource_kind=(
                            str(
                                _first_mapping_value(
                                    changed_subject_mapping,
                                    "resourceKind",
                                    "resource_kind",
                                )
                            )
                            if _first_mapping_value(
                                changed_subject_mapping, "resourceKind", "resource_kind"
                            )
                            is not None
                            else None
                        ),
                        uri=(
                            str(changed_subject_mapping["uri"])
                            if changed_subject_mapping.get("uri") is not None
                            else None
                        ),
                        metadata=dict(changed_subject_mapping.get("metadata", {}))
                        if isinstance(
                            changed_subject_mapping.get("metadata", {}), Mapping
                        )
                        else {},
                    )
                    try:
                        workflow.record_review(
                            review_id=review_id,
                            reviewer=reviewer,
                            scope=scope,
                            decision=decision,
                            created_at=created_at,
                            subject=changed_subject,
                            comments=comments,
                        )
                        observed = {"error": None}
                    except ReviewSubjectChangedError as error:
                        observed = {
                            "error": "review_subject_changed",
                            "expectedDigest": error.expected_digest,
                            "actualDigest": error.actual_digest,
                        }
                elif kind == "review_invalidated":
                    review = workflow.record_review(
                        review_id=review_id,
                        reviewer=reviewer,
                        scope=scope,
                        decision=decision,
                        created_at=created_at,
                        comments=comments,
                    )
                    invalidated_at = review_mapping.get(
                        "invalidatedAt", review_mapping.get("invalidated_at")
                    )
                    if invalidated_at is not None:
                        workflow = workflow.with_review(
                            review.invalidate(str(invalidated_at))
                        )
                    observed = {
                        "completedScopes": list(workflow.completed_scopes()),
                        "complete": workflow.is_complete(),
                    }
                elif kind == "review_missing_credential":
                    try:
                        workflow.record_review(
                            review_id=review_id,
                            reviewer=reviewer,
                            scope=scope,
                            decision=decision,
                            created_at=created_at,
                            comments=comments,
                        )
                        observed = {"error": None}
                    except ReviewCredentialMissingError as error:
                        observed = {
                            "error": "review_credential_missing",
                            "reviewerId": error.reviewer.principal_id,
                            "scope": error.scope,
                        }
                    except ReviewScopeNotRequestedError as error:
                        observed = {
                            "error": "review_scope_not_requested",
                            "scope": error.scope,
                        }
                else:
                    diagnostics.append(
                        {
                            "code": "ApprovalReviewKindUnknown",
                            "message": f"approval-review TCK kind {kind!r} is not supported",
                            "path": "$.kind",
                        }
                    )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "ApprovalReviewExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "ApprovalReviewExpectedMismatch",
                        "message": f"approval-review observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_exhaustion_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.exhaustion_fixture
        policy_mapping = fixture.get("policy")
        if not isinstance(policy_mapping, Mapping):
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="failed",
                diagnostics=(
                    {
                        "code": "ExhaustionPolicyMissing",
                        "message": "exhaustion TCK case requires policy",
                        "path": "$.policy",
                    },
                ),
                observed={},
            )

        continuation = None
        continuation_mapping = policy_mapping.get("continuation")
        if isinstance(continuation_mapping, Mapping):
            max_additional_usage = []
            raw_usage = continuation_mapping.get("maxAdditionalUsage", [])
            if isinstance(raw_usage, list):
                for amount in raw_usage:
                    if isinstance(amount, Mapping):
                        max_additional_usage.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            max_steps = continuation_mapping.get("maxAdditionalSteps")
            continuation = ContinuationEnvelope(
                allowed_work=set(
                    str(item)
                    for item in continuation_mapping.get("allowedWork", []) or []
                ),
                forbidden_work=set(
                    str(item)
                    for item in continuation_mapping.get("forbiddenWork", []) or []
                ),
                max_additional_usage=max_additional_usage,
                max_additional_steps=int(max_steps) if max_steps is not None else None,
                deadline=str(continuation_mapping["deadline"])
                if continuation_mapping.get("deadline")
                else None,
            )

        policy = ExhaustionPolicy.from_preset(
            str(policy_mapping.get("preset", "")),
            unit=str(policy_mapping.get("unit", "")),
            continuation=continuation,
        )
        observed: dict[str, object] = {"admissions": []}
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}

        validation = fixture.get("validate")
        if isinstance(validation, Mapping):
            validation_error = None
            try:
                validate_exhaustion_policy(
                    policy, production=bool(validation.get("production", False))
                )
            except MissingExhaustionBoundaryError:
                validation_error = "missing_exhaustion_boundary"
            observed["validation_error"] = validation_error
            expected_error = validation.get("expectError")
            if expected_error is not None:
                if validation_error != expected_error:
                    diagnostics.append(
                        {
                            "code": "ExhaustionValidationErrorMismatch",
                            "message": "exhaustion validation error did not match expected result",
                            "path": "$.validate.expectError",
                        }
                    )
            elif validation_error is not None:
                diagnostics.append(
                    {
                        "code": "ExhaustionValidationUnexpectedError",
                        "message": "exhaustion validation failed unexpectedly",
                        "path": "$.validate",
                    }
                )

        try:
            atomic_unit = str(fixture.get("atomicUnit", "turn:1"))
            admission_epoch = fixture.get("admissionEpoch", 7)
            profile = str(policy.preset or "finish_current_turn")
            stored_permit = None
            stored_permit_mapping = fixture.get("continuationPermit")
            if isinstance(stored_permit_mapping, Mapping):
                authorized_usage = []
                raw_authorized_usage = stored_permit_mapping.get(
                    "authorizedUsage",
                    [{"kind": "model_output_tokens", "amount": 100, "unit": "tokens"}],
                )
                if isinstance(raw_authorized_usage, list):
                    for amount in raw_authorized_usage:
                        if isinstance(amount, Mapping):
                            authorized_usage.append(
                                UsageAmount(
                                    kind=str(amount.get("kind", "")),
                                    amount=amount.get("amount", 0),
                                    unit=str(amount.get("unit", "")),
                                )
                            )
                stored_permit = BudgetPermit(
                    permit_id=str(stored_permit_mapping.get("permitId", "permit-1")),
                    reservation_refs=("reservation-1",),
                    owner=PolicyResourceRef(
                        str(stored_permit_mapping.get("owner", "worker:1"))
                    ),
                    atomic_unit=PolicyResourceRef(
                        str(stored_permit_mapping.get("atomicUnit", atomic_unit)),
                        resource_kind="turn",
                    ),
                    admission_epoch=stored_permit_mapping.get(  # type: ignore[arg-type]
                        "admissionEpoch",
                        admission_epoch,
                    ),
                    authorized_amounts=authorized_usage,
                    continuation_profile=str(
                        stored_permit_mapping.get("continuationProfile", profile)
                    ),
                    policy_snapshot_digest="sha256:policy",
                    expires_at=str(
                        stored_permit_mapping.get("expiresAt", "2026-06-22T01:00:00Z")
                    ),
                    fencing_tokens={"budget-1": 1},
                )

            controller = ExhaustionController(
                policy,
                atomic_unit_id=atomic_unit,
                admission_epoch=admission_epoch,  # type: ignore[arg-type]
                continuation_permit=stored_permit,
                validation_time=str(fixture["validationTime"])
                if fixture.get("validationTime")
                else None,
            )
        except ValueError as error:
            observed["error"] = str(error)
            for key, expected_value in expected.items():
                if observed.get(str(key)) != expected_value:
                    diagnostics.append(
                        {
                            "code": "ExhaustionExpectedMismatch",
                            "message": f"exhaustion observed {key} did not match expected value",
                            "path": f"$.expected.{key}",
                        }
                    )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )

        admission_results: list[dict[str, object]] = []
        admissions = fixture.get("admissions", [])
        if isinstance(admissions, list):
            for operation_index, operation in enumerate(admissions):
                if not isinstance(operation, Mapping):
                    diagnostics.append(
                        {
                            "code": "ExhaustionAdmissionInvalid",
                            "message": "exhaustion admission operation must be a mapping",
                            "path": f"$.admissions[{operation_index}]",
                        }
                    )
                    continue
                try:
                    permit = None
                    permit_value = operation.get("permit")
                    if isinstance(permit_value, Mapping):
                        authorized_usage = []
                        raw_authorized_usage = permit_value.get(
                            "authorizedUsage",
                            [
                                {
                                    "kind": "model_output_tokens",
                                    "amount": 100,
                                    "unit": "tokens",
                                }
                            ],
                        )
                        if isinstance(raw_authorized_usage, list):
                            for amount in raw_authorized_usage:
                                if isinstance(amount, Mapping):
                                    authorized_usage.append(
                                        UsageAmount(
                                            kind=str(amount.get("kind", "")),
                                            amount=amount.get("amount", 0),
                                            unit=str(amount.get("unit", "")),
                                        )
                                    )
                        permit = BudgetPermit(
                            permit_id=str(permit_value.get("permitId", "permit-1")),
                            reservation_refs=("reservation-1",),
                            owner=PolicyResourceRef(
                                str(permit_value.get("owner", "worker:1"))
                            ),
                            atomic_unit=PolicyResourceRef(
                                str(permit_value.get("atomicUnit", atomic_unit)),
                                resource_kind="turn",
                            ),
                            admission_epoch=permit_value.get(  # type: ignore[arg-type]
                                "admissionEpoch",
                                admission_epoch,
                            ),
                            authorized_amounts=authorized_usage,
                            continuation_profile=str(
                                permit_value.get("continuationProfile", profile)
                            ),
                            policy_snapshot_digest="sha256:policy",
                            expires_at=str(
                                permit_value.get("expiresAt", "2026-06-22T01:00:00Z")
                            ),
                            fencing_tokens={"budget-1": 1},
                        )
                    elif permit_value == "stored":
                        permit = stored_permit
                    elif permit_value not in (None, "none"):
                        diagnostics.append(
                            {
                                "code": "ExhaustionPermitReferenceUnknown",
                                "message": "exhaustion admission references an unknown permit",
                                "path": f"$.admissions[{operation_index}].permit",
                            }
                        )
                except ValueError as error:
                    observed["error"] = str(error)
                    continue

                requested_usage = []
                raw_requested_usage = operation.get("usage", [])
                if isinstance(raw_requested_usage, list):
                    for amount in raw_requested_usage:
                        if isinstance(amount, Mapping):
                            requested_usage.append(
                                UsageAmount(
                                    kind=str(amount.get("kind", "")),
                                    amount=amount.get("amount", 0),
                                    unit=str(amount.get("unit", "")),
                                )
                            )
                decision = controller.admit(
                    str(operation.get("workKind", "")),
                    work_epoch=operation.get("workEpoch", 0),  # type: ignore[arg-type]
                    permit=permit,
                    requested_usage=requested_usage or None,
                )
                admission_results.append(
                    {"allowed": decision.allowed, "reason": decision.reason}
                )
                if decision.allowed is not operation.get("allowed"):
                    diagnostics.append(
                        {
                            "code": "ExhaustionAdmissionAllowedMismatch",
                            "message": "exhaustion admission allowed value did not match expected result",
                            "path": f"$.admissions[{operation_index}].allowed",
                        }
                    )
                if decision.reason != operation.get("reason"):
                    diagnostics.append(
                        {
                            "code": "ExhaustionAdmissionReasonMismatch",
                            "message": "exhaustion admission reason did not match expected result",
                            "path": f"$.admissions[{operation_index}].reason",
                        }
                    )
        observed["admissions"] = admission_results
        observed["usedAdditionalSteps"] = controller.used_additional_steps
        expected = fixture.get("expected")
        if isinstance(expected, Mapping) and "usedAdditionalSteps" in expected:
            if controller.used_additional_steps != expected["usedAdditionalSteps"]:
                diagnostics.append(
                    {
                        "code": "ExhaustionUsedStepsMismatch",
                        "message": "exhaustion used additional steps did not match expected result",
                        "path": "$.expected.usedAdditionalSteps",
                    }
                )

        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_budget_race_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.budget_race_fixture
        ledger = InMemoryBudgetLedger()

        allocated = []
        raw_allocated = fixture.get("allocated", [])
        if isinstance(raw_allocated, list):
            for amount in raw_allocated:
                if isinstance(amount, Mapping):
                    allocated.append(
                        UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=amount.get("amount", 0),
                            unit=str(amount.get("unit", "")),
                        )
                    )
        budget_id = str(fixture.get("budgetId", ""))
        ledger.allocate(
            budget_id,
            PolicyResourceRef(str(fixture.get("scope", ""))),
            allocated,
            policy_ref=str(fixture.get("policyRef", "")),
        )

        outcomes: list[dict[str, object]] = []
        if fixture.get("kind") == "reservation_race":
            reservation_amounts = []
            raw_reservation_amounts = fixture.get("reservationAmounts", [])
            if isinstance(raw_reservation_amounts, list):
                for amount in raw_reservation_amounts:
                    if isinstance(amount, Mapping):
                        reservation_amounts.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            for owner in fixture.get("owners", []) or []:
                try:
                    ledger.reserve(
                        budget_id,
                        PolicyResourceRef(str(owner)),
                        reservation_amounts,
                        purpose=str(fixture.get("reservationPurpose", "provider_call")),
                        expires_at=str(fixture.get("expiresAt", "")),
                    )
                    outcomes.append({"allowed": True, "error": None})
                except BudgetExceededError:
                    outcomes.append({"allowed": False, "error": "BudgetExceeded"})
            balance = ledger.balance(budget_id)
            observed_reserved = [
                {
                    "kind": amount.kind,
                    "amount": int(amount.amount)
                    if amount.amount == amount.amount.to_integral_value()
                    else str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in balance.reserved
            ]
            observed_available = [
                {
                    "kind": amount.kind,
                    "amount": int(amount.amount)
                    if amount.amount == amount.amount.to_integral_value()
                    else str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in balance.available
            ]
            expected_reserved = []
            try:
                for amount in fixture.get("expectedReserved", []):
                    if isinstance(amount, Mapping):
                        usage_amount = UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=amount.get("amount", 0),
                            unit=str(amount.get("unit", "")),
                        )
                        expected_reserved.append(
                            {
                                "kind": usage_amount.kind,
                                "amount": int(usage_amount.amount)
                                if usage_amount.amount
                                == usage_amount.amount.to_integral_value()
                                else str(usage_amount.amount),
                                "unit": usage_amount.unit,
                            }
                        )
            except ValueError as error:
                observed = {"error": str(error)}
                expected = fixture.get("expected", {})
                if not isinstance(expected, Mapping) or observed[
                    "error"
                ] != expected.get("error"):
                    diagnostics.append(
                        {
                            "code": "BudgetRaceExpectedAmountInvalid",
                            "message": "budget-race expected reserved amount was invalid",
                            "path": "$.expectedReserved",
                        }
                    )
                return TckResult(
                    case_id=case.case_id,
                    kind=case.kind,
                    status="passed" if not diagnostics else "failed",
                    diagnostics=tuple(diagnostics),
                    observed=observed,
                )
            expected_available = []
            try:
                for amount in fixture.get("expectedAvailable", []):
                    if isinstance(amount, Mapping):
                        usage_amount = UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=amount.get("amount", 0),
                            unit=str(amount.get("unit", "")),
                        )
                        expected_available.append(
                            {
                                "kind": usage_amount.kind,
                                "amount": int(usage_amount.amount)
                                if usage_amount.amount
                                == usage_amount.amount.to_integral_value()
                                else str(usage_amount.amount),
                                "unit": usage_amount.unit,
                            }
                        )
            except ValueError as error:
                observed = {"error": str(error)}
                expected = fixture.get("expected", {})
                if not isinstance(expected, Mapping) or observed[
                    "error"
                ] != expected.get("error"):
                    diagnostics.append(
                        {
                            "code": "BudgetRaceExpectedAmountInvalid",
                            "message": "budget-race expected available amount was invalid",
                            "path": "$.expectedAvailable",
                        }
                    )
                return TckResult(
                    case_id=case.case_id,
                    kind=case.kind,
                    status="passed" if not diagnostics else "failed",
                    diagnostics=tuple(diagnostics),
                    observed=observed,
                )
            if observed_reserved != expected_reserved:
                diagnostics.append(
                    {
                        "code": "BudgetRaceReservedMismatch",
                        "message": "budget-race reserved amounts did not match expected result",
                        "path": "$.expectedReserved",
                    }
                )
            if observed_available != expected_available:
                diagnostics.append(
                    {
                        "code": "BudgetRaceAvailableMismatch",
                        "message": "budget-race available amounts did not match expected result",
                        "path": "$.expectedAvailable",
                    }
                )
            observed: dict[str, object] = {
                "allowed": sum(1 for outcome in outcomes if outcome["allowed"]),
                "denied": sum(1 for outcome in outcomes if not outcome["allowed"]),
                "denied_errors": [
                    outcome["error"] for outcome in outcomes if not outcome["allowed"]
                ],
                "reserved": observed_reserved,
                "available": observed_available,
            }
        elif fixture.get("kind") == "completion_reserve_race":
            reserve_amounts = []
            raw_reserve_amounts = fixture.get("reserveAmounts", [])
            if isinstance(raw_reserve_amounts, list):
                for amount in raw_reserve_amounts:
                    if isinstance(amount, Mapping):
                        reserve_amounts.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=amount.get("amount", 0),
                                unit=str(amount.get("unit", "")),
                            )
                        )
            ledger.create_completion_reserve(
                str(fixture.get("reserveId", "")),
                budget_id,
                purpose=str(fixture.get("reservePurpose", "finalization")),
                amounts=reserve_amounts,
                spendable_by=tuple(
                    str(spender) for spender in fixture.get("spendableBy", []) or []
                ),
            )
            for spender in fixture.get("spenders", []) or []:
                try:
                    ledger.spend_completion_reserve(
                        str(fixture.get("reserveId", "")),
                        str(spender),
                        expires_at=str(fixture.get("expiresAt", "")),
                    )
                    outcomes.append({"allowed": True, "error": None})
                except BudgetCompletionReserveStateError:
                    outcomes.append(
                        {"allowed": False, "error": "CompletionReserveState"}
                    )
            reserve = ledger.completion_reserve(str(fixture.get("reserveId", "")))
            balance = ledger.balance(budget_id)
            observed_reserved = [
                {
                    "kind": amount.kind,
                    "amount": int(amount.amount)
                    if amount.amount == amount.amount.to_integral_value()
                    else str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in balance.reserved
            ]
            expected_reserved = []
            try:
                for amount in fixture.get("expectedReserved", []):
                    if isinstance(amount, Mapping):
                        usage_amount = UsageAmount(
                            kind=str(amount.get("kind", "")),
                            amount=amount.get("amount", 0),
                            unit=str(amount.get("unit", "")),
                        )
                        expected_reserved.append(
                            {
                                "kind": usage_amount.kind,
                                "amount": int(usage_amount.amount)
                                if usage_amount.amount
                                == usage_amount.amount.to_integral_value()
                                else str(usage_amount.amount),
                                "unit": usage_amount.unit,
                            }
                        )
            except ValueError as error:
                observed = {"error": str(error)}
                expected = fixture.get("expected", {})
                if not isinstance(expected, Mapping) or observed[
                    "error"
                ] != expected.get("error"):
                    diagnostics.append(
                        {
                            "code": "BudgetRaceExpectedAmountInvalid",
                            "message": "budget-race expected reserved amount was invalid",
                            "path": "$.expectedReserved",
                        }
                    )
                return TckResult(
                    case_id=case.case_id,
                    kind=case.kind,
                    status="passed" if not diagnostics else "failed",
                    diagnostics=tuple(diagnostics),
                    observed=observed,
                )
            if reserve.status != fixture.get("expectedReserveStatus"):
                diagnostics.append(
                    {
                        "code": "BudgetRaceReserveStatusMismatch",
                        "message": "budget-race completion reserve status did not match expected result",
                        "path": "$.expectedReserveStatus",
                    }
                )
            if observed_reserved != expected_reserved:
                diagnostics.append(
                    {
                        "code": "BudgetRaceReservedMismatch",
                        "message": "budget-race reserved amounts did not match expected result",
                        "path": "$.expectedReserved",
                    }
                )
            observed = {
                "allowed": sum(1 for outcome in outcomes if outcome["allowed"]),
                "denied": sum(1 for outcome in outcomes if not outcome["allowed"]),
                "denied_errors": [
                    outcome["error"] for outcome in outcomes if not outcome["allowed"]
                ],
                "reserve_status": reserve.status,
                "reserved": observed_reserved,
            }
        else:
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="failed",
                diagnostics=(
                    {
                        "code": "BudgetRaceKindUnknown",
                        "message": f"budget-race TCK kind {fixture.get('kind')!r} is not supported",
                        "path": "$.kind",
                    },
                ),
                observed={},
            )

        if observed["allowed"] != fixture.get("expectedAllowed"):
            diagnostics.append(
                {
                    "code": "BudgetRaceAllowedMismatch",
                    "message": "budget-race allowed count did not match expected result",
                    "path": "$.expectedAllowed",
                }
            )
        if observed["denied"] != fixture.get("expectedDenied"):
            diagnostics.append(
                {
                    "code": "BudgetRaceDeniedMismatch",
                    "message": "budget-race denied count did not match expected result",
                    "path": "$.expectedDenied",
                }
            )
        expected_denied_error = fixture.get("expectedDeniedError")
        if expected_denied_error is not None and any(
            error != expected_denied_error for error in observed["denied_errors"]
        ):
            diagnostics.append(
                {
                    "code": "BudgetRaceDeniedErrorMismatch",
                    "message": "budget-race denied error did not match expected result",
                    "path": "$.expectedDeniedError",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_conversation_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.conversation_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "ConversationExpectedInvalid",
                    "message": "conversation TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        observed: dict[str, object] = {}
        store = InMemoryConversationStore()
        conversation_id = str(
            fixture.get("conversationId", fixture.get("conversation_id", "conv-1"))
        )

        try:
            if kind == "turn_commit":
                turn_id = str(fixture.get("turnId", fixture.get("turn_id", "turn-1")))
                raw_message = fixture.get("message", {})
                if not isinstance(raw_message, Mapping):
                    raw_message = {}
                    diagnostics.append(
                        {
                            "code": "ConversationMessageInvalid",
                            "message": "conversation TCK message must be a mapping",
                            "path": "$.message",
                        }
                    )
                parent_message_id = raw_message.get(
                    "parentMessageId", raw_message.get("parent_message_id")
                )
                store.create(Conversation(conversation_id=conversation_id))
                store.begin_turn(conversation_id, expected_revision=0, turn_id=turn_id)
                draft_turn = store.append_turn_message(
                    turn_id,
                    Message(
                        message_id=str(
                            raw_message.get(
                                "messageId", raw_message.get("message_id", "msg-1")
                            )
                        ),
                        role=str(raw_message.get("role", "assistant")),
                        parts=(
                            ContentPart(
                                kind="text", text=str(raw_message.get("text", ""))
                            ),
                        ),
                        parent_message_id=parent_message_id
                        if isinstance(parent_message_id, str)
                        else None,
                    ),
                )
                before_commit = store.get(conversation_id)
                completed_turn = store.commit_turn(turn_id)
                after_commit = store.get(conversation_id)
                observed = {
                    "draftStatus": draft_turn.status,
                    "draftMessageStatuses": [
                        message.status for message in draft_turn.messages
                    ],
                    "preCommitMessageCount": len(before_commit.conversation.messages),
                    "turnStatus": completed_turn.status,
                    "committedRevision": completed_turn.committed_revision,
                    "committedMessageIds": list(completed_turn.committed_message_ids),
                    "conversationRevision": after_commit.revision,
                    "conversationMessageIds": [
                        message.message_id
                        for message in after_commit.conversation.messages
                    ],
                    "conversationMessageStatuses": [
                        message.status for message in after_commit.conversation.messages
                    ],
                }
            elif kind in {"abort_turn", "policy_stop_turn"}:
                turn_id = str(fixture.get("turnId", fixture.get("turn_id", "turn-1")))
                raw_message = fixture.get("message", {})
                if not isinstance(raw_message, Mapping):
                    raw_message = {}
                    diagnostics.append(
                        {
                            "code": "ConversationMessageInvalid",
                            "message": "conversation TCK message must be a mapping",
                            "path": "$.message",
                        }
                    )
                parent_message_id = raw_message.get(
                    "parentMessageId", raw_message.get("parent_message_id")
                )
                store.create(Conversation(conversation_id=conversation_id))
                store.begin_turn(conversation_id, expected_revision=0, turn_id=turn_id)
                store.append_turn_message(
                    turn_id,
                    Message(
                        message_id=str(
                            raw_message.get(
                                "messageId", raw_message.get("message_id", "msg-1")
                            )
                        ),
                        role=str(raw_message.get("role", "assistant")),
                        parts=(
                            ContentPart(
                                kind="text", text=str(raw_message.get("text", ""))
                            ),
                        ),
                        parent_message_id=parent_message_id
                        if isinstance(parent_message_id, str)
                        else None,
                    ),
                )
                terminal_turn = (
                    store.abort_turn(turn_id)
                    if kind == "abort_turn"
                    else store.policy_stop_turn(turn_id)
                )
                terminal_commit_denied = False
                try:
                    store.commit_turn(turn_id)
                except TurnConflictError:
                    terminal_commit_denied = True
                snapshot = store.get(conversation_id)
                observed = {
                    "turnStatus": terminal_turn.status,
                    "turnMessageStatuses": [
                        message.status for message in terminal_turn.messages
                    ],
                    "conversationMessageCount": len(snapshot.conversation.messages),
                    "terminalCommitDenied": terminal_commit_denied,
                }
            elif kind == "commit_conflict":
                turn_id = str(fixture.get("turnId", fixture.get("turn_id", "turn-1")))
                raw_draft = fixture.get(
                    "draftMessage", fixture.get("draft_message", {})
                )
                if not isinstance(raw_draft, Mapping):
                    raw_draft = {}
                    diagnostics.append(
                        {
                            "code": "ConversationDraftInvalid",
                            "message": "conversation TCK draftMessage must be a mapping",
                            "path": "$.draftMessage",
                        }
                    )
                raw_conflict = fixture.get(
                    "conflictingMessage", fixture.get("conflicting_message", {})
                )
                if not isinstance(raw_conflict, Mapping):
                    raw_conflict = {}
                    diagnostics.append(
                        {
                            "code": "ConversationConflictMessageInvalid",
                            "message": "conversation TCK conflictingMessage must be a mapping",
                            "path": "$.conflictingMessage",
                        }
                    )
                store.create(Conversation(conversation_id=conversation_id))
                store.begin_turn(conversation_id, expected_revision=0, turn_id=turn_id)
                draft_parent = raw_draft.get(
                    "parentMessageId", raw_draft.get("parent_message_id")
                )
                store.append_turn_message(
                    turn_id,
                    Message(
                        message_id=str(
                            raw_draft.get(
                                "messageId", raw_draft.get("message_id", "msg-draft")
                            )
                        ),
                        role=str(raw_draft.get("role", "assistant")),
                        parts=(
                            ContentPart(
                                kind="text", text=str(raw_draft.get("text", ""))
                            ),
                        ),
                        parent_message_id=draft_parent
                        if isinstance(draft_parent, str)
                        else None,
                    ),
                )
                conflict_parent = raw_conflict.get(
                    "parentMessageId", raw_conflict.get("parent_message_id")
                )
                store.append_messages(
                    conversation_id,
                    expected_revision=0,
                    messages=[
                        Message(
                            message_id=str(
                                raw_conflict.get(
                                    "messageId",
                                    raw_conflict.get("message_id", "msg-other"),
                                )
                            ),
                            role=str(raw_conflict.get("role", "user")),
                            parts=(
                                ContentPart(
                                    kind="text", text=str(raw_conflict.get("text", ""))
                                ),
                            ),
                            parent_message_id=conflict_parent
                            if isinstance(conflict_parent, str)
                            else None,
                        )
                    ],
                )
                commit_conflict = False
                try:
                    store.commit_turn(turn_id)
                except ConversationConflictError:
                    commit_conflict = True
                failed_turn = store.get_turn(turn_id)
                snapshot = store.get(conversation_id)
                observed = {
                    "commitConflict": commit_conflict,
                    "turnStatus": failed_turn.status,
                    "conversationRevision": snapshot.revision,
                    "conversationMessageIds": [
                        message.message_id for message in snapshot.conversation.messages
                    ],
                    "committedMessageIds": list(failed_turn.committed_message_ids),
                }
            elif kind == "branch_regenerate":
                raw_messages = fixture.get("messages", [])
                if not isinstance(raw_messages, list) or not all(
                    isinstance(message, Mapping) for message in raw_messages
                ):
                    raw_messages = []
                    diagnostics.append(
                        {
                            "code": "ConversationMessagesInvalid",
                            "message": "conversation TCK messages must be a list of mappings",
                            "path": "$.messages",
                        }
                    )
                messages: list[Message] = []
                for raw_message in raw_messages:
                    parent_message_id = raw_message.get(
                        "parentMessageId", raw_message.get("parent_message_id")
                    )
                    messages.append(
                        Message(
                            message_id=str(
                                raw_message.get(
                                    "messageId", raw_message.get("message_id", "msg")
                                )
                            ),
                            role=str(raw_message.get("role", "user")),
                            parts=(
                                ContentPart(
                                    kind="text", text=str(raw_message.get("text", ""))
                                ),
                            ),
                            parent_message_id=parent_message_id
                            if isinstance(parent_message_id, str)
                            else None,
                        )
                    )
                branch_from_message_id = str(
                    fixture.get(
                        "branchFromMessageId",
                        fixture.get("branch_from_message_id", "msg-user"),
                    )
                )
                branch_conversation_id = str(
                    fixture.get(
                        "branchConversationId",
                        fixture.get("branch_conversation_id", "conv-branch"),
                    )
                )
                regenerate_assistant_message_id = str(
                    fixture.get(
                        "regenerateAssistantMessageId",
                        fixture.get("regenerate_assistant_message_id", "msg-assistant"),
                    )
                )
                regenerate_conversation_id = str(
                    fixture.get(
                        "regenerateConversationId",
                        fixture.get("regenerate_conversation_id", "conv-regenerated"),
                    )
                )
                store.create(Conversation(conversation_id=conversation_id))
                source_revision = store.append_messages(
                    conversation_id,
                    expected_revision=0,
                    messages=messages,
                )
                branch = store.branch(
                    BranchRequest(
                        conversation_id=conversation_id,
                        from_message_id=branch_from_message_id,
                        expected_revision=source_revision,
                        new_conversation_id=branch_conversation_id,
                    )
                )
                regenerated = store.regenerate(
                    RegenerateRequest(
                        conversation_id=conversation_id,
                        assistant_message_id=regenerate_assistant_message_id,
                        expected_revision=source_revision,
                        new_conversation_id=regenerate_conversation_id,
                    )
                )
                source = store.get(conversation_id)
                observed = {
                    "branchId": branch.conversation_id,
                    "branchOf": branch.branch_of,
                    "branchFrom": branch.branched_from_message_id,
                    "branchMessageIds": [
                        message.message_id for message in branch.messages
                    ],
                    "branchSourceRevision": branch.metadata.get("source_revision"),
                    "regenerateId": regenerated.conversation_id,
                    "regenerateBranchOf": regenerated.branch_of,
                    "regenerateFrom": regenerated.branched_from_message_id,
                    "regenerateMessageIds": [
                        message.message_id for message in regenerated.messages
                    ],
                    "regeneratedFromMessageId": regenerated.metadata.get(
                        "regenerated_from_message_id"
                    ),
                    "regenerateSourceRevision": regenerated.metadata.get(
                        "source_revision"
                    ),
                    "sourceRevision": source.revision,
                    "sourceMessageStatuses": [
                        message.status for message in source.conversation.messages
                    ],
                }
            elif kind == "branch_attachments":
                raw_messages = fixture.get("messages", [])
                if not isinstance(raw_messages, list) or not all(
                    isinstance(message, Mapping) for message in raw_messages
                ):
                    raw_messages = []
                    diagnostics.append(
                        {
                            "code": "ConversationMessagesInvalid",
                            "message": "conversation TCK messages must be a list of mappings",
                            "path": "$.messages",
                        }
                    )
                messages: list[Message] = []
                for raw_message in raw_messages:
                    parent_message_id = raw_message.get(
                        "parentMessageId", raw_message.get("parent_message_id")
                    )
                    messages.append(
                        Message(
                            message_id=str(
                                raw_message.get(
                                    "messageId", raw_message.get("message_id", "msg")
                                )
                            ),
                            role=str(raw_message.get("role", "user")),
                            parts=(
                                ContentPart(
                                    kind="text", text=str(raw_message.get("text", ""))
                                ),
                            ),
                            parent_message_id=parent_message_id
                            if isinstance(parent_message_id, str)
                            else None,
                        )
                    )
                raw_attachments = fixture.get("attachments", [])
                if not isinstance(raw_attachments, list) or not all(
                    isinstance(attachment, Mapping) for attachment in raw_attachments
                ):
                    raw_attachments = []
                    diagnostics.append(
                        {
                            "code": "ConversationAttachmentsInvalid",
                            "message": "conversation TCK attachments must be a list of mappings",
                            "path": "$.attachments",
                        }
                    )
                branch_from_message_id = str(
                    fixture.get(
                        "branchFromMessageId",
                        fixture.get("branch_from_message_id", "msg-1"),
                    )
                )
                branch_conversation_id = str(
                    fixture.get(
                        "branchConversationId",
                        fixture.get("branch_conversation_id", "conv-branch"),
                    )
                )
                branch_without_attachments_id = str(
                    fixture.get(
                        "branchWithoutAttachmentsId",
                        fixture.get(
                            "branch_without_attachments_id",
                            "conv-branch-without-attachments",
                        ),
                    )
                )
                store.create(Conversation(conversation_id=conversation_id))
                source_revision = store.append_messages(
                    conversation_id,
                    expected_revision=0,
                    messages=messages,
                )
                for raw_attachment in raw_attachments:
                    source_revision = store.add_attachment(
                        conversation_id,
                        FileAttachment(
                            attachment_id=str(
                                raw_attachment.get(
                                    "attachmentId",
                                    raw_attachment.get("attachment_id", "att"),
                                )
                            ),
                            asset=ArtifactRef(
                                str(
                                    raw_attachment.get(
                                        "artifactId",
                                        raw_attachment.get("artifact_id", "artifact"),
                                    )
                                ),
                                str(
                                    raw_attachment.get("uri", "blob://attachments/file")
                                ),
                            ),
                            scope=str(raw_attachment.get("scope", "message")),
                            purpose=str(raw_attachment.get("purpose", "retrieval")),
                            ingestion_status=str(
                                raw_attachment.get(
                                    "ingestionStatus",
                                    raw_attachment.get("ingestion_status", "ready"),
                                )
                            ),
                            message_id=(
                                str(
                                    raw_attachment.get(
                                        "messageId", raw_attachment.get("message_id")
                                    )
                                )
                                if raw_attachment.get(
                                    "messageId", raw_attachment.get("message_id")
                                )
                                is not None
                                else None
                            ),
                        ),
                        expected_revision=source_revision,
                    )
                branch = store.branch(
                    BranchRequest(
                        conversation_id=conversation_id,
                        from_message_id=branch_from_message_id,
                        expected_revision=source_revision,
                        new_conversation_id=branch_conversation_id,
                    )
                )
                request_without_attachments = BranchRequest(
                    conversation_id=conversation_id,
                    from_message_id=branch_from_message_id,
                    expected_revision=source_revision,
                    new_conversation_id=branch_without_attachments_id,
                    include_attachments=False,
                )
                branch_without_attachments = store.branch(request_without_attachments)
                source = store.get(conversation_id)
                observed = {
                    "branchAttachmentIds": [
                        attachment.attachment_id for attachment in branch.attachments
                    ],
                    "branchWithoutAttachmentIds": [
                        attachment.attachment_id
                        for attachment in branch_without_attachments.attachments
                    ],
                    "branchMessageIds": [
                        message.message_id for message in branch.messages
                    ],
                    "sourceAttachmentIds": [
                        attachment.attachment_id
                        for attachment in source.conversation.attachments
                    ],
                }
            elif kind == "attachment_resolution":
                raw_attachments = fixture.get("attachments", [])
                if not isinstance(raw_attachments, list) or not all(
                    isinstance(attachment, Mapping) for attachment in raw_attachments
                ):
                    raw_attachments = []
                    diagnostics.append(
                        {
                            "code": "ConversationAttachmentsInvalid",
                            "message": "conversation TCK attachments must be a list of mappings",
                            "path": "$.attachments",
                        }
                    )
                raw_message_ids = fixture.get(
                    "messageIds", fixture.get("message_ids", [])
                )
                if not isinstance(raw_message_ids, list):
                    raw_message_ids = []
                    diagnostics.append(
                        {
                            "code": "ConversationMessageIdsInvalid",
                            "message": "conversation TCK messageIds must be a list",
                            "path": "$.messageIds",
                        }
                    )
                message_ids = [str(message_id) for message_id in raw_message_ids]
                conversation_message_ids: list[str] = []
                for raw_attachment in raw_attachments:
                    attachment_scope = raw_attachment.get("scope", "message")
                    attachment_message_id = raw_attachment.get(
                        "messageId",
                        raw_attachment.get("message_id"),
                    )
                    if (
                        attachment_scope == "message"
                        and attachment_message_id is not None
                        and str(attachment_message_id) not in conversation_message_ids
                    ):
                        conversation_message_ids.append(str(attachment_message_id))
                store.create(
                    Conversation(
                        conversation_id=conversation_id,
                        messages=tuple(
                            Message(message_id=message_id, role="user")
                            for message_id in conversation_message_ids
                        ),
                    )
                )
                source_revision = 0
                for raw_attachment in raw_attachments:
                    source_revision = store.add_attachment(
                        conversation_id,
                        FileAttachment(
                            attachment_id=str(
                                raw_attachment.get(
                                    "attachmentId",
                                    raw_attachment.get("attachment_id", "att"),
                                )
                            ),
                            asset=ArtifactRef(
                                str(
                                    raw_attachment.get(
                                        "artifactId",
                                        raw_attachment.get("artifact_id", "artifact"),
                                    )
                                ),
                                str(
                                    raw_attachment.get("uri", "blob://attachments/file")
                                ),
                            ),
                            scope=str(raw_attachment.get("scope", "message")),
                            purpose=str(raw_attachment.get("purpose", "retrieval")),
                            ingestion_status=str(
                                raw_attachment.get(
                                    "ingestionStatus",
                                    raw_attachment.get("ingestion_status", "ready"),
                                )
                            ),
                            message_id=(
                                str(
                                    raw_attachment.get(
                                        "messageId", raw_attachment.get("message_id")
                                    )
                                )
                                if raw_attachment.get(
                                    "messageId", raw_attachment.get("message_id")
                                )
                                is not None
                                else None
                            ),
                        ),
                        expected_revision=source_revision,
                    )
                with_conversation_scope = store.resolve_attachments(
                    conversation_id,
                    message_ids,
                    include_conversation_scope=True,
                )
                without_conversation_scope = store.resolve_attachments(
                    conversation_id,
                    message_ids,
                    include_conversation_scope=False,
                )
                observed = {
                    "withConversationScopeIds": [
                        attachment.attachment_id
                        for attachment in with_conversation_scope
                    ],
                    "withoutConversationScopeIds": [
                        attachment.attachment_id
                        for attachment in without_conversation_scope
                    ],
                }
            elif kind == "archive_conversation":
                raw_message = fixture.get("message", {})
                if not isinstance(raw_message, Mapping):
                    raw_message = {}
                    diagnostics.append(
                        {
                            "code": "ConversationMessageInvalid",
                            "message": "conversation TCK message must be a mapping",
                            "path": "$.message",
                        }
                    )
                parent_message_id = raw_message.get(
                    "parentMessageId", raw_message.get("parent_message_id")
                )
                store.create(Conversation(conversation_id=conversation_id))
                archive_revision = store.archive(
                    conversation_id,
                    expected_revision=0,
                )
                append_rejected = False
                try:
                    store.append_messages(
                        conversation_id,
                        expected_revision=archive_revision,
                        messages=[
                            Message(
                                message_id=str(
                                    raw_message.get(
                                        "messageId",
                                        raw_message.get("message_id", "msg-1"),
                                    )
                                ),
                                role=str(raw_message.get("role", "user")),
                                parts=(
                                    ContentPart(
                                        kind="text",
                                        text=str(raw_message.get("text", "")),
                                    ),
                                ),
                                parent_message_id=parent_message_id
                                if isinstance(parent_message_id, str)
                                else None,
                            )
                        ],
                    )
                except ConversationArchivedError:
                    append_rejected = True
                snapshot = store.get(conversation_id)
                observed = {
                    "archiveRevision": archive_revision,
                    "archived": snapshot.conversation.archived,
                    "appendRejected": append_rejected,
                    "messageCount": len(snapshot.conversation.messages),
                }
            elif kind == "compaction_record":
                raw_messages = fixture.get("messages", [])
                if not isinstance(raw_messages, list) or not all(
                    isinstance(message, Mapping) for message in raw_messages
                ):
                    raw_messages = []
                    diagnostics.append(
                        {
                            "code": "ConversationMessagesInvalid",
                            "message": "conversation TCK messages must be a list of mappings",
                            "path": "$.messages",
                        }
                    )
                messages: list[Message] = []
                for raw_message in raw_messages:
                    parent_message_id = raw_message.get(
                        "parentMessageId", raw_message.get("parent_message_id")
                    )
                    messages.append(
                        Message(
                            message_id=str(
                                raw_message.get(
                                    "messageId", raw_message.get("message_id", "msg")
                                )
                            ),
                            role=str(raw_message.get("role", "user")),
                            parts=(
                                ContentPart(
                                    kind="text", text=str(raw_message.get("text", ""))
                                ),
                            ),
                            parent_message_id=parent_message_id
                            if isinstance(parent_message_id, str)
                            else None,
                        )
                    )
                raw_compaction = fixture.get("compaction", {})
                if not isinstance(raw_compaction, Mapping):
                    raw_compaction = {}
                    diagnostics.append(
                        {
                            "code": "ConversationCompactionInvalid",
                            "message": "conversation TCK compaction must be a mapping",
                            "path": "$.compaction",
                        }
                    )
                source_message_ids = raw_compaction.get(
                    "sourceMessageIds",
                    raw_compaction.get("source_message_ids", []),
                )
                if not isinstance(source_message_ids, list):
                    source_message_ids = []
                    diagnostics.append(
                        {
                            "code": "ConversationCompactionSourceInvalid",
                            "message": "conversation TCK compaction sourceMessageIds must be a list",
                            "path": "$.compaction.sourceMessageIds",
                        }
                    )
                store.create(Conversation(conversation_id=conversation_id))
                source_revision = store.append_messages(
                    conversation_id,
                    expected_revision=0,
                    messages=messages,
                )
                revision = store.record_compaction(
                    conversation_id,
                    CompactionRecord(
                        compaction_id=str(
                            raw_compaction.get(
                                "compactionId",
                                raw_compaction.get("compaction_id", "compact-1"),
                            )
                        ),
                        source_message_ids=tuple(
                            str(message_id) for message_id in source_message_ids
                        ),
                        output_message_id=str(
                            raw_compaction.get(
                                "outputMessageId",
                                raw_compaction.get("output_message_id", "msg-summary"),
                            )
                        ),
                        method=str(raw_compaction.get("method", "summary_memory")),
                        token_before=int(
                            raw_compaction.get(
                                "tokenBefore",
                                raw_compaction.get("token_before", 0),
                            )
                        ),
                        token_after=int(
                            raw_compaction.get(
                                "tokenAfter",
                                raw_compaction.get("token_after", 0),
                            )
                        ),
                        model=(
                            str(raw_compaction.get("model"))
                            if raw_compaction.get("model") is not None
                            else None
                        ),
                    ),
                    expected_revision=source_revision,
                )
                snapshot = store.get(conversation_id)
                compaction = snapshot.conversation.compactions[0]
                observed = {
                    "revision": revision,
                    "compactionIds": [
                        record.compaction_id
                        for record in snapshot.conversation.compactions
                    ],
                    "sourceMessageIds": list(compaction.source_message_ids),
                    "outputMessageId": compaction.output_message_id,
                    "method": compaction.method,
                    "tokenBefore": compaction.token_before,
                    "tokenAfter": compaction.token_after,
                    "model": compaction.model,
                }
            elif kind == "delete_retention":
                raw_messages = fixture.get("messages", [])
                if not isinstance(raw_messages, list) or not all(
                    isinstance(message, Mapping) for message in raw_messages
                ):
                    raw_messages = []
                    diagnostics.append(
                        {
                            "code": "ConversationMessagesInvalid",
                            "message": "conversation TCK messages must be a list of mappings",
                            "path": "$.messages",
                        }
                    )
                messages: list[Message] = []
                for raw_message in raw_messages:
                    parent_message_id = raw_message.get(
                        "parentMessageId", raw_message.get("parent_message_id")
                    )
                    messages.append(
                        Message(
                            message_id=str(
                                raw_message.get(
                                    "messageId", raw_message.get("message_id", "msg")
                                )
                            ),
                            role=str(raw_message.get("role", "user")),
                            parts=(
                                ContentPart(
                                    kind="text", text=str(raw_message.get("text", ""))
                                ),
                            ),
                            parent_message_id=parent_message_id
                            if isinstance(parent_message_id, str)
                            else None,
                        )
                    )
                raw_attachments = fixture.get("attachments", [])
                if not isinstance(raw_attachments, list) or not all(
                    isinstance(attachment, Mapping) for attachment in raw_attachments
                ):
                    raw_attachments = []
                    diagnostics.append(
                        {
                            "code": "ConversationAttachmentsInvalid",
                            "message": "conversation TCK attachments must be a list of mappings",
                            "path": "$.attachments",
                        }
                    )
                raw_compaction = fixture.get("compaction", {})
                if not isinstance(raw_compaction, Mapping):
                    raw_compaction = {}
                    diagnostics.append(
                        {
                            "code": "ConversationCompactionInvalid",
                            "message": "conversation TCK compaction must be a mapping",
                            "path": "$.compaction",
                        }
                    )
                source_message_ids = raw_compaction.get(
                    "sourceMessageIds",
                    raw_compaction.get("source_message_ids", []),
                )
                if not isinstance(source_message_ids, list):
                    source_message_ids = []
                    diagnostics.append(
                        {
                            "code": "ConversationCompactionSourceInvalid",
                            "message": "conversation TCK compaction sourceMessageIds must be a list",
                            "path": "$.compaction.sourceMessageIds",
                        }
                    )
                store.create(Conversation(conversation_id=conversation_id))
                source_revision = store.append_messages(
                    conversation_id,
                    expected_revision=0,
                    messages=messages,
                )
                for raw_attachment in raw_attachments:
                    source_revision = store.add_attachment(
                        conversation_id,
                        FileAttachment(
                            attachment_id=str(
                                raw_attachment.get(
                                    "attachmentId",
                                    raw_attachment.get("attachment_id", "att"),
                                )
                            ),
                            asset=ArtifactRef(
                                str(
                                    raw_attachment.get(
                                        "artifactId",
                                        raw_attachment.get("artifact_id", "artifact"),
                                    )
                                ),
                                str(
                                    raw_attachment.get("uri", "blob://attachments/file")
                                ),
                            ),
                            scope=str(raw_attachment.get("scope", "message")),
                            purpose=str(raw_attachment.get("purpose", "retrieval")),
                            ingestion_status=str(
                                raw_attachment.get(
                                    "ingestionStatus",
                                    raw_attachment.get("ingestion_status", "ready"),
                                )
                            ),
                            message_id=(
                                str(
                                    raw_attachment.get(
                                        "messageId", raw_attachment.get("message_id")
                                    )
                                )
                                if raw_attachment.get(
                                    "messageId", raw_attachment.get("message_id")
                                )
                                is not None
                                else None
                            ),
                        ),
                        expected_revision=source_revision,
                    )
                source_revision = store.record_compaction(
                    conversation_id,
                    CompactionRecord(
                        compaction_id=str(
                            raw_compaction.get(
                                "compactionId",
                                raw_compaction.get("compaction_id", "compact-1"),
                            )
                        ),
                        source_message_ids=tuple(
                            str(message_id) for message_id in source_message_ids
                        ),
                        output_message_id=str(
                            raw_compaction.get(
                                "outputMessageId",
                                raw_compaction.get("output_message_id", "msg-summary"),
                            )
                        ),
                        method=str(raw_compaction.get("method", "summary_memory")),
                        token_before=int(
                            raw_compaction.get(
                                "tokenBefore",
                                raw_compaction.get("token_before", 0),
                            )
                        ),
                        token_after=int(
                            raw_compaction.get(
                                "tokenAfter",
                                raw_compaction.get("token_after", 0),
                            )
                        ),
                    ),
                    expected_revision=source_revision,
                )
                tombstone_revision = store.delete(
                    conversation_id,
                    policy="tombstone",
                    expected_revision=source_revision,
                )
                tombstone = store.get(conversation_id).conversation

                hard_delete_conversation_id = str(
                    fixture.get(
                        "hardDeleteConversationId",
                        fixture.get(
                            "hard_delete_conversation_id", f"{conversation_id}-hard"
                        ),
                    )
                )
                store.create(
                    Conversation(
                        conversation_id=hard_delete_conversation_id,
                        messages=tuple(messages),
                    )
                )
                store.delete(
                    hard_delete_conversation_id,
                    policy="hard",
                    expected_revision=0,
                )
                hard_deleted = False
                try:
                    store.get(hard_delete_conversation_id)
                except ConversationNotFoundError:
                    hard_deleted = True

                observed = {
                    "tombstoneRevision": tombstone_revision,
                    "tombstoneArchived": tombstone.archived,
                    "tombstoneDeleted": tombstone.metadata.get("deleted"),
                    "tombstoneMessageCount": len(tombstone.messages),
                    "tombstoneAttachmentCount": len(tombstone.attachments),
                    "tombstoneCompactionCount": len(tombstone.compactions),
                    "hardDeleted": hard_deleted,
                }
            else:
                diagnostics.append(
                    {
                        "code": "ConversationKindUnknown",
                        "message": f"conversation TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "ConversationExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "ConversationExpectedMismatch",
                        "message": f"conversation observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_documents_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.documents_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "DocumentsExpectedInvalid",
                    "message": "documents TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        source_uri = str(
            fixture.get(
                "sourceUri", fixture.get("source_uri", "file:///tmp/document.txt")
            )
        )
        text = str(fixture.get("text", ""))
        observed_at = str(
            fixture.get(
                "observedAt", fixture.get("observed_at", "2026-06-22T00:00:00Z")
            )
        )
        raw_filename = fixture.get("filename")
        filename = raw_filename if isinstance(raw_filename, str) else None
        asset, revision = create_local_text_revision(
            source_uri, text, observed_at, filename=filename
        )
        raw_acl = fixture.get("acl")
        if isinstance(raw_acl, Mapping):
            revision = replace(revision, acl=dict(raw_acl))
        document = parse_plain_text_document(asset, revision, text)
        observed: dict[str, object] = {}

        try:
            if kind == "plain_text_parse":
                observed = {
                    "contentHash": revision.content_hash,
                    "assetId": asset.asset_id,
                    "artifactMediaType": revision.artifact.media_type,
                    "artifactSizeBytes": revision.artifact.size_bytes,
                    "parserProcessorId": document.parser.get("processor_id"),
                    "elementTexts": [element.content for element in document.elements],
                    "elementSpans": [
                        [element.location.char_start, element.location.char_end]
                        for element in document.elements
                    ],
                    "documentLineageConsistent": (
                        document.asset_id == asset.asset_id
                        and document.revision_id == revision.revision_id
                        and document.document_id == f"doc:{revision.revision_id}"
                    ),
                    "assetCurrentRevisionMatches": asset.current_revision_id
                    == revision.revision_id,
                }
            elif kind == "line_chunks":
                max_elements = fixture.get(
                    "maxElements", fixture.get("max_elements", 8)
                )
                if isinstance(max_elements, bool) or not isinstance(max_elements, int):
                    raise ValueError("documents TCK maxElements must be an integer")
                chunks = chunk_document_by_lines(
                    document, revision, max_elements=max_elements
                )
                observed = {
                    "chunkTexts": [chunk.text for chunk in chunks],
                    "chunkSpans": [
                        [
                            chunk.source_refs[0].locator.char_start
                            if chunk.source_refs[0].locator
                            else None,
                            chunk.source_refs[0].locator.char_end
                            if chunk.source_refs[0].locator
                            else None,
                        ]
                        for chunk in chunks
                    ],
                    "chunkElementCounts": [len(chunk.element_ids) for chunk in chunks],
                    "sourceRefKinds": [
                        chunk.source_refs[0].source_kind if chunk.source_refs else None
                        for chunk in chunks
                    ],
                    "sourceRefDigestMatches": all(
                        chunk.source_refs
                        and chunk.source_refs[0].digest == revision.content_hash
                        for chunk in chunks
                    ),
                    "sourceRefLocatorConsistent": all(
                        chunk.source_refs
                        and chunk.source_refs[0].locator is not None
                        and chunk.source_refs[0].locator.asset_id == chunk.asset_id
                        and chunk.source_refs[0].locator.revision_id
                        == chunk.revision_id
                        and chunk.source_refs[0].locator.document_id
                        == chunk.document_id
                        and chunk.source_refs[0].locator.chunk_id == chunk.chunk_id
                        for chunk in chunks
                    ),
                    "chunkAcls": [chunk.acl for chunk in chunks],
                }
            elif kind == "invalid_chunk_size":
                max_elements = fixture.get(
                    "maxElements", fixture.get("max_elements", 0)
                )
                if isinstance(max_elements, bool) or not isinstance(max_elements, int):
                    raise ValueError("documents TCK maxElements must be an integer")
                error = None
                try:
                    chunk_document_by_lines(
                        document, revision, max_elements=max_elements
                    )
                except ValueError:
                    error = "invalid_max_elements"
                observed = {"error": error}
            elif kind == "parser_selection_lock":
                raw_artifact = fixture.get("artifact", {})
                if not isinstance(raw_artifact, Mapping):
                    raise ValueError("documents TCK parser artifact must be a mapping")
                artifact = ArtifactRef(
                    str(
                        raw_artifact.get(
                            "artifactId", raw_artifact.get("artifact_id", "artifact-1")
                        )
                    ),
                    str(raw_artifact.get("uri", "file:///tmp/document.txt")),
                    media_type=(
                        str(raw_artifact["mediaType"])
                        if raw_artifact.get("mediaType") is not None
                        else (
                            str(raw_artifact["media_type"])
                            if raw_artifact.get("media_type") is not None
                            else None
                        )
                    ),
                    checksum=(
                        str(raw_artifact["checksum"])
                        if raw_artifact.get("checksum") is not None
                        else None
                    ),
                    filename=(
                        str(raw_artifact["filename"])
                        if raw_artifact.get("filename") is not None
                        else None
                    ),
                )
                raw_descriptors = fixture.get("descriptors", [])
                if not isinstance(raw_descriptors, list):
                    raise ValueError("documents TCK parser descriptors must be a list")
                registry = DocumentParserRegistry()
                for descriptor_index, raw_descriptor in enumerate(raw_descriptors):
                    if not isinstance(raw_descriptor, Mapping):
                        raise ValueError(
                            f"documents TCK parser descriptor {descriptor_index} must be a mapping"
                        )
                    raw_media_types = raw_descriptor.get(
                        "mediaTypes", raw_descriptor.get("media_types", ())
                    )
                    raw_extensions = raw_descriptor.get("extensions", ())
                    raw_metadata = raw_descriptor.get("metadata", {})
                    registry.register(
                        ParserDescriptor(
                            str(
                                raw_descriptor.get(
                                    "processorId",
                                    raw_descriptor.get("processor_id", ""),
                                )
                            ),
                            str(raw_descriptor.get("version", "")),
                            media_types=(
                                tuple(str(item) for item in raw_media_types)
                                if isinstance(raw_media_types, list | tuple)
                                else ()
                            ),
                            extensions=(
                                tuple(str(item) for item in raw_extensions)
                                if isinstance(raw_extensions, list | tuple)
                                else ()
                            ),
                            priority=(
                                raw_descriptor.get("priority")
                                if isinstance(raw_descriptor.get("priority"), int)
                                and not isinstance(raw_descriptor.get("priority"), bool)
                                else 0
                            ),
                            supports_ocr=bool(
                                raw_descriptor.get(
                                    "supportsOcr",
                                    raw_descriptor.get("supports_ocr", False),
                                )
                            ),
                            metadata=(
                                dict(raw_metadata)
                                if isinstance(raw_metadata, Mapping)
                                else {}
                            ),
                        )
                    )
                lock = registry.select(
                    artifact,
                    allow_ocr_fallback=bool(
                        fixture.get(
                            "allowOcrFallback",
                            fixture.get("allow_ocr_fallback", False),
                        )
                    ),
                )
                resolved = registry.resolve_locked(lock)
                observed = {
                    "processorId": lock.processor_id,
                    "processorVersion": lock.processor_version,
                    "reason": lock.reason,
                    "mediaType": lock.media_type,
                    "filename": lock.filename,
                    "artifactChecksum": lock.artifact_checksum,
                    "metadata": dict(lock.metadata),
                    "resolvedMetadata": dict(resolved.metadata),
                }
            elif kind == "parser_locked_parse":
                registry = DocumentParserRegistry()
                registry.register(plain_text_parser_descriptor())
                selected_revision = replace(
                    revision,
                    artifact=replace(
                        revision.artifact,
                        checksum=str(
                            fixture.get(
                                "selectedArtifactChecksum",
                                fixture.get("selected_artifact_checksum", ""),
                            )
                        ),
                    ),
                )
                current_revision = replace(
                    revision,
                    artifact=replace(
                        revision.artifact,
                        checksum=str(
                            fixture.get(
                                "revisionArtifactChecksum",
                                fixture.get("revision_artifact_checksum", ""),
                            )
                        ),
                    ),
                )
                lock = registry.select(selected_revision.artifact)
                try:
                    registry.parse_locked(
                        asset,
                        current_revision,
                        text.encode("utf-8"),
                        lock,
                    )
                    observed = {"parsed": True, "error": None}
                except Exception as error:
                    message = str(error)
                    observed = {
                        "parsed": False,
                        "error": (
                            "artifact_checksum_mismatch"
                            if "artifact checksum" in message
                            else message
                        ),
                    }
            else:
                diagnostics.append(
                    {
                        "code": "DocumentsKindUnknown",
                        "message": f"documents TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "DocumentsExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "DocumentsExpectedMismatch",
                        "message": f"documents observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_deployment_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.deployment_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "DeploymentExpectedInvalid",
                    "message": "deployment TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        observed: dict[str, object] = {}
        try:
            if kind == "deployment_revision_digest":
                raw_left = fixture.get("left", {})
                raw_right = fixture.get("right", {})
                raw_changed = fixture.get("changed", {})
                if (
                    not isinstance(raw_left, Mapping)
                    or not isinstance(raw_right, Mapping)
                    or not isinstance(raw_changed, Mapping)
                ):
                    raise ValueError(
                        "deployment revision digest case requires left, right, and changed mappings"
                    )
                left = DeploymentRevision(
                    revision_id=str(
                        raw_left.get("revisionId", raw_left.get("revision_id", ""))
                    ),
                    release_digest=str(
                        raw_left.get(
                            "releaseDigest", raw_left.get("release_digest", "")
                        )
                    ),
                    deployment_spec_hash=str(
                        raw_left.get(
                            "deploymentSpecHash",
                            raw_left.get("deployment_spec_hash", ""),
                        )
                    ),
                    physical_plan_hash=str(
                        raw_left.get(
                            "physicalPlanHash", raw_left.get("physical_plan_hash", "")
                        )
                    ),
                    resolved_binding_hash=str(
                        raw_left.get(
                            "resolvedBindingHash",
                            raw_left.get("resolved_binding_hash", ""),
                        )
                    ),
                    target_capability_hash=str(
                        raw_left.get(
                            "targetCapabilityHash",
                            raw_left.get("target_capability_hash", ""),
                        )
                    ),
                    created_at=str(
                        raw_left.get("createdAt", raw_left.get("created_at", ""))
                    ),
                )
                right = DeploymentRevision(
                    revision_id=str(
                        raw_right.get("revisionId", raw_right.get("revision_id", ""))
                    ),
                    release_digest=str(
                        raw_right.get(
                            "releaseDigest", raw_right.get("release_digest", "")
                        )
                    ),
                    deployment_spec_hash=str(
                        raw_right.get(
                            "deploymentSpecHash",
                            raw_right.get("deployment_spec_hash", ""),
                        )
                    ),
                    physical_plan_hash=str(
                        raw_right.get(
                            "physicalPlanHash", raw_right.get("physical_plan_hash", "")
                        )
                    ),
                    resolved_binding_hash=str(
                        raw_right.get(
                            "resolvedBindingHash",
                            raw_right.get("resolved_binding_hash", ""),
                        )
                    ),
                    target_capability_hash=str(
                        raw_right.get(
                            "targetCapabilityHash",
                            raw_right.get("target_capability_hash", ""),
                        )
                    ),
                    created_at=str(
                        raw_right.get("createdAt", raw_right.get("created_at", ""))
                    ),
                )
                changed = DeploymentRevision(
                    revision_id=str(
                        raw_changed.get(
                            "revisionId", raw_changed.get("revision_id", "")
                        )
                    ),
                    release_digest=str(
                        raw_changed.get(
                            "releaseDigest", raw_changed.get("release_digest", "")
                        )
                    ),
                    deployment_spec_hash=str(
                        raw_changed.get(
                            "deploymentSpecHash",
                            raw_changed.get("deployment_spec_hash", ""),
                        )
                    ),
                    physical_plan_hash=str(
                        raw_changed.get(
                            "physicalPlanHash",
                            raw_changed.get("physical_plan_hash", ""),
                        )
                    ),
                    resolved_binding_hash=str(
                        raw_changed.get(
                            "resolvedBindingHash",
                            raw_changed.get("resolved_binding_hash", ""),
                        )
                    ),
                    target_capability_hash=str(
                        raw_changed.get(
                            "targetCapabilityHash",
                            raw_changed.get("target_capability_hash", ""),
                        )
                    ),
                    created_at=str(
                        raw_changed.get("createdAt", raw_changed.get("created_at", ""))
                    ),
                )
                observed = {
                    "sameDigest": left.content_digest() == right.content_digest(),
                    "changedDigestDifferent": left.content_digest()
                    != changed.content_digest(),
                }
            elif kind == "release_pins":
                raw_release = fixture.get("release", {})
                if not isinstance(raw_release, Mapping):
                    raise ValueError("deployment release_pins case requires release")
                release = GraphRelease(
                    name=str(raw_release.get("name", "")),
                    version=str(raw_release.get("version", "")),
                )
                bundle_digest = raw_release.get(
                    "bundleDigest", raw_release.get("bundle_digest")
                )
                bundle_media_type = raw_release.get(
                    "bundleMediaType", raw_release.get("bundle_media_type")
                )
                if bundle_digest is not None and bundle_media_type is not None:
                    release = release.with_bundle(
                        str(bundle_digest), str(bundle_media_type)
                    )
                application_hash = raw_release.get(
                    "applicationHash", raw_release.get("application_hash")
                )
                if application_hash is not None:
                    release = release.with_application_hash(str(application_hash))
                raw_graphs = raw_release.get("graphs", {})
                if isinstance(raw_graphs, Mapping):
                    for graph_name, raw_graph in raw_graphs.items():
                        if isinstance(raw_graph, Mapping):
                            release = release.with_graph(
                                str(graph_name),
                                GraphReleaseGraph(
                                    str(
                                        raw_graph.get(
                                            "graphHash", raw_graph.get("graph_hash", "")
                                        )
                                    ),
                                    str(
                                        raw_graph.get(
                                            "normalizedPlanHash",
                                            raw_graph.get("normalized_plan_hash", ""),
                                        )
                                    ),
                                ),
                            )
                raw_images = raw_release.get("images", {})
                if isinstance(raw_images, Mapping):
                    for image_name, image_ref in raw_images.items():
                        release = release.with_image(
                            str(image_name), ImageRef(str(image_ref))
                        )
                raw_locks = raw_release.get("locks", {})
                if isinstance(raw_locks, Mapping):
                    for lock_name, raw_lock in raw_locks.items():
                        if isinstance(raw_lock, Mapping):
                            release = release.with_lock(
                                str(lock_name),
                                ReleaseLockRef(
                                    str(
                                        raw_lock.get(
                                            "ref", raw_lock.get("reference", "")
                                        )
                                    ),
                                    digest=(
                                        str(raw_lock.get("digest"))
                                        if raw_lock.get("digest") is not None
                                        else None
                                    ),
                                    lock_type=(
                                        str(
                                            raw_lock.get(
                                                "lockType", raw_lock.get("lock_type")
                                            )
                                        )
                                        if raw_lock.get(
                                            "lockType", raw_lock.get("lock_type")
                                        )
                                        is not None
                                        else None
                                    ),
                                ),
                            )
                raw_knowledge = raw_release.get("knowledge", {})
                if isinstance(raw_knowledge, Mapping):
                    for index_id, raw_binding in raw_knowledge.items():
                        if isinstance(raw_binding, Mapping):
                            release = release.with_knowledge(
                                KnowledgeBinding(
                                    str(index_id),
                                    str(
                                        raw_binding.get(
                                            "indexRevision",
                                            raw_binding.get("index_revision", ""),
                                        )
                                    ),
                                )
                            )
                raw_prompt_locks = raw_release.get(
                    "promptLocks", raw_release.get("prompt_locks", {})
                )
                if isinstance(raw_prompt_locks, Mapping):
                    for prompt_name, raw_prompt in raw_prompt_locks.items():
                        if isinstance(raw_prompt, Mapping):
                            prompt_kind = str(raw_prompt.get("kind", ""))
                            if prompt_kind == "versioned":
                                prompt_lock = PromptLock.versioned(
                                    str(raw_prompt.get("name", prompt_name)),
                                    str(raw_prompt.get("version", "")),
                                )
                            else:
                                prompt_lock = PromptLock.label(
                                    str(raw_prompt.get("name", prompt_name)),
                                    str(
                                        raw_prompt.get(
                                            "label", raw_prompt.get("lockLabel", "")
                                        )
                                    ),
                                )
                            release = release.with_prompt_lock(
                                str(prompt_name), prompt_lock
                            )
                raw_supply_chain = raw_release.get(
                    "supplyChain", raw_release.get("supply_chain")
                )
                if isinstance(raw_supply_chain, Mapping):
                    release = release.with_supply_chain(
                        SupplyChainLock(
                            sbom_ref=(
                                str(
                                    raw_supply_chain.get(
                                        "sbomRef", raw_supply_chain.get("sbom_ref")
                                    )
                                )
                                if raw_supply_chain.get(
                                    "sbomRef", raw_supply_chain.get("sbom_ref")
                                )
                                is not None
                                else None
                            ),
                            provenance_ref=(
                                str(
                                    raw_supply_chain.get(
                                        "provenanceRef",
                                        raw_supply_chain.get("provenance_ref"),
                                    )
                                )
                                if raw_supply_chain.get(
                                    "provenanceRef",
                                    raw_supply_chain.get("provenance_ref"),
                                )
                                is not None
                                else None
                            ),
                            signature_policy=(
                                str(
                                    raw_supply_chain.get(
                                        "signaturePolicy",
                                        raw_supply_chain.get("signature_policy"),
                                    )
                                )
                                if raw_supply_chain.get(
                                    "signaturePolicy",
                                    raw_supply_chain.get("signature_policy"),
                                )
                                is not None
                                else None
                            ),
                        )
                    )
                try:
                    release.validate_production_pins()
                    observed = {"error": None, "references": []}
                except GraphReleaseMutableReferencesError as error:
                    observed = {
                        "error": "mutable_references",
                        "references": list(error.references),
                    }
            elif kind == "upgrade_policy":
                policy = UpgradePolicy.workload_aware(
                    str(
                        fixture.get("oldRevisionId", fixture.get("old_revision_id", ""))
                    ),
                    str(
                        fixture.get("newRevisionId", fixture.get("new_revision_id", ""))
                    ),
                )
                observed_decisions = []
                raw_decisions = fixture.get("decisions", [])
                if not isinstance(raw_decisions, list):
                    raise ValueError(
                        "deployment upgrade_policy case decisions must be a list"
                    )
                for raw_decision in raw_decisions:
                    if not isinstance(raw_decision, Mapping):
                        raise ValueError(
                            "deployment upgrade_policy decision must be a mapping"
                        )
                    decision = policy.decide(
                        str(raw_decision.get("workload", "")),
                        (
                            str(
                                raw_decision.get(
                                    "affinityRevisionId",
                                    raw_decision.get("affinity_revision_id"),
                                )
                            )
                            if raw_decision.get(
                                "affinityRevisionId",
                                raw_decision.get("affinity_revision_id"),
                            )
                            is not None
                            else None
                        ),
                        bool(
                            raw_decision.get(
                                "checkpointCompatible",
                                raw_decision.get("checkpoint_compatible", False),
                            )
                        ),
                    )
                    observed_decisions.append(
                        {
                            "kind": decision.kind,
                            "revisionId": decision.revision_id,
                            "fromRevisionId": decision.from_revision_id,
                            "toRevisionId": decision.to_revision_id,
                        }
                    )
                observed = {"decisions": observed_decisions}
            elif kind == "rollout_gate":
                raw_steps = fixture.get("canarySteps", fixture.get("canary_steps", []))
                if not isinstance(raw_steps, list):
                    raise ValueError(
                        "deployment rollout_gate case canarySteps must be a list"
                    )
                canary_steps = []
                for raw_step in raw_steps:
                    if not isinstance(raw_step, Mapping):
                        raise ValueError(
                            "deployment rollout_gate canary step must be a mapping"
                        )
                    canary_steps.append(
                        RolloutStep.canary(
                            str(raw_step.get("stepId", raw_step.get("step_id", ""))),
                            traffic_percent=int(
                                raw_step.get(
                                    "trafficPercent", raw_step.get("traffic_percent", 0)
                                )
                            ),
                            minimum_samples=(
                                int(
                                    raw_step.get(
                                        "minimumSamples",
                                        raw_step.get("minimum_samples"),
                                    )
                                )
                                if raw_step.get(
                                    "minimumSamples", raw_step.get("minimum_samples")
                                )
                                is not None
                                else None
                            ),
                            minimum_duration_seconds=(
                                int(
                                    raw_step.get(
                                        "minimumDurationSeconds",
                                        raw_step.get("minimum_duration_seconds"),
                                    )
                                )
                                if raw_step.get(
                                    "minimumDurationSeconds",
                                    raw_step.get("minimum_duration_seconds"),
                                )
                                is not None
                                else None
                            ),
                        )
                    )
                plan = RolloutPlan.canary(
                    str(fixture.get("rolloutId", fixture.get("rollout_id", ""))),
                    str(
                        fixture.get(
                            "stableRevisionId", fixture.get("stable_revision_id", "")
                        )
                    ),
                    str(
                        fixture.get(
                            "candidateRevisionId",
                            fixture.get("candidate_revision_id", ""),
                        )
                    ),
                    canary_steps=tuple(canary_steps),
                )
                observed_decisions = []
                raw_evaluations = fixture.get("evaluations", [])
                if not isinstance(raw_evaluations, list):
                    raise ValueError(
                        "deployment rollout_gate case evaluations must be a list"
                    )
                for raw_evaluation in raw_evaluations:
                    if not isinstance(raw_evaluation, Mapping):
                        raise ValueError(
                            "deployment rollout_gate evaluation must be a mapping"
                        )
                    state = plan.initial_state().advance_for_test(
                        int(
                            raw_evaluation.get(
                                "currentStepIndex",
                                raw_evaluation.get("current_step_index", 0),
                            )
                        )
                    )
                    result = RolloutAnalysisResult(
                        step_id=str(
                            raw_evaluation.get(
                                "stepId", raw_evaluation.get("step_id", "")
                            )
                        ),
                        passed=bool(raw_evaluation.get("passed", False)),
                        sample_count=int(
                            raw_evaluation.get(
                                "sampleCount", raw_evaluation.get("sample_count", 0)
                            )
                        ),
                        duration_seconds=int(
                            raw_evaluation.get(
                                "durationSeconds",
                                raw_evaluation.get("duration_seconds", 0),
                            )
                        ),
                        reason=(
                            str(raw_evaluation.get("reason"))
                            if raw_evaluation.get("reason") is not None
                            else None
                        ),
                        non_reversible_effect_observed=bool(
                            raw_evaluation.get(
                                "nonReversibleEffectObserved",
                                raw_evaluation.get(
                                    "non_reversible_effect_observed", False
                                ),
                            )
                        ),
                    )
                    decision = state.evaluate_gate(result)
                    observed_decisions.append(
                        {
                            "decision": decision.decision,
                            "reason": decision.reason,
                            "nextStepIndex": decision.next_state.current_step_index,
                            "nextStatus": decision.next_state.status,
                            "automaticRollbackAllowed": decision.automatic_rollback_allowed,
                        }
                    )
                observed = {"decisions": observed_decisions}
            elif kind == "slo_condition":
                profile = DeploymentSloProfile(
                    profile_id=str(
                        fixture.get("profileId", fixture.get("profile_id", ""))
                    ),
                    slo_objective_ids=_string_tuple(fixture.get("objectives")),
                )
                raw_evaluations = fixture.get("evaluations", [])
                if not isinstance(raw_evaluations, list):
                    raise ValueError(
                        "deployment slo_condition case evaluations must be a list"
                    )
                conditions = []
                for raw_evaluation in raw_evaluations:
                    if not isinstance(raw_evaluation, Mapping):
                        raise ValueError(
                            "deployment slo_condition evaluation must be a mapping"
                        )
                    raw_reports = raw_evaluation.get("reports", [])
                    if not isinstance(raw_reports, list):
                        raise ValueError(
                            "deployment slo_condition reports must be a list"
                        )
                    reports = []
                    for raw_report in raw_reports:
                        if not isinstance(raw_report, Mapping):
                            raise ValueError(
                                "deployment slo_condition report must be a mapping"
                            )
                        slo_id = str(
                            raw_report.get("sloId", raw_report.get("slo_id", ""))
                        )
                        reports.append(
                            SloReport(
                                slo_id=slo_id,
                                indicator=slo_id,
                                window="deployment",
                                status=str(raw_report.get("status", "")),
                                objective=0.0,
                            )
                        )
                    conditions.append(
                        profile.evaluate_slo_reports(reports).condition_contract()
                    )
                observed = {"conditions": conditions}
            else:
                diagnostics.append(
                    {
                        "code": "DeploymentKindUnknown",
                        "message": f"deployment TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "DeploymentExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "DeploymentExpectedMismatch",
                        "message": f"deployment observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_durable_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.durable_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "DurableExpectedInvalid",
                    "message": "durable TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )
        raw_expected_diagnostics = fixture.get(
            "expectedDiagnostics", fixture.get("expected_diagnostics")
        )
        expected_diagnostics = None
        if raw_expected_diagnostics is not None:
            if (
                isinstance(raw_expected_diagnostics, (str, bytes))
                or isinstance(raw_expected_diagnostics, Mapping)
                or not isinstance(raw_expected_diagnostics, Sequence)
            ):
                diagnostics.append(
                    {
                        "code": "DurableExpectedDiagnosticsInvalid",
                        "message": "durable TCK expectedDiagnostics must be a sequence",
                        "path": "$.expectedDiagnostics",
                    }
                )
            else:
                expected_diagnostic_values = []
                for index, raw_diagnostic in enumerate(raw_expected_diagnostics):
                    if not isinstance(raw_diagnostic, Mapping):
                        diagnostics.append(
                            {
                                "code": "DurableExpectedDiagnosticsInvalid",
                                "message": "durable TCK expected diagnostic must be object",
                                "path": f"$.expectedDiagnostics[{index}]",
                            }
                        )
                    else:
                        expected_diagnostic_values.append(dict(raw_diagnostic))
                expected_diagnostics = tuple(expected_diagnostic_values)
        expected_keys_with_structural_diagnostics: set[str] = set()

        try:
            durable = importlib.import_module("graphblocks.durable")
        except ModuleNotFoundError as error:
            diagnostics.append(
                {
                    "code": "DurablePackageMissing",
                    "message": str(error),
                    "path": "$",
                }
            )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="failed",
                diagnostics=tuple(diagnostics),
                observed={},
            )

        observed: dict[str, object] = {}
        try:
            if kind == "source_replay":
                raw_events = fixture.get("events", [])
                if not isinstance(raw_events, list):
                    raise ValueError("durable source_replay case requires events")
                events = []
                for raw_event in raw_events:
                    if not isinstance(raw_event, Mapping):
                        raise ValueError("durable source event must be a mapping")
                    event_time = raw_event.get(
                        "eventTimeUnixMs", raw_event.get("event_time_unix_ms")
                    )
                    events.append(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_event.get("stream", "")),
                                int(raw_event.get("partition", 0)),
                                int(raw_event.get("offset", 0)),
                            ),
                            deepcopy(raw_event.get("payload")),
                            event_time_unix_ms=int(event_time)
                            if event_time is not None
                            else None,
                        )
                    )
                source = durable.InMemoryDurableSource(
                    str(fixture.get("guarantee", "")), events
                )
                raw_first_poll = fixture.get("firstPoll", fixture.get("first_poll", {}))
                if not isinstance(raw_first_poll, Mapping):
                    raw_first_poll = {}
                first = source.poll(None, demand=int(raw_first_poll.get("demand", 1)))
                raw_commit = fixture.get(
                    "commitCursor", fixture.get("commit_cursor", {})
                )
                if not isinstance(raw_commit, Mapping):
                    raise ValueError("durable source_replay case requires commitCursor")
                source.commit(
                    durable.SourceCursor(
                        str(raw_commit.get("stream", "")),
                        int(raw_commit.get("partition", 0)),
                        int(raw_commit.get("offset", 0)),
                    )
                )
                raw_after_commit = fixture.get(
                    "afterCommitPoll", fixture.get("after_commit_poll", {})
                )
                if not isinstance(raw_after_commit, Mapping):
                    raw_after_commit = {}
                after_commit = source.poll(
                    None, demand=int(raw_after_commit.get("demand", 1))
                )
                raw_replay = fixture.get("replayPoll", fixture.get("replay_poll", {}))
                if not isinstance(raw_replay, Mapping):
                    raise ValueError("durable source_replay case requires replayPoll")
                raw_replay_cursor = raw_replay.get("cursor", {})
                if not isinstance(raw_replay_cursor, Mapping):
                    raise ValueError(
                        "durable source_replay case requires replay cursor"
                    )
                replay = source.poll(
                    durable.SourceCursor(
                        str(raw_replay_cursor.get("stream", "")),
                        int(raw_replay_cursor.get("partition", 0)),
                        int(raw_replay_cursor.get("offset", 0)),
                    ),
                    demand=int(raw_replay.get("demand", 1)),
                )
                high_cursor = first.high_cursor()
                observed = {
                    "firstOffsets": [event.cursor.offset for event in first.events],
                    "firstHighCursor": (
                        {
                            "stream": high_cursor.stream,
                            "partition": high_cursor.partition,
                            "offset": high_cursor.offset,
                        }
                        if high_cursor is not None
                        else None
                    ),
                    "firstWatermarkUnixMs": first.watermark.unix_ms
                    if first.watermark is not None
                    else None,
                    "afterCommitOffsets": [
                        event.cursor.offset for event in after_commit.events
                    ],
                    "replayOffsets": [event.cursor.offset for event in replay.events],
                }
            elif kind == "source_errors":
                raw_events = fixture.get("events", [])
                if not isinstance(raw_events, list):
                    raise ValueError("durable source_errors case requires events")
                events = []
                for raw_event in raw_events:
                    if not isinstance(raw_event, Mapping):
                        raise ValueError("durable source event must be a mapping")
                    event_time = raw_event.get(
                        "eventTimeUnixMs", raw_event.get("event_time_unix_ms")
                    )
                    events.append(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_event.get("stream", "")),
                                int(raw_event.get("partition", 0)),
                                int(raw_event.get("offset", 0)),
                            ),
                            deepcopy(raw_event.get("payload")),
                            event_time_unix_ms=int(event_time)
                            if event_time is not None
                            else None,
                        )
                    )
                source = durable.InMemoryDurableSource(
                    str(fixture.get("guarantee", "")), events
                )
                paused_error = None
                source.pause()
                try:
                    source.poll(None, demand=1)
                except durable.SourcePausedError:
                    paused_error = "source_paused"
                source.resume()
                raw_committed = fixture.get(
                    "committedCursor", fixture.get("committed_cursor", {})
                )
                raw_stale = fixture.get("staleCursor", fixture.get("stale_cursor", {}))
                raw_unknown = fixture.get(
                    "unknownCursor", fixture.get("unknown_cursor", {})
                )
                if (
                    not isinstance(raw_committed, Mapping)
                    or not isinstance(raw_stale, Mapping)
                    or not isinstance(raw_unknown, Mapping)
                ):
                    raise ValueError(
                        "durable source_errors case requires committed, stale, and unknown cursors"
                    )
                source.commit(
                    durable.SourceCursor(
                        str(raw_committed.get("stream", "")),
                        int(raw_committed.get("partition", 0)),
                        int(raw_committed.get("offset", 0)),
                    )
                )
                stale_error = None
                stale_current_offset = None
                stale_attempted_offset = None
                try:
                    source.commit(
                        durable.SourceCursor(
                            str(raw_stale.get("stream", "")),
                            int(raw_stale.get("partition", 0)),
                            int(raw_stale.get("offset", 0)),
                        )
                    )
                except durable.StaleCommitError as error:
                    stale_error = "stale_commit"
                    stale_current_offset = error.current.offset
                    stale_attempted_offset = error.attempted.offset
                unknown_cursor = durable.SourceCursor(
                    str(raw_unknown.get("stream", "")),
                    int(raw_unknown.get("partition", 0)),
                    int(raw_unknown.get("offset", 0)),
                )
                unknown_commit_error = None
                unknown_poll_error = None
                try:
                    source.commit(unknown_cursor)
                except durable.UnknownSourceCursorError:
                    unknown_commit_error = "unknown_source_cursor"
                try:
                    source.poll(unknown_cursor, demand=1)
                except durable.UnknownSourceCursorError:
                    unknown_poll_error = "unknown_source_cursor"
                observed = {
                    "pausedError": paused_error,
                    "staleError": stale_error,
                    "staleCurrentOffset": stale_current_offset,
                    "staleAttemptedOffset": stale_attempted_offset,
                    "unknownCommitError": unknown_commit_error,
                    "unknownPollError": unknown_poll_error,
                }
            elif kind == "source_offset_reuse":
                raw_events = fixture.get("events", [])
                if not isinstance(raw_events, list):
                    raise ValueError("durable source_offset_reuse case requires events")
                events = []
                for raw_event in raw_events:
                    if not isinstance(raw_event, Mapping):
                        raise ValueError("durable source event must be a mapping")
                    event_time = raw_event.get(
                        "eventTimeUnixMs", raw_event.get("event_time_unix_ms")
                    )
                    events.append(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_event.get("stream", "")),
                                int(raw_event.get("partition", 0)),
                                int(raw_event.get("offset", 0)),
                            ),
                            deepcopy(raw_event.get("payload")),
                            event_time_unix_ms=int(event_time)
                            if event_time is not None
                            else None,
                        )
                    )
                source_error = None
                conflict_cursor = None
                source_events = ()
                try:
                    source = durable.InMemoryDurableSource(
                        str(fixture.get("guarantee", "")),
                        events,
                    )
                    batch = source.poll(
                        None,
                        demand=int(
                            fixture.get("pollDemand", fixture.get("poll_demand", 1))
                        ),
                    )
                    source_events = batch.events
                except durable.ConflictingSourceOffsetError as error:
                    source_error = "conflicting_source_offset"
                    conflict_cursor = {
                        "stream": error.cursor.stream,
                        "partition": error.cursor.partition,
                        "offset": error.cursor.offset,
                    }
                observed = {
                    "error": source_error,
                    "conflictCursor": conflict_cursor,
                    "eventCount": len(source_events),
                    "offsets": [event.cursor.offset for event in source_events],
                }
            elif kind == "window_lateness":
                raw_policy = fixture.get("policy", {})
                raw_events = fixture.get("events", [])
                raw_watermarks = fixture.get("watermarks", [])
                raw_late_events = fixture.get(
                    "lateEvents", fixture.get("late_events", [])
                )
                raw_late_event = fixture.get("lateEvent", fixture.get("late_event", {}))
                if (
                    not isinstance(raw_policy, Mapping)
                    or not isinstance(raw_events, list)
                    or not isinstance(raw_watermarks, list)
                    or not isinstance(raw_late_events, list)
                    or not isinstance(raw_late_event, Mapping)
                ):
                    raise ValueError(
                        "durable window_lateness case requires policy, events, watermarks, and lateEvent"
                    )
                policy = durable.WindowPolicy.tumbling_event_time(
                    size_ms=int(raw_policy.get("sizeMs", raw_policy.get("size_ms", 0))),
                    allowed_lateness_ms=int(
                        raw_policy.get(
                            "allowedLatenessMs",
                            raw_policy.get("allowed_lateness_ms", 0),
                        )
                    ),
                    accumulation_mode=str(
                        raw_policy.get(
                            "accumulationMode", raw_policy.get("accumulation_mode", "")
                        )
                    ),
                )
                windows = durable.WindowAccumulator(policy)
                for raw_event in raw_events:
                    if not isinstance(raw_event, Mapping):
                        raise ValueError("durable window event must be a mapping")
                    event_time = raw_event.get(
                        "eventTimeUnixMs", raw_event.get("event_time_unix_ms")
                    )
                    windows.ingest(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_event.get("stream", "")),
                                int(raw_event.get("partition", 0)),
                                int(raw_event.get("offset", 0)),
                            ),
                            deepcopy(raw_event.get("payload")),
                            event_time_unix_ms=int(event_time)
                            if event_time is not None
                            else None,
                        )
                    )
                closed_before = windows.advance_watermark(
                    durable.Watermark.event_time(int(raw_watermarks[0]))
                )
                for raw_event in raw_late_events:
                    if not isinstance(raw_event, Mapping):
                        raise ValueError("durable late window event must be a mapping")
                    event_time = raw_event.get(
                        "eventTimeUnixMs", raw_event.get("event_time_unix_ms")
                    )
                    windows.ingest(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_event.get("stream", "")),
                                int(raw_event.get("partition", 0)),
                                int(raw_event.get("offset", 0)),
                            ),
                            deepcopy(raw_event.get("payload")),
                            event_time_unix_ms=int(event_time)
                            if event_time is not None
                            else None,
                        )
                    )
                closed_after = windows.advance_watermark(
                    durable.Watermark.event_time(int(raw_watermarks[1]))
                )
                late_error = None
                late_watermark_unix_ms = None
                try:
                    late_event_time = raw_late_event.get(
                        "eventTimeUnixMs", raw_late_event.get("event_time_unix_ms")
                    )
                    windows.ingest(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_late_event.get("stream", "")),
                                int(raw_late_event.get("partition", 0)),
                                int(raw_late_event.get("offset", 0)),
                            ),
                            deepcopy(raw_late_event.get("payload")),
                            event_time_unix_ms=int(late_event_time)
                            if late_event_time is not None
                            else None,
                        )
                    )
                except durable.LateEventError as error:
                    late_error = "late_event"
                    late_watermark_unix_ms = error.watermark_unix_ms
                before_pane = closed_before[0] if closed_before else None
                first_pane = closed_after[0] if closed_after else None
                observed = {
                    "closedBefore": len(closed_before),
                    "closedAfter": len(closed_after),
                    "beforePaneStartUnixMs": before_pane.start_unix_ms
                    if before_pane is not None
                    else None,
                    "beforePaneEndUnixMs": before_pane.end_unix_ms
                    if before_pane is not None
                    else None,
                    "beforePaneRevision": before_pane.revision
                    if before_pane is not None
                    else None,
                    "beforePaneIsFinal": before_pane.is_final
                    if before_pane is not None
                    else None,
                    "beforePaneOffsets": [
                        event.cursor.offset for event in before_pane.events
                    ]
                    if before_pane is not None
                    else [],
                    "paneStartUnixMs": first_pane.start_unix_ms
                    if first_pane is not None
                    else None,
                    "paneEndUnixMs": first_pane.end_unix_ms
                    if first_pane is not None
                    else None,
                    "paneRevision": first_pane.revision
                    if first_pane is not None
                    else None,
                    "paneIsFinal": first_pane.is_final
                    if first_pane is not None
                    else None,
                    "paneOffsets": [event.cursor.offset for event in first_pane.events]
                    if first_pane is not None
                    else [],
                    "lateError": late_error,
                    "lateWatermarkUnixMs": late_watermark_unix_ms,
                }
            elif kind == "window_boundary":
                raw_policy = fixture.get("policy", {})
                raw_event = fixture.get("event", {})
                if not isinstance(raw_policy, Mapping) or not isinstance(
                    raw_event, Mapping
                ):
                    raise ValueError(
                        "durable window_boundary case requires policy and event"
                    )
                size_ms = int(raw_policy.get("sizeMs", raw_policy.get("size_ms", 0)))
                allowed_lateness_ms = int(
                    raw_policy.get(
                        "allowedLatenessMs", raw_policy.get("allowed_lateness_ms", 0)
                    )
                )
                event_time = raw_event.get(
                    "eventTimeUnixMs", raw_event.get("event_time_unix_ms")
                )
                if event_time is None:
                    raise ValueError(
                        "durable window_boundary case requires eventTimeUnixMs"
                    )
                event_time_unix_ms = int(event_time)
                policy = durable.WindowPolicy.tumbling_event_time(
                    size_ms=size_ms,
                    allowed_lateness_ms=allowed_lateness_ms,
                    accumulation_mode=str(
                        raw_policy.get(
                            "accumulationMode",
                            raw_policy.get("accumulation_mode", ""),
                        )
                    ),
                )
                windows = durable.WindowAccumulator(policy)
                boundary_error = None
                panes = []
                try:
                    windows.ingest(
                        durable.SourceEvent(
                            durable.SourceCursor(
                                str(raw_event.get("stream", "")),
                                int(raw_event.get("partition", 0)),
                                int(raw_event.get("offset", 0)),
                            ),
                            deepcopy(raw_event.get("payload")),
                            event_time_unix_ms=event_time_unix_ms,
                        )
                    )
                    panes = windows.advance_watermark(
                        durable.Watermark.event_time(
                            int(
                                fixture.get(
                                    "watermarkUnixMs",
                                    fixture.get("watermark_unix_ms", 0),
                                )
                            )
                        )
                    )
                except durable.WindowBoundaryOverflowError as error:
                    boundary_error = "window_boundary_overflow"
                    event_time_unix_ms = error.event_time_unix_ms
                    size_ms = error.size_ms
                    allowed_lateness_ms = error.allowed_lateness_ms
                first_pane = panes[0] if panes else None
                observed = {
                    "error": boundary_error,
                    "eventTimeUnixMs": event_time_unix_ms,
                    "sizeMs": size_ms,
                    "allowedLatenessMs": allowed_lateness_ms,
                    "paneCount": len(panes),
                    "paneStartUnixMs": first_pane.start_unix_ms
                    if first_pane is not None
                    else None,
                    "paneEndUnixMs": first_pane.end_unix_ms
                    if first_pane is not None
                    else None,
                    "paneIsFinal": first_pane.is_final
                    if first_pane is not None
                    else None,
                }
            elif kind == "sink_idempotency":
                raw_request = fixture.get("request", {})
                if not isinstance(raw_request, Mapping):
                    raise ValueError("durable sink_idempotency case requires request")
                sink = durable.InMemoryDurableSink(
                    str(fixture.get("sinkId", fixture.get("sink_id", "")))
                )
                request = durable.SinkCommitRequest(
                    run_id=str(raw_request.get("runId", raw_request.get("run_id", ""))),
                    node_id=str(
                        raw_request.get("nodeId", raw_request.get("node_id", ""))
                    ),
                    node_attempt_id=str(
                        raw_request.get(
                            "nodeAttemptId", raw_request.get("node_attempt_id", "")
                        )
                    ),
                    idempotency_key=str(
                        raw_request.get(
                            "idempotencyKey", raw_request.get("idempotency_key", "")
                        )
                    ),
                    payload=deepcopy(raw_request.get("payload")),
                )
                precondition = raw_request.get(
                    "preconditionDigest", raw_request.get("precondition_digest")
                )
                if precondition is not None:
                    request = request.with_precondition_digest(str(precondition))
                first = sink.commit(request)
                replay = sink.commit(request)
                conflict_error = None
                conflict = durable.SinkCommitRequest(
                    run_id=request.run_id,
                    node_id=request.node_id,
                    node_attempt_id=request.node_attempt_id,
                    idempotency_key=request.idempotency_key,
                    payload=deepcopy(
                        fixture.get("conflictPayload", fixture.get("conflict_payload"))
                    ),
                )
                if request.precondition_digest is not None:
                    conflict = conflict.with_precondition_digest(
                        request.precondition_digest
                    )
                try:
                    sink.commit(conflict)
                except durable.IdempotencyConflictError:
                    conflict_error = "idempotency_conflict"
                observed = {
                    "firstSequence": first.sequence,
                    "replaySequence": replay.sequence,
                    "replayReplayed": replay.replayed,
                    "committedCount": sink.committed_count(),
                    "conflictError": conflict_error,
                }
            elif kind == "checkpoint_replay":
                raw_missing_plan = fixture.get(
                    "missingPlanBarrier", fixture.get("missing_plan_barrier", {})
                )
                raw_barrier = fixture.get("barrier", {})
                raw_checkpoints = fixture.get("checkpoints", [])
                if (
                    not isinstance(raw_missing_plan, Mapping)
                    or not isinstance(raw_barrier, Mapping)
                    or not isinstance(raw_checkpoints, list)
                ):
                    raise ValueError(
                        "durable checkpoint_replay case requires missingPlanBarrier, barrier, and checkpoints"
                    )

                raw_schema = raw_missing_plan.get(
                    "checkpointSchema", raw_missing_plan.get("checkpoint_schema", {})
                )
                if not isinstance(raw_schema, Mapping):
                    raw_schema = {}
                missing_plan = durable.CheckpointBarrier(
                    checkpoint_id=str(
                        raw_missing_plan.get(
                            "checkpointId", raw_missing_plan.get("checkpoint_id", "")
                        )
                    ),
                    run_id=str(
                        raw_missing_plan.get(
                            "runId", raw_missing_plan.get("run_id", "")
                        )
                    ),
                    release_id=str(
                        raw_missing_plan.get(
                            "releaseId", raw_missing_plan.get("release_id", "")
                        )
                    ),
                    deployment_revision_id=str(
                        raw_missing_plan.get(
                            "deploymentRevisionId",
                            raw_missing_plan.get("deployment_revision_id", ""),
                        )
                    ),
                    plan_hash=str(
                        raw_missing_plan.get(
                            "planHash", raw_missing_plan.get("plan_hash", "")
                        )
                    ),
                    checkpoint_schema=durable.SchemaRef(
                        str(
                            raw_schema.get("schemaId", raw_schema.get("schema_id", ""))
                        ),
                        int(
                            raw_schema.get(
                                "schemaVersion", raw_schema.get("schema_version", 0)
                            )
                        ),
                    ),
                    state_revision=int(
                        raw_missing_plan.get(
                            "stateRevision", raw_missing_plan.get("state_revision", 0)
                        )
                    ),
                    schema_versions=dict(
                        raw_missing_plan.get(
                            "schemaVersions",
                            raw_missing_plan.get("schema_versions", {}),
                        )
                    )
                    if isinstance(
                        raw_missing_plan.get(
                            "schemaVersions",
                            raw_missing_plan.get("schema_versions", {}),
                        ),
                        Mapping,
                    )
                    else {},
                )
                missing_plan_error = None
                try:
                    missing_plan.validate()
                except durable.CheckpointBarrierError as error:
                    missing_plan_error = error.reason

                raw_schema = raw_barrier.get(
                    "checkpointSchema", raw_barrier.get("checkpoint_schema", {})
                )
                if not isinstance(raw_schema, Mapping):
                    raw_schema = {}
                raw_source_cursors = raw_barrier.get(
                    "sourceCursors", raw_barrier.get("source_cursors", {})
                )
                source_cursors = {}
                if isinstance(raw_source_cursors, Mapping):
                    for source_id, raw_cursor in raw_source_cursors.items():
                        if isinstance(raw_cursor, Mapping):
                            source_cursors[str(source_id)] = durable.SourceCursor(
                                str(raw_cursor.get("stream", "")),
                                int(raw_cursor.get("partition", 0)),
                                int(raw_cursor.get("offset", 0)),
                            )
                barrier = durable.CheckpointBarrier(
                    checkpoint_id=str(
                        raw_barrier.get(
                            "checkpointId", raw_barrier.get("checkpoint_id", "")
                        )
                    ),
                    run_id=str(raw_barrier.get("runId", raw_barrier.get("run_id", ""))),
                    release_id=str(
                        raw_barrier.get("releaseId", raw_barrier.get("release_id", ""))
                    ),
                    deployment_revision_id=str(
                        raw_barrier.get(
                            "deploymentRevisionId",
                            raw_barrier.get("deployment_revision_id", ""),
                        )
                    ),
                    plan_hash=str(
                        raw_barrier.get("planHash", raw_barrier.get("plan_hash", ""))
                    ),
                    checkpoint_schema=durable.SchemaRef(
                        str(
                            raw_schema.get("schemaId", raw_schema.get("schema_id", ""))
                        ),
                        int(
                            raw_schema.get(
                                "schemaVersion", raw_schema.get("schema_version", 0)
                            )
                        ),
                    ),
                    state_revision=int(
                        raw_barrier.get(
                            "stateRevision", raw_barrier.get("state_revision", 0)
                        )
                    ),
                    completed_nodes=tuple(
                        str(node)
                        for node in raw_barrier.get(
                            "completedNodes", raw_barrier.get("completed_nodes", [])
                        )
                    ),
                    pending_nodes=tuple(
                        str(node)
                        for node in raw_barrier.get(
                            "pendingNodes", raw_barrier.get("pending_nodes", [])
                        )
                    ),
                    source_cursors=source_cursors,
                    operator_state=deepcopy(
                        raw_barrier.get(
                            "operatorState", raw_barrier.get("operator_state", {})
                        )
                    )
                    if isinstance(
                        raw_barrier.get(
                            "operatorState", raw_barrier.get("operator_state", {})
                        ),
                        Mapping,
                    )
                    else {},
                    sink_commit_metadata=deepcopy(
                        raw_barrier.get(
                            "sinkCommitMetadata",
                            raw_barrier.get("sink_commit_metadata", {}),
                        )
                    )
                    if isinstance(
                        raw_barrier.get(
                            "sinkCommitMetadata",
                            raw_barrier.get("sink_commit_metadata", {}),
                        ),
                        Mapping,
                    )
                    else {},
                    schema_versions=dict(
                        raw_barrier.get(
                            "schemaVersions", raw_barrier.get("schema_versions", {})
                        )
                    )
                    if isinstance(
                        raw_barrier.get(
                            "schemaVersions", raw_barrier.get("schema_versions", {})
                        ),
                        Mapping,
                    )
                    else {},
                    created_at_unix_ms=int(
                        raw_barrier.get(
                            "createdAtUnixMs", raw_barrier.get("created_at_unix_ms", 0)
                        )
                    ),
                )
                commit_plan = [
                    f"{source_id}:{cursor.stream}:{cursor.partition}:{cursor.offset}"
                    for source_id, cursor in barrier.validate()
                    .source_commit_plan()
                    .cursors
                ]
                store = durable.InMemoryCheckpointStore()
                for raw_checkpoint in raw_checkpoints:
                    if not isinstance(raw_checkpoint, Mapping):
                        raise ValueError("durable checkpoint must be a mapping")
                    raw_schema = raw_checkpoint.get(
                        "checkpointSchema", raw_checkpoint.get("checkpoint_schema", {})
                    )
                    if not isinstance(raw_schema, Mapping):
                        raw_schema = {}
                    raw_source_cursors = raw_checkpoint.get(
                        "sourceCursors", raw_checkpoint.get("source_cursors", {})
                    )
                    checkpoint_source_cursors = {}
                    if isinstance(raw_source_cursors, Mapping):
                        for source_id, raw_cursor in raw_source_cursors.items():
                            if isinstance(raw_cursor, Mapping):
                                checkpoint_source_cursors[str(source_id)] = (
                                    durable.SourceCursor(
                                        str(raw_cursor.get("stream", "")),
                                        int(raw_cursor.get("partition", 0)),
                                        int(raw_cursor.get("offset", 0)),
                                    )
                                )
                    store.put(
                        durable.CheckpointBarrier(
                            checkpoint_id=str(
                                raw_checkpoint.get(
                                    "checkpointId",
                                    raw_checkpoint.get("checkpoint_id", ""),
                                )
                            ),
                            run_id=str(
                                raw_checkpoint.get(
                                    "runId", raw_checkpoint.get("run_id", "")
                                )
                            ),
                            release_id=str(
                                raw_checkpoint.get(
                                    "releaseId", raw_checkpoint.get("release_id", "")
                                )
                            ),
                            deployment_revision_id=str(
                                raw_checkpoint.get(
                                    "deploymentRevisionId",
                                    raw_checkpoint.get("deployment_revision_id", ""),
                                )
                            ),
                            plan_hash=str(
                                raw_checkpoint.get(
                                    "planHash", raw_checkpoint.get("plan_hash", "")
                                )
                            ),
                            checkpoint_schema=durable.SchemaRef(
                                str(
                                    raw_schema.get(
                                        "schemaId", raw_schema.get("schema_id", "")
                                    )
                                ),
                                int(
                                    raw_schema.get(
                                        "schemaVersion",
                                        raw_schema.get("schema_version", 0),
                                    )
                                ),
                            ),
                            state_revision=int(
                                raw_checkpoint.get(
                                    "stateRevision",
                                    raw_checkpoint.get("state_revision", 0),
                                )
                            ),
                            completed_nodes=tuple(
                                str(node)
                                for node in raw_checkpoint.get(
                                    "completedNodes",
                                    raw_checkpoint.get("completed_nodes", []),
                                )
                            ),
                            pending_nodes=tuple(
                                str(node)
                                for node in raw_checkpoint.get(
                                    "pendingNodes",
                                    raw_checkpoint.get("pending_nodes", []),
                                )
                            ),
                            source_cursors=checkpoint_source_cursors,
                            operator_state=deepcopy(
                                raw_checkpoint.get(
                                    "operatorState",
                                    raw_checkpoint.get("operator_state", {}),
                                )
                            )
                            if isinstance(
                                raw_checkpoint.get(
                                    "operatorState",
                                    raw_checkpoint.get("operator_state", {}),
                                ),
                                Mapping,
                            )
                            else {},
                            sink_commit_metadata=deepcopy(
                                raw_checkpoint.get(
                                    "sinkCommitMetadata",
                                    raw_checkpoint.get("sink_commit_metadata", {}),
                                )
                            )
                            if isinstance(
                                raw_checkpoint.get(
                                    "sinkCommitMetadata",
                                    raw_checkpoint.get("sink_commit_metadata", {}),
                                ),
                                Mapping,
                            )
                            else {},
                            schema_versions=dict(
                                raw_checkpoint.get(
                                    "schemaVersions",
                                    raw_checkpoint.get("schema_versions", {}),
                                )
                            )
                            if isinstance(
                                raw_checkpoint.get(
                                    "schemaVersions",
                                    raw_checkpoint.get("schema_versions", {}),
                                ),
                                Mapping,
                            )
                            else {},
                            created_at_unix_ms=int(
                                raw_checkpoint.get(
                                    "createdAtUnixMs",
                                    raw_checkpoint.get("created_at_unix_ms", 0),
                                )
                            ),
                        )
                    )
                raw_lookup = fixture.get("lookup", {})
                raw_missing_lookup = fixture.get(
                    "missingLookup", fixture.get("missing_lookup", {})
                )
                if not isinstance(raw_lookup, Mapping) or not isinstance(
                    raw_missing_lookup, Mapping
                ):
                    raise ValueError(
                        "durable checkpoint_replay case requires lookup and missingLookup"
                    )
                latest = store.latest_compatible(
                    run_id=str(raw_lookup.get("runId", raw_lookup.get("run_id", ""))),
                    release_id=str(
                        raw_lookup.get("releaseId", raw_lookup.get("release_id", ""))
                    ),
                    deployment_revision_id=str(
                        raw_lookup.get(
                            "deploymentRevisionId",
                            raw_lookup.get("deployment_revision_id", ""),
                        )
                    ),
                    plan_hash=str(
                        raw_lookup.get("planHash", raw_lookup.get("plan_hash", ""))
                    ),
                )
                missing = store.latest_compatible(
                    run_id=str(
                        raw_missing_lookup.get(
                            "runId", raw_missing_lookup.get("run_id", "")
                        )
                    ),
                    release_id=str(
                        raw_missing_lookup.get(
                            "releaseId", raw_missing_lookup.get("release_id", "")
                        )
                    ),
                    deployment_revision_id=str(
                        raw_missing_lookup.get(
                            "deploymentRevisionId",
                            raw_missing_lookup.get("deployment_revision_id", ""),
                        )
                    ),
                    plan_hash=str(
                        raw_missing_lookup.get(
                            "planHash", raw_missing_lookup.get("plan_hash", "")
                        )
                    ),
                )
                observed = {
                    "missingPlanError": missing_plan_error,
                    "commitPlan": commit_plan,
                    "latestCheckpointId": latest.checkpoint_id
                    if latest is not None
                    else None,
                    "latestStateRevision": latest.state_revision
                    if latest is not None
                    else None,
                    "missingCompatible": missing is None,
                }
            elif kind == "tool_terminal_from_tool_result":
                store = durable.InMemoryDurableToolTerminalStore()
                raw_result = fixture.get("toolResult", fixture.get("tool_result", {}))
                raw_record = fixture.get("record", {})
                if not isinstance(raw_result, Mapping) or not isinstance(
                    raw_record, Mapping
                ):
                    raise ValueError(
                        "durable tool_terminal_from_tool_result case requires toolResult and record"
                    )

                status = str(raw_result.get("status", ""))
                tool_call_id = str(
                    raw_result.get("toolCallId", raw_result.get("tool_call_id", ""))
                )
                started_at = str(
                    raw_result.get(
                        "startedAt",
                        raw_result.get("started_at", "2026-06-23T00:00:00Z"),
                    )
                )
                completed_at = str(
                    raw_result.get(
                        "completedAt",
                        raw_result.get("completed_at", "2026-06-23T00:00:00Z"),
                    )
                )
                raw_error = raw_result.get("error", {"code": status, "message": status})
                if not isinstance(raw_error, Mapping):
                    raise ValueError(
                        "durable tool_terminal_from_tool_result error must be a mapping"
                    )
                raw_output = raw_result.get("output", [])
                if not isinstance(raw_output, list):
                    raise ValueError(
                        "durable tool_terminal_from_tool_result output must be a list"
                    )
                output_parts: list[ContentPart] = []
                for part_index, raw_part in enumerate(raw_output):
                    if not isinstance(raw_part, Mapping):
                        raise ValueError(
                            f"durable tool terminal output part {part_index} must be a mapping"
                        )
                    metadata_value = raw_part.get("metadata", {})
                    if not isinstance(metadata_value, Mapping):
                        raise ValueError(
                            f"durable tool terminal output part {part_index} metadata must be a mapping"
                        )
                    part_kind = str(raw_part.get("kind", "text"))
                    if part_kind == "text":
                        text = raw_part.get("text")
                        if not isinstance(text, str):
                            raise ValueError(
                                f"durable tool terminal output part {part_index} text must be a string"
                            )
                        output_parts.append(
                            ContentPart(
                                kind="text", text=text, metadata=dict(metadata_value)
                            )
                        )
                    elif part_kind in {"json", "artifact_ref"}:
                        data = raw_part.get("data")
                        if not isinstance(data, Mapping):
                            raise ValueError(
                                f"durable tool terminal output part {part_index} data must be a mapping"
                            )
                        output_parts.append(
                            ContentPart(
                                kind=part_kind,
                                data=dict(data),
                                metadata=dict(metadata_value),
                            )
                        )
                    else:
                        raise ValueError(
                            f"durable tool terminal output part {part_index} has unsupported kind {part_kind!r}"
                        )

                if status == "completed":
                    tool_result = ToolResult.completed(
                        tool_call_id,
                        tuple(output_parts),
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                elif status == "failed":
                    tool_result = ToolResult.failed(
                        tool_call_id,
                        error=dict(raw_error),
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                elif status == "denied":
                    tool_result = ToolResult.denied(
                        tool_call_id,
                        error=dict(raw_error),
                        completed_at=completed_at,
                    )
                elif status == "cancelled":
                    tool_result = ToolResult.cancelled(
                        tool_call_id,
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                elif status == "policy_stopped":
                    tool_result = ToolResult.policy_stopped(
                        tool_call_id,
                        error=dict(raw_error),
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                elif status == "incomplete":
                    tool_result = ToolResult.incomplete(
                        tool_call_id,
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                else:
                    raise ValueError(
                        f"durable tool_terminal_from_tool_result has unsupported status {status!r}"
                    )

                effect_outcome = raw_result.get(
                    "effectOutcome", raw_result.get("effect_outcome")
                )
                if effect_outcome is not None:
                    tool_result = tool_result.with_effect_outcome(str(effect_outcome))
                idempotency_key = raw_record.get(
                    "idempotencyKey", raw_record.get("idempotency_key")
                )
                record_durable_result_path = (
                    "durableResultCommitted"
                    if "durableResultCommitted" in raw_record
                    or "durable_result_committed" not in raw_record
                    else "durable_result_committed"
                )
                raw_record_durable_result_committed = raw_record.get(
                    "durableResultCommitted",
                    raw_record.get("durable_result_committed", False),
                )
                if not isinstance(raw_record_durable_result_committed, bool):
                    diagnostics.append(
                        {
                            "code": "DurableToolTerminalInvalid",
                            "message": "durable tool terminal record durableResultCommitted must be a boolean",
                            "path": f"$.record.{record_durable_result_path}",
                        }
                    )
                record = durable.DurableToolTerminalRecord.from_tool_result(
                    tool_result,
                    run_id=str(raw_record.get("runId", raw_record.get("run_id", ""))),
                    response_id=str(
                        raw_record.get("responseId", raw_record.get("response_id", ""))
                    ),
                    revision=int(raw_record.get("revision", 0)),
                    arguments_digest=str(
                        raw_record.get(
                            "argumentsDigest", raw_record.get("arguments_digest", "")
                        )
                    ),
                    completed_at_unix_ms=int(
                        raw_record.get(
                            "completedAtUnixMs",
                            raw_record.get("completed_at_unix_ms", 0),
                        )
                    ),
                    idempotency_key=str(idempotency_key)
                    if idempotency_key is not None
                    else None,
                    durable_result_committed=(
                        raw_record_durable_result_committed
                        if isinstance(raw_record_durable_result_committed, bool)
                        else False
                    ),
                )
                committed = store.record_tool_terminal(record)
                observed = {
                    "commitSequence": committed.sequence,
                    "toolCallId": committed.record.tool_call_id,
                    "terminalState": committed.record.terminal_state,
                    "outputDigestMatchesResult": committed.record.output_digest
                    == tool_result.output_digest,
                    "outputDigestPrefix": (
                        committed.record.output_digest[:7]
                        if committed.record.output_digest is not None
                        else None
                    ),
                    "idempotencyKey": committed.record.idempotency_key,
                    "effectCommitted": committed.record.effect_committed,
                    "durableResultCommitted": committed.record.durable_result_committed,
                    "toolTerminalCount": store.tool_terminal_count(),
                }
            elif kind == "tool_terminal_policy_stop":
                store = durable.InMemoryDurableToolTerminalStore()
                raw_stop = fixture.get("policyStop", fixture.get("policy_stop", {}))
                raw_late_result = fixture.get(
                    "lateDurableResult", fixture.get("late_durable_result", {})
                )
                raw_audited = fixture.get(
                    "auditedLateEffect", fixture.get("audited_late_effect", {})
                )
                if (
                    not isinstance(raw_stop, Mapping)
                    or not isinstance(raw_late_result, Mapping)
                    or not isinstance(raw_audited, Mapping)
                ):
                    raise ValueError(
                        "durable tool_terminal_policy_stop case requires policyStop and terminal records"
                    )
                policy_stop = store.record_response_policy_stopped(
                    str(raw_stop.get("responseId", raw_stop.get("response_id", ""))),
                    str(
                        raw_stop.get(
                            "policyDecisionId", raw_stop.get("policy_decision_id", "")
                        )
                    ),
                    last_policy_accepted_sequence=int(
                        raw_stop.get(
                            "lastPolicyAcceptedSequence",
                            raw_stop.get("last_policy_accepted_sequence", 0),
                        )
                    ),
                    occurred_at_unix_ms=int(
                        raw_stop.get(
                            "occurredAtUnixMs", raw_stop.get("occurred_at_unix_ms", 0)
                        )
                    ),
                )
                replay = store.record_response_policy_stopped(
                    str(raw_stop.get("responseId", raw_stop.get("response_id", ""))),
                    str(
                        raw_stop.get(
                            "policyDecisionId", raw_stop.get("policy_decision_id", "")
                        )
                    ),
                    last_policy_accepted_sequence=int(
                        raw_stop.get(
                            "lastPolicyAcceptedSequence",
                            raw_stop.get("last_policy_accepted_sequence", 0),
                        )
                    ),
                    occurred_at_unix_ms=int(
                        raw_stop.get(
                            "occurredAtUnixMs", raw_stop.get("occurred_at_unix_ms", 0)
                        )
                    ),
                )
                late_error = None
                try:
                    late_effect_committed_path = (
                        "effectCommitted"
                        if "effectCommitted" in raw_late_result
                        or "effect_committed" not in raw_late_result
                        else "effect_committed"
                    )
                    raw_late_effect_committed = raw_late_result.get(
                        "effectCommitted",
                        raw_late_result.get("effect_committed", False),
                    )
                    if not isinstance(raw_late_effect_committed, bool):
                        diagnostics.append(
                            {
                                "code": "DurableToolTerminalInvalid",
                                "message": "durable tool terminal lateDurableResult effectCommitted must be a boolean",
                                "path": f"$.lateDurableResult.{late_effect_committed_path}",
                            }
                        )
                    late_durable_result_path = (
                        "durableResultCommitted"
                        if "durableResultCommitted" in raw_late_result
                        or "durable_result_committed" not in raw_late_result
                        else "durable_result_committed"
                    )
                    raw_late_durable_result_committed = raw_late_result.get(
                        "durableResultCommitted",
                        raw_late_result.get("durable_result_committed", False),
                    )
                    if not isinstance(raw_late_durable_result_committed, bool):
                        diagnostics.append(
                            {
                                "code": "DurableToolTerminalInvalid",
                                "message": "durable tool terminal lateDurableResult durableResultCommitted must be a boolean",
                                "path": f"$.lateDurableResult.{late_durable_result_path}",
                            }
                        )
                    store.record_tool_terminal(
                        durable.DurableToolTerminalRecord(
                            run_id=str(
                                raw_late_result.get(
                                    "runId", raw_late_result.get("run_id", "")
                                )
                            ),
                            response_id=str(
                                raw_late_result.get(
                                    "responseId", raw_late_result.get("response_id", "")
                                )
                            ),
                            tool_call_id=str(
                                raw_late_result.get(
                                    "toolCallId",
                                    raw_late_result.get("tool_call_id", ""),
                                )
                            ),
                            revision=int(raw_late_result.get("revision", 0)),
                            terminal_state=str(
                                raw_late_result.get(
                                    "terminalState",
                                    raw_late_result.get("terminal_state", ""),
                                )
                            ),
                            arguments_digest=str(
                                raw_late_result.get(
                                    "argumentsDigest",
                                    raw_late_result.get("arguments_digest", ""),
                                )
                            ),
                            completed_at_unix_ms=int(
                                raw_late_result.get(
                                    "completedAtUnixMs",
                                    raw_late_result.get("completed_at_unix_ms", 0),
                                )
                            ),
                            output_digest=(
                                str(
                                    raw_late_result.get(
                                        "outputDigest",
                                        raw_late_result.get("output_digest"),
                                    )
                                )
                                if raw_late_result.get(
                                    "outputDigest", raw_late_result.get("output_digest")
                                )
                                is not None
                                else None
                            ),
                            effect_committed=(
                                raw_late_effect_committed
                                if isinstance(raw_late_effect_committed, bool)
                                else False
                            ),
                            durable_result_committed=(
                                raw_late_durable_result_committed
                                if isinstance(raw_late_durable_result_committed, bool)
                                else False
                            ),
                        )
                    )
                except durable.ResponsePolicyStoppedError:
                    late_error = "response_policy_stopped"
                audited_effect_committed_path = (
                    "effectCommitted"
                    if "effectCommitted" in raw_audited
                    or "effect_committed" not in raw_audited
                    else "effect_committed"
                )
                raw_audited_effect_committed = raw_audited.get(
                    "effectCommitted",
                    raw_audited.get("effect_committed", False),
                )
                if not isinstance(raw_audited_effect_committed, bool):
                    diagnostics.append(
                        {
                            "code": "DurableToolTerminalInvalid",
                            "message": "durable tool terminal auditedLateEffect effectCommitted must be a boolean",
                            "path": f"$.auditedLateEffect.{audited_effect_committed_path}",
                        }
                    )
                audited_durable_result_path = (
                    "durableResultCommitted"
                    if "durableResultCommitted" in raw_audited
                    or "durable_result_committed" not in raw_audited
                    else "durable_result_committed"
                )
                raw_audited_durable_result_committed = raw_audited.get(
                    "durableResultCommitted",
                    raw_audited.get("durable_result_committed", False),
                )
                if not isinstance(raw_audited_durable_result_committed, bool):
                    diagnostics.append(
                        {
                            "code": "DurableToolTerminalInvalid",
                            "message": "durable tool terminal auditedLateEffect durableResultCommitted must be a boolean",
                            "path": f"$.auditedLateEffect.{audited_durable_result_path}",
                        }
                    )
                audited = store.record_tool_terminal(
                    durable.DurableToolTerminalRecord(
                        run_id=str(
                            raw_audited.get("runId", raw_audited.get("run_id", ""))
                        ),
                        response_id=str(
                            raw_audited.get(
                                "responseId", raw_audited.get("response_id", "")
                            )
                        ),
                        tool_call_id=str(
                            raw_audited.get(
                                "toolCallId", raw_audited.get("tool_call_id", "")
                            )
                        ),
                        revision=int(raw_audited.get("revision", 0)),
                        terminal_state=str(
                            raw_audited.get(
                                "terminalState", raw_audited.get("terminal_state", "")
                            )
                        ),
                        arguments_digest=str(
                            raw_audited.get(
                                "argumentsDigest",
                                raw_audited.get("arguments_digest", ""),
                            )
                        ),
                        completed_at_unix_ms=int(
                            raw_audited.get(
                                "completedAtUnixMs",
                                raw_audited.get("completed_at_unix_ms", 0),
                            )
                        ),
                        output_digest=(
                            str(
                                raw_audited.get(
                                    "outputDigest", raw_audited.get("output_digest")
                                )
                            )
                            if raw_audited.get(
                                "outputDigest", raw_audited.get("output_digest")
                            )
                            is not None
                            else None
                        ),
                        effect_committed=(
                            raw_audited_effect_committed
                            if isinstance(raw_audited_effect_committed, bool)
                            else False
                        ),
                        durable_result_committed=(
                            raw_audited_durable_result_committed
                            if isinstance(raw_audited_durable_result_committed, bool)
                            else False
                        ),
                    )
                )
                observed = {
                    "policyStopSequence": policy_stop.sequence,
                    "policyStopReplaySequence": replay.sequence,
                    "policyStopReplayReplayed": replay.replayed,
                    "lateDurableResultError": late_error,
                    "auditedTerminalState": audited.record.terminal_state,
                    "auditedEffectCommitted": audited.record.effect_committed,
                    "auditedDurableResultCommitted": audited.record.durable_result_committed,
                    "toolTerminalCount": store.tool_terminal_count(),
                }
            elif kind == "tool_terminal_effect_invariant":
                store = durable.InMemoryDurableToolTerminalStore()
                raw_record = fixture.get("record", {})
                if not isinstance(raw_record, Mapping):
                    raise ValueError(
                        "durable tool_terminal_effect_invariant case requires record"
                    )
                record_error = None
                try:
                    record_effect_committed_path = (
                        "effectCommitted"
                        if "effectCommitted" in raw_record
                        or "effect_committed" not in raw_record
                        else "effect_committed"
                    )
                    raw_record_effect_committed = raw_record.get(
                        "effectCommitted",
                        raw_record.get("effect_committed", False),
                    )
                    if not isinstance(raw_record_effect_committed, bool):
                        diagnostics.append(
                            {
                                "code": "DurableToolTerminalInvalid",
                                "message": "durable tool terminal record effectCommitted must be a boolean",
                                "path": f"$.record.{record_effect_committed_path}",
                            }
                        )
                    record_durable_result_path = (
                        "durableResultCommitted"
                        if "durableResultCommitted" in raw_record
                        or "durable_result_committed" not in raw_record
                        else "durable_result_committed"
                    )
                    raw_record_durable_result_committed = raw_record.get(
                        "durableResultCommitted",
                        raw_record.get("durable_result_committed", False),
                    )
                    if not isinstance(raw_record_durable_result_committed, bool):
                        diagnostics.append(
                            {
                                "code": "DurableToolTerminalInvalid",
                                "message": "durable tool terminal record durableResultCommitted must be a boolean",
                                "path": f"$.record.{record_durable_result_path}",
                            }
                        )
                    record = durable.DurableToolTerminalRecord(
                        run_id=str(
                            raw_record.get("runId", raw_record.get("run_id", ""))
                        ),
                        response_id=str(
                            raw_record.get(
                                "responseId", raw_record.get("response_id", "")
                            )
                        ),
                        tool_call_id=str(
                            raw_record.get(
                                "toolCallId", raw_record.get("tool_call_id", "")
                            )
                        ),
                        revision=int(raw_record.get("revision", 0)),
                        terminal_state=str(
                            raw_record.get(
                                "terminalState", raw_record.get("terminal_state", "")
                            )
                        ),
                        arguments_digest=str(
                            raw_record.get(
                                "argumentsDigest",
                                raw_record.get("arguments_digest", ""),
                            )
                        ),
                        completed_at_unix_ms=int(
                            raw_record.get(
                                "completedAtUnixMs",
                                raw_record.get("completed_at_unix_ms", 0),
                            )
                        ),
                        output_digest=(
                            str(
                                raw_record.get(
                                    "outputDigest", raw_record.get("output_digest")
                                )
                            )
                            if raw_record.get(
                                "outputDigest", raw_record.get("output_digest")
                            )
                            is not None
                            else None
                        ),
                        effect_committed=(
                            raw_record_effect_committed
                            if isinstance(raw_record_effect_committed, bool)
                            else False
                        ),
                        durable_result_committed=(
                            raw_record_durable_result_committed
                            if isinstance(raw_record_durable_result_committed, bool)
                            else False
                        ),
                    )
                    store.record_tool_terminal(record)
                except durable.ToolTerminalStoreError as error:
                    message = str(error)
                    if (
                        "denied terminal records cannot have committed effects"
                        in message
                    ):
                        record_error = "denied_effect_committed"
                    elif (
                        "expired terminal records cannot have committed effects"
                        in message
                    ):
                        record_error = "expired_effect_committed"
                    else:
                        record_error = type(error).__name__
                observed = {
                    "recordError": record_error,
                    "toolTerminalCount": store.tool_terminal_count(),
                }
            elif kind == "background_run_event_stream":
                raw_events = fixture.get("events", [])
                raw_attach = fixture.get("attach", {})
                raw_detach = fixture.get("detach", {})
                raw_retention = fixture.get("retention", {})
                if not isinstance(raw_events, list) or not isinstance(
                    raw_attach, Mapping
                ):
                    raise ValueError(
                        "durable background_run_event_stream case requires events and attach"
                    )
                if not isinstance(raw_detach, Mapping):
                    raw_detach = {}
                if not isinstance(raw_retention, Mapping):
                    raw_retention = {}
                raw_lifetime = fixture.get("lifetime")
                if not isinstance(raw_lifetime, str) or raw_lifetime not in {
                    "background",
                    "job",
                }:
                    lifetime = ""
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run lifetime must be background or job",
                            "path": "$.lifetime",
                        }
                    )
                else:
                    lifetime = raw_lifetime
                response_mode_path = (
                    "responseMode"
                    if "responseMode" in fixture or "response_mode" not in fixture
                    else "response_mode"
                )
                raw_response_mode = fixture.get(
                    "responseMode", fixture.get("response_mode")
                )
                if not isinstance(raw_response_mode, str) or raw_response_mode not in {
                    "accepted",
                    "background",
                }:
                    response_mode = ""
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run responseMode must be accepted or background",
                            "path": f"$.{response_mode_path}",
                        }
                    )
                else:
                    response_mode = raw_response_mode
                raw_initial_response = fixture.get(
                    "initialResponse", fixture.get("initial_response", {})
                )
                accepted_response_has_run_id = False
                initial_response_run_id = None
                if response_mode in {"accepted", "background"}:
                    if not isinstance(raw_initial_response, Mapping):
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": f"background run {response_mode} response requires object initialResponse",
                                "path": "$.initialResponse",
                            }
                        )
                    else:
                        initial_status = raw_initial_response.get("status")
                        valid_initial_status = (
                            initial_status.strip()
                            if isinstance(initial_status, str)
                            and initial_status.strip()
                            else None
                        )
                        if valid_initial_status != response_mode:
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run {response_mode} response status must match responseMode",
                                    "path": "$.initialResponse.status",
                                }
                            )
                        initial_run_id = raw_initial_response.get(
                            "runId", raw_initial_response.get("run_id")
                        )
                        valid_initial_run_id = (
                            initial_run_id.strip()
                            if isinstance(initial_run_id, str)
                            and initial_run_id.strip()
                            else None
                        )
                        if valid_initial_run_id is None:
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run {response_mode} response requires runId",
                                    "path": "$.initialResponse.runId",
                                }
                            )
                        else:
                            accepted_response_has_run_id = True
                            initial_response_run_id = valid_initial_run_id
                        initial_event_stream = raw_initial_response.get(
                            "eventStream",
                            raw_initial_response.get("event_stream"),
                        )
                        valid_initial_event_stream = (
                            initial_event_stream.strip()
                            if isinstance(initial_event_stream, str)
                            and initial_event_stream.strip()
                            else None
                        )
                        event_stream_path = (
                            "eventStream"
                            if "eventStream" in raw_initial_response
                            or "event_stream" not in raw_initial_response
                            else "event_stream"
                        )
                        if valid_initial_event_stream is None:
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run {response_mode} response requires eventStream",
                                    "path": f"$.initialResponse.{event_stream_path}",
                                }
                            )
                        else:
                            if (
                                valid_initial_run_id is not None
                                and f"/runs/{valid_initial_run_id}/"
                                not in valid_initial_event_stream
                            ):
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run eventStream must include runId",
                                        "path": f"$.initialResponse.{event_stream_path}",
                                    }
                                )
                            if not valid_initial_event_stream.endswith("/events"):
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run eventStream must end with /events",
                                        "path": f"$.initialResponse.{event_stream_path}",
                                    }
                                )
                        initial_websocket = raw_initial_response.get(
                            "websocket",
                            raw_initial_response.get("web_socket"),
                        )
                        valid_initial_websocket = (
                            initial_websocket.strip()
                            if isinstance(initial_websocket, str)
                            and initial_websocket.strip()
                            else None
                        )
                        websocket_path = (
                            "websocket"
                            if "websocket" in raw_initial_response
                            or "web_socket" not in raw_initial_response
                            else "web_socket"
                        )
                        if valid_initial_websocket is None:
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run {response_mode} response requires websocket",
                                    "path": f"$.initialResponse.{websocket_path}",
                                }
                            )
                        else:
                            if (
                                valid_initial_run_id is not None
                                and f"/runs/{valid_initial_run_id}/"
                                not in valid_initial_websocket
                            ):
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run websocket must include runId",
                                        "path": f"$.initialResponse.{websocket_path}",
                                    }
                                )
                            if not valid_initial_websocket.endswith("/ws"):
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run websocket must end with /ws",
                                        "path": f"$.initialResponse.{websocket_path}",
                                    }
                                )
                        initial_cancel = raw_initial_response.get(
                            "cancel",
                            raw_initial_response.get("cancel_route"),
                        )
                        valid_initial_cancel = (
                            initial_cancel.strip()
                            if isinstance(initial_cancel, str)
                            and initial_cancel.strip()
                            else None
                        )
                        cancel_path = (
                            "cancel"
                            if "cancel" in raw_initial_response
                            or "cancel_route" not in raw_initial_response
                            else "cancel_route"
                        )
                        if valid_initial_cancel is None:
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run {response_mode} response requires cancel",
                                    "path": f"$.initialResponse.{cancel_path}",
                                }
                            )
                        else:
                            if (
                                valid_initial_run_id is not None
                                and f"/runs/{valid_initial_run_id}/"
                                not in valid_initial_cancel
                            ):
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run cancel must include runId",
                                        "path": f"$.initialResponse.{cancel_path}",
                                    }
                                )
                            if not valid_initial_cancel.endswith("/cancel"):
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run cancel must end with /cancel",
                                        "path": f"$.initialResponse.{cancel_path}",
                                    }
                                )
                        initial_cursor_value = raw_initial_response.get(
                            "initialCursor",
                            raw_initial_response.get("initial_cursor"),
                        )
                        if (
                            not isinstance(initial_cursor_value, str)
                            or not initial_cursor_value.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run {response_mode} response requires initialCursor",
                                    "path": "$.initialResponse.initialCursor",
                                }
                            )
                initial_cursor = None
                if isinstance(raw_initial_response, Mapping):
                    raw_initial_cursor = raw_initial_response.get(
                        "initialCursor",
                        raw_initial_response.get("initial_cursor"),
                    )
                    if (
                        isinstance(raw_initial_cursor, str)
                        and raw_initial_cursor.strip()
                    ):
                        initial_cursor = raw_initial_cursor
                event_records = []
                previous_event_sequence = None
                event_ids = set()
                event_cursors = set()
                for event_index, raw_event in enumerate(raw_events):
                    if not isinstance(raw_event, Mapping):
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event must be object",
                                "path": f"$.events[{event_index}]",
                            }
                        )
                        continue
                    event_valid = True
                    event_id = raw_event.get("eventId", raw_event.get("event_id"))
                    event_id_path = (
                        "eventId"
                        if "eventId" in raw_event or "event_id" not in raw_event
                        else "event_id"
                    )
                    if not isinstance(event_id, str) or not event_id.strip():
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires eventId",
                                "path": f"$.events[{event_index}].{event_id_path}",
                            }
                        )
                    elif event_id.strip() in event_ids:
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run eventId must be unique",
                                "path": f"$.events[{event_index}].{event_id_path}",
                            }
                        )
                    else:
                        event_ids.add(event_id.strip())
                    event_run_id = raw_event.get("runId", raw_event.get("run_id"))
                    event_run_id_path = (
                        "runId"
                        if "runId" in raw_event or "run_id" not in raw_event
                        else "run_id"
                    )
                    valid_event_run_id = (
                        event_run_id.strip()
                        if isinstance(event_run_id, str) and event_run_id.strip()
                        else None
                    )
                    if valid_event_run_id is None:
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires runId",
                                "path": f"$.events[{event_index}].{event_run_id_path}",
                            }
                        )
                    elif (
                        initial_response_run_id is not None
                        and valid_event_run_id != initial_response_run_id
                    ):
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event runId must match initial response runId",
                                "path": f"$.events[{event_index}].{event_run_id_path}",
                            }
                        )
                    event_release_id = raw_event.get(
                        "releaseId", raw_event.get("release_id")
                    )
                    event_release_id_path = (
                        "releaseId"
                        if "releaseId" in raw_event or "release_id" not in raw_event
                        else "release_id"
                    )
                    if (
                        not isinstance(event_release_id, str)
                        or not event_release_id.strip()
                    ):
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires releaseId",
                                "path": f"$.events[{event_index}].{event_release_id_path}",
                            }
                        )
                    if "payload" not in raw_event:
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires payload",
                                "path": f"$.events[{event_index}].payload",
                            }
                        )
                    visibility = raw_event.get("visibility")
                    if visibility is not None and visibility not in {
                        "client",
                        "operator",
                        "internal",
                        "audit_only",
                    }:
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event visibility must be client, operator, internal, or audit_only",
                                "path": f"$.events[{event_index}].visibility",
                            }
                        )
                    for metadata_field, metadata_snake_field, metadata_label in (
                        ("graphId", "graph_id", "graphId"),
                        ("nodeId", "node_id", "nodeId"),
                        ("turnId", "turn_id", "turnId"),
                        ("operationId", "operation_id", "operationId"),
                    ):
                        metadata_path = (
                            metadata_field
                            if metadata_field in raw_event
                            or metadata_snake_field not in raw_event
                            else metadata_snake_field
                        )
                        metadata_value = raw_event.get(
                            metadata_field, raw_event.get(metadata_snake_field)
                        )
                        if metadata_value is not None and (
                            not isinstance(metadata_value, str)
                            or not metadata_value.strip()
                        ):
                            event_valid = False
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": f"background run event {metadata_label} must be nonblank string",
                                    "path": f"$.events[{event_index}].{metadata_path}",
                                }
                            )
                    cursor = raw_event.get("cursor")
                    if not isinstance(cursor, str) or not cursor.strip():
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires cursor",
                                "path": f"$.events[{event_index}].cursor",
                            }
                        )
                    elif (
                        initial_cursor is not None
                        and cursor.strip() == initial_cursor.strip()
                    ):
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event cursor must not equal initialCursor",
                                "path": f"$.events[{event_index}].cursor",
                            }
                        )
                    elif cursor.strip() in event_cursors:
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run cursor must be unique",
                                "path": f"$.events[{event_index}].cursor",
                            }
                        )
                    else:
                        event_cursors.add(cursor.strip())
                    event_type = raw_event.get("type")
                    if not isinstance(event_type, str) or not event_type.strip():
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires type",
                                "path": f"$.events[{event_index}].type",
                            }
                        )
                    occurred_at_path = (
                        "occurredAt"
                        if "occurredAt" in raw_event or "occurred_at" not in raw_event
                        else "occurred_at"
                    )
                    occurred_at = raw_event.get(
                        "occurredAt",
                        raw_event.get("occurred_at"),
                    )
                    if not isinstance(occurred_at, str) or not occurred_at.strip():
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires ISO occurredAt",
                                "path": f"$.events[{event_index}].{occurred_at_path}",
                            }
                        )
                    else:
                        occurred_at_text = occurred_at.strip()
                        if len(occurred_at_text) <= 10 or occurred_at_text[10] != "T":
                            event_valid = False
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": "background run event requires ISO occurredAt",
                                    "path": f"$.events[{event_index}].{occurred_at_path}",
                                }
                            )
                        else:
                            suffix = occurred_at_text[19:]
                            suffix_valid = False
                            if suffix.startswith("."):
                                offset_start = min(
                                    (
                                        position
                                        for position in (
                                            suffix.find("Z"),
                                            suffix.find("+"),
                                            suffix.find("-"),
                                        )
                                        if position >= 0
                                    ),
                                    default=-1,
                                )
                                if (
                                    offset_start > 1
                                    and suffix[1:offset_start].isdigit()
                                ):
                                    suffix = suffix[offset_start:]
                            if suffix == "Z":
                                suffix_valid = True
                            elif (
                                len(suffix) == 6
                                and suffix[0] in "+-"
                                and suffix[1:3].isdigit()
                                and suffix[3] == ":"
                                and suffix[4:6].isdigit()
                                and 0 <= int(suffix[1:3]) <= 23
                                and 0 <= int(suffix[4:6]) <= 59
                            ):
                                suffix_valid = True
                            if not suffix_valid:
                                event_valid = False
                                diagnostics.append(
                                    {
                                        "code": "DurableBackgroundRunInvalid",
                                        "message": "background run event requires ISO occurredAt",
                                        "path": f"$.events[{event_index}].{occurred_at_path}",
                                    }
                                )
                            else:
                                try:
                                    datetime.fromisoformat(
                                        occurred_at_text.replace("Z", "+00:00")
                                        if occurred_at_text.endswith("Z")
                                        else occurred_at_text
                                    )
                                except ValueError:
                                    event_valid = False
                                    diagnostics.append(
                                        {
                                            "code": "DurableBackgroundRunInvalid",
                                            "message": "background run event requires ISO occurredAt",
                                            "path": f"$.events[{event_index}].{occurred_at_path}",
                                        }
                                    )
                    sequence = raw_event.get("sequence")
                    event_sequence = None
                    if (
                        isinstance(sequence, bool)
                        or not isinstance(sequence, int)
                        or sequence < 0
                    ):
                        event_valid = False
                        diagnostics.append(
                            {
                                "code": "DurableBackgroundRunInvalid",
                                "message": "background run event requires integer sequence",
                                "path": f"$.events[{event_index}].sequence",
                            }
                        )
                    else:
                        event_sequence = sequence
                        if event_sequence == 0:
                            event_valid = False
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": "background run event requires positive integer sequence",
                                    "path": f"$.events[{event_index}].sequence",
                                }
                            )
                        elif (
                            previous_event_sequence is not None
                            and event_sequence <= previous_event_sequence
                        ):
                            event_valid = False
                            diagnostics.append(
                                {
                                    "code": "DurableBackgroundRunInvalid",
                                    "message": "background run event sequence must be strictly increasing",
                                    "path": f"$.events[{event_index}].sequence",
                                }
                            )
                    if event_valid:
                        previous_event_sequence = event_sequence
                        event_records.append(raw_event)
                cursor_positions = {}
                if initial_cursor is not None:
                    cursor_positions[initial_cursor] = -1
                for event_index, event in enumerate(event_records):
                    event_cursor = event.get("cursor")
                    if (
                        isinstance(event_cursor, str)
                        and event_cursor not in cursor_positions
                    ):
                        cursor_positions[event_cursor] = event_index
                has_last_cursor = (
                    "lastCursor" in raw_attach or "last_cursor" in raw_attach
                )
                raw_last_cursor = raw_attach.get(
                    "lastCursor", raw_attach.get("last_cursor")
                )
                if has_last_cursor and (
                    not isinstance(raw_last_cursor, str) or not raw_last_cursor.strip()
                ):
                    last_cursor_path = (
                        "lastCursor"
                        if "lastCursor" in raw_attach or "last_cursor" not in raw_attach
                        else "last_cursor"
                    )
                    last_cursor = None
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run attach requires string lastCursor",
                            "path": f"$.attach.{last_cursor_path}",
                        }
                    )
                else:
                    last_cursor = raw_last_cursor
                if last_cursor is None:
                    last_cursor_index = None
                else:
                    last_cursor_index = cursor_positions.get(last_cursor)
                replay_after_cursor = [
                    str(event.get("eventId", event.get("event_id", "")))
                    for event_index, event in enumerate(event_records)
                    if last_cursor is None
                    or (
                        last_cursor_index is not None
                        and event_index > last_cursor_index
                    )
                ]
                has_expired_cursor = (
                    "expiredCursor" in raw_attach or "expired_cursor" in raw_attach
                )
                raw_expired_cursor = raw_attach.get(
                    "expiredCursor", raw_attach.get("expired_cursor", "")
                )
                if has_expired_cursor and (
                    not isinstance(raw_expired_cursor, str)
                    or not raw_expired_cursor.strip()
                ):
                    expired_cursor_path = (
                        "expiredCursor"
                        if "expiredCursor" in raw_attach
                        or "expired_cursor" not in raw_attach
                        else "expired_cursor"
                    )
                    expired_cursor = ""
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run attach requires string expiredCursor",
                            "path": f"$.attach.{expired_cursor_path}",
                        }
                    )
                else:
                    expired_cursor = raw_expired_cursor
                has_retained_from = (
                    "retainedFromCursor" in raw_retention
                    or "retained_from_cursor" in raw_retention
                )
                raw_retained_from = raw_retention.get(
                    "retainedFromCursor",
                    raw_retention.get("retained_from_cursor", ""),
                )
                if has_retained_from and (
                    not isinstance(raw_retained_from, str)
                    or not raw_retained_from.strip()
                ):
                    retained_from_path = (
                        "retainedFromCursor"
                        if "retainedFromCursor" in raw_retention
                        or "retained_from_cursor" not in raw_retention
                        else "retained_from_cursor"
                    )
                    retained_from = ""
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run retention requires string retainedFromCursor",
                            "path": f"$.retention.{retained_from_path}",
                        }
                    )
                else:
                    retained_from = raw_retained_from
                expired_cursor_index = cursor_positions.get(expired_cursor)
                retained_from_index = cursor_positions.get(retained_from)
                raw_cancel_run = raw_detach.get(
                    "cancelRun", raw_detach.get("cancel_run", False)
                )
                if isinstance(raw_cancel_run, bool):
                    cancel_run = raw_cancel_run
                else:
                    cancel_run = False
                    cancel_run_path = (
                        "cancelRun"
                        if "cancelRun" in raw_detach or "cancel_run" not in raw_detach
                        else "cancel_run"
                    )
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run detach requires boolean cancelRun",
                            "path": f"$.detach.{cancel_run_path}",
                        }
                    )
                raw_summary_included = raw_attach.get(
                    "summaryOnExpiredCursor",
                    raw_attach.get("summary_on_expired_cursor", False),
                )
                if isinstance(raw_summary_included, bool):
                    summary_included = raw_summary_included
                else:
                    summary_included = False
                    summary_path = (
                        "summaryOnExpiredCursor"
                        if "summaryOnExpiredCursor" in raw_attach
                        or "summary_on_expired_cursor" not in raw_attach
                        else "summary_on_expired_cursor"
                    )
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run attach requires boolean summaryOnExpiredCursor",
                            "path": f"$.attach.{summary_path}",
                        }
                    )
                source_of_truth_path = (
                    "sourceOfTruth"
                    if "sourceOfTruth" in fixture or "source_of_truth" not in fixture
                    else "source_of_truth"
                )
                source_of_truth = fixture.get(
                    "sourceOfTruth", fixture.get("source_of_truth")
                )
                authoritative_stream = (
                    isinstance(source_of_truth, str)
                    and source_of_truth == "ApplicationEventStream"
                )
                if not authoritative_stream:
                    diagnostics.append(
                        {
                            "code": "DurableBackgroundRunInvalid",
                            "message": "background run sourceOfTruth must be ApplicationEventStream",
                            "path": f"$.{source_of_truth_path}",
                        }
                    )
                observed = {
                    "runContinuesAfterDetach": lifetime in {"background", "job"}
                    and not cancel_run,
                    "acceptedResponseReturnsRunId": accepted_response_has_run_id,
                    "replayEventIds": replay_after_cursor,
                    "cursorExpired": expired_cursor_index is not None
                    and retained_from_index is not None
                    and expired_cursor_index < retained_from_index,
                    "summaryIncluded": summary_included,
                    "authoritativeStream": authoritative_stream,
                    "diagnosticCount": len(diagnostics),
                }
            elif kind == "callback_delivery_projection":
                raw_deliveries = fixture.get("deliveries", [])
                raw_redrive = fixture.get("redrive", {})
                raw_subscription = fixture.get("subscription", {})
                subscription_supplied = "subscription" in fixture and isinstance(
                    raw_subscription, Mapping
                )
                if not isinstance(raw_deliveries, list):
                    raise ValueError(
                        "durable callback_delivery_projection case requires deliveries"
                    )
                if not raw_deliveries:
                    diagnostics.append(
                        {
                            "code": "DurableCallbackDeliveryInvalid",
                            "message": "callback delivery requires at least one delivery",
                            "path": "$.deliveries",
                        }
                    )
                if "subscription" in fixture and not isinstance(
                    raw_subscription, Mapping
                ):
                    diagnostics.append(
                        {
                            "code": "DurableCallbackProjectionInvalid",
                            "message": "callback projection subscription must be object",
                            "path": "$.subscription",
                        }
                    )
                    raw_subscription = {}
                elif not isinstance(raw_subscription, Mapping):
                    raw_subscription = {}
                subscription_identity = None
                subscription_failure_policy = None
                if subscription_supplied:
                    subscription_id = raw_subscription.get(
                        "subscriptionId", raw_subscription.get("subscription_id")
                    )
                    if (
                        not isinstance(subscription_id, str)
                        or not subscription_id.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackProjectionInvalid",
                                "message": "callback subscription requires subscriptionId",
                                "path": "$.subscription.subscriptionId",
                            }
                        )
                    else:
                        subscription_identity = subscription_id.strip()
                    failure_policy = raw_subscription.get(
                        "failurePolicy", raw_subscription.get("failure_policy")
                    )
                    if failure_policy in {
                        "best_effort",
                        "retry_then_dead_letter",
                        "pause_run_on_failure",
                        "fail_run_on_failure",
                    }:
                        subscription_failure_policy = failure_policy
                    elif failure_policy is not None:
                        diagnostics.append(
                            {
                                "code": "DurableCallbackProjectionInvalid",
                                "message": "callback subscription has invalid failurePolicy",
                                "path": "$.subscription.failurePolicy",
                            }
                        )
                    mandatory = raw_subscription.get("mandatory")
                    if mandatory is True and (
                        failure_policy is None or failure_policy == "best_effort"
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackProjectionInvalid",
                                "message": "mandatory callback subscription requires retry, dead-letter, or fallback failurePolicy",
                                "path": "$.subscription.failurePolicy",
                            }
                        )
                    if not isinstance(mandatory, bool):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackProjectionInvalid",
                                "message": "callback subscription requires boolean mandatory",
                                "path": "$.subscription.mandatory",
                            }
                        )
                if "redrive" in fixture and not isinstance(raw_redrive, Mapping):
                    diagnostics.append(
                        {
                            "code": "DurableCallbackRedriveInvalid",
                            "message": "callback redrive must be object",
                            "path": "$.redrive",
                        }
                    )
                    raw_redrive = {}
                elif not isinstance(raw_redrive, Mapping):
                    raw_redrive = {}
                raw_redrive_assertions = fixture.get(
                    "redriveAssertions", fixture.get("redrive_assertions", {})
                )
                if not isinstance(raw_redrive_assertions, Mapping):
                    if (
                        "redriveAssertions" in fixture
                        or "redrive_assertions" in fixture
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackRedriveInvalid",
                                "message": "callback redrive assertions must be object",
                                "path": "$.redriveAssertions",
                            }
                        )
                    raw_redrive_assertions = {}
                for key, alias in (
                    ("deadLetterPreservesEventId", "dead_letter_preserves_event_id"),
                    (
                        "redriveCreatesApplicationEvent",
                        "redrive_creates_application_event",
                    ),
                ):
                    if key in raw_redrive_assertions or alias in raw_redrive_assertions:
                        value = raw_redrive_assertions.get(
                            key, raw_redrive_assertions.get(alias)
                        )
                        path = (
                            key
                            if key in raw_redrive_assertions
                            or alias not in raw_redrive_assertions
                            else alias
                        )
                        if not isinstance(value, bool):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackRedriveInvalid",
                                    "message": f"callback redrive assertion requires boolean {key}",
                                    "path": f"$.redriveAssertions.{path}",
                                }
                            )
                redrive_creates_application_event = False
                redrive_event_id_preserved = False
                raw_non_mandatory_outage_blocks_run = fixture.get(
                    "nonMandatoryOutageBlocksRun",
                    fixture.get("non_mandatory_outage_blocks_run"),
                )
                if isinstance(raw_non_mandatory_outage_blocks_run, bool):
                    non_mandatory_outage_blocks_run = (
                        raw_non_mandatory_outage_blocks_run
                    )
                else:
                    non_mandatory_outage_blocks_run = True
                    diagnostics.append(
                        {
                            "code": "DurableCallbackProjectionInvalid",
                            "message": "callback projection requires boolean nonMandatoryOutageBlocksRun",
                            "path": "$.nonMandatoryOutageBlocksRun",
                        }
                    )
                if raw_redrive:
                    for key, alias in (
                        ("deliveryId", "delivery_id"),
                        ("eventId", "event_id"),
                        ("originalEventId", "original_event_id"),
                        ("operatorPrincipal", "operator_principal"),
                        ("reason", "redrive_reason"),
                    ):
                        value = raw_redrive.get(key, raw_redrive.get(alias))
                        if not isinstance(value, str) or not value.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackRedriveInvalid",
                                    "message": f"callback redrive requires {key}",
                                    "path": f"$.redrive.{key}",
                                }
                            )
                    redrive_event_id = raw_redrive.get(
                        "eventId", raw_redrive.get("event_id")
                    )
                    original_event_id = raw_redrive.get(
                        "originalEventId", raw_redrive.get("original_event_id")
                    )
                    if (
                        isinstance(redrive_event_id, str)
                        and redrive_event_id.strip()
                        and isinstance(original_event_id, str)
                        and original_event_id.strip()
                    ):
                        redrive_event_id_preserved = (
                            redrive_event_id == original_event_id
                        )
                        if not redrive_event_id_preserved:
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackRedriveInvalid",
                                    "message": "callback redrive must preserve originalEventId",
                                    "path": "$.redrive.eventId",
                                }
                            )
                    raw_creates_application_event = raw_redrive.get(
                        "createsApplicationEvent",
                        raw_redrive.get("creates_application_event"),
                    )
                    if raw_creates_application_event is None:
                        diagnostics.append(
                            {
                                "code": "DurableCallbackRedriveInvalid",
                                "message": "callback redrive requires boolean createsApplicationEvent",
                                "path": "$.redrive.createsApplicationEvent",
                            }
                        )
                        redrive_creates_application_event = False
                    elif isinstance(raw_creates_application_event, bool):
                        redrive_creates_application_event = (
                            raw_creates_application_event
                        )
                    else:
                        diagnostics.append(
                            {
                                "code": "DurableCallbackRedriveInvalid",
                                "message": "callback redrive requires boolean createsApplicationEvent",
                                "path": "$.redrive.createsApplicationEvent",
                            }
                        )
                else:
                    if (
                        expected.get("deadLetterPreservesEventId") is True
                        or raw_redrive_assertions.get("deadLetterPreservesEventId")
                        is True
                        or raw_redrive_assertions.get("dead_letter_preserves_event_id")
                        is True
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackRedriveInvalid",
                                "message": "callback redrive evidence required for deadLetterPreservesEventId",
                                "path": "$.redrive",
                            }
                        )
                        expected_keys_with_structural_diagnostics.add(
                            "deadLetterPreservesEventId"
                        )
                    if (
                        expected.get("redriveCreatesApplicationEvent") is True
                        or raw_redrive_assertions.get("redriveCreatesApplicationEvent")
                        is True
                        or raw_redrive_assertions.get(
                            "redrive_creates_application_event"
                        )
                        is True
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackRedriveInvalid",
                                "message": "callback redrive evidence required for redriveCreatesApplicationEvent",
                                "path": "$.redrive",
                            }
                        )
                        expected_keys_with_structural_diagnostics.add(
                            "redriveCreatesApplicationEvent"
                        )
                for index, raw_delivery in enumerate(raw_deliveries):
                    if not isinstance(raw_delivery, Mapping):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery must be object",
                                "path": f"$.deliveries[{index}]",
                            }
                        )
                deliveries = [
                    (index, delivery)
                    for index, delivery in enumerate(raw_deliveries)
                    if isinstance(delivery, Mapping)
                ]
                valid_delivery_statuses = {
                    "pending",
                    "delivering",
                    "delivered",
                    "acknowledged",
                    "failed",
                    "dead_lettered",
                    "cancelled",
                    "expired",
                }
                receiver_statuses = []
                next_retry_at_values = []
                seen_delivery_ids = set()
                seen_idempotency_keys: dict[str, tuple[str, str]] = {}
                idempotency_keys_unique_per_subscription_event = True
                for index, delivery in deliveries:
                    for key, alias in (
                        ("deliveryId", "delivery_id"),
                        ("subscriptionId", "subscription_id"),
                        ("eventId", "event_id"),
                        ("runId", "run_id"),
                        ("cursor", "cursor"),
                    ):
                        value = delivery.get(key, delivery.get(alias))
                        if not isinstance(value, str) or not value.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": f"callback delivery requires {key}",
                                    "path": f"$.deliveries[{index}].{key}",
                                }
                            )
                    delivery_id = delivery.get(
                        "deliveryId", delivery.get("delivery_id")
                    )
                    if isinstance(delivery_id, str) and delivery_id.strip():
                        normalized_delivery_id = delivery_id.strip()
                        if normalized_delivery_id in seen_delivery_ids:
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "callback delivery deliveryId must be unique",
                                    "path": f"$.deliveries[{index}].deliveryId",
                                }
                            )
                        else:
                            seen_delivery_ids.add(normalized_delivery_id)
                    sequence = delivery.get("sequence")
                    if (
                        isinstance(sequence, bool)
                        or not isinstance(sequence, int)
                        or sequence < 0
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery requires integer sequence",
                                "path": f"$.deliveries[{index}].sequence",
                            }
                        )
                    elif sequence == 0:
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery requires positive integer sequence",
                                "path": f"$.deliveries[{index}].sequence",
                            }
                        )
                    attempt = delivery.get("attempt")
                    if (
                        isinstance(attempt, bool)
                        or not isinstance(attempt, int)
                        or attempt < 0
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery requires integer attempt",
                                "path": f"$.deliveries[{index}].attempt",
                            }
                        )
                    elif attempt == 0:
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery requires positive integer attempt",
                                "path": f"$.deliveries[{index}].attempt",
                            }
                        )
                    idempotency_key = delivery.get(
                        "idempotencyKey", delivery.get("idempotency_key")
                    )
                    if (
                        not isinstance(idempotency_key, str)
                        or not idempotency_key.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery requires idempotencyKey",
                                "path": f"$.deliveries[{index}].idempotencyKey",
                            }
                        )
                    else:
                        subscription_id = delivery.get(
                            "subscriptionId", delivery.get("subscription_id")
                        )
                        event_id = delivery.get("eventId", delivery.get("event_id"))
                        logical_delivery = (
                            subscription_id.strip()
                            if isinstance(subscription_id, str)
                            else "",
                            event_id.strip() if isinstance(event_id, str) else "",
                        )
                        normalized_idempotency_key = idempotency_key.strip()
                        previous_delivery = seen_idempotency_keys.get(
                            normalized_idempotency_key
                        )
                        if previous_delivery is None:
                            seen_idempotency_keys[normalized_idempotency_key] = (
                                logical_delivery
                            )
                        elif previous_delivery != logical_delivery:
                            idempotency_keys_unique_per_subscription_event = False
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "callback delivery idempotencyKey must be unique",
                                    "path": f"$.deliveries[{index}].idempotencyKey",
                                }
                            )
                    delivery_subscription_id = delivery.get(
                        "subscriptionId", delivery.get("subscription_id")
                    )
                    if (
                        subscription_identity is not None
                        and isinstance(delivery_subscription_id, str)
                        and delivery_subscription_id.strip()
                        and delivery_subscription_id.strip() != subscription_identity
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery subscriptionId must match subscription",
                                "path": f"$.deliveries[{index}].subscriptionId",
                            }
                        )
                    raw_status = delivery.get("status")
                    status_is_valid = (
                        isinstance(raw_status, str)
                        and raw_status in valid_delivery_statuses
                    )
                    if not status_is_valid:
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery has invalid status",
                                "path": f"$.deliveries[{index}].status",
                            }
                        )
                    status = raw_status if isinstance(raw_status, str) else ""
                    if status in {"pending", "delivering"} and (
                        "deliveredAt" in delivery or "delivered_at" in delivery
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": f"{status} callback delivery must not have deliveredAt",
                                "path": f"$.deliveries[{index}].deliveredAt",
                            }
                        )
                    if status != "acknowledged" and (
                        "acknowledgedAt" in delivery or "acknowledged_at" in delivery
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": f"{status} callback delivery must not have acknowledgedAt",
                                "path": f"$.deliveries[{index}].acknowledgedAt",
                            }
                        )
                    raw_receiver_status = delivery.get(
                        "receiverStatus", delivery.get("receiver_status")
                    )
                    receiver_status = None
                    if raw_receiver_status is not None:
                        if isinstance(raw_receiver_status, bool) or not isinstance(
                            raw_receiver_status, int
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "callback delivery requires integer receiverStatus",
                                    "path": f"$.deliveries[{index}].receiverStatus",
                                }
                            )
                        elif raw_receiver_status < 100 or raw_receiver_status > 599:
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "callback delivery receiverStatus must be an HTTP status code",
                                    "path": f"$.deliveries[{index}].receiverStatus",
                                }
                            )
                        else:
                            receiver_status = raw_receiver_status
                    receiver_statuses.append(receiver_status)
                    raw_next_retry_at = delivery.get(
                        "nextRetryAt", delivery.get("next_retry_at")
                    )
                    next_retry_at = None
                    if raw_next_retry_at is not None:
                        if (
                            not isinstance(raw_next_retry_at, str)
                            or not raw_next_retry_at.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "callback delivery requires nextRetryAt timestamp",
                                    "path": f"$.deliveries[{index}].nextRetryAt",
                                }
                            )
                        else:
                            next_retry_at_text = raw_next_retry_at.strip()
                            if (
                                len(next_retry_at_text) <= 10
                                or next_retry_at_text[10] != "T"
                            ):
                                diagnostics.append(
                                    {
                                        "code": "DurableCallbackDeliveryInvalid",
                                        "message": "callback delivery requires nextRetryAt timestamp",
                                        "path": f"$.deliveries[{index}].nextRetryAt",
                                    }
                                )
                            else:
                                suffix = next_retry_at_text[19:]
                                suffix_valid = False
                                if suffix.startswith("."):
                                    offset_start = min(
                                        (
                                            position
                                            for position in (
                                                suffix.find("Z"),
                                                suffix.find("+"),
                                                suffix.find("-"),
                                            )
                                            if position >= 0
                                        ),
                                        default=-1,
                                    )
                                    if (
                                        offset_start > 1
                                        and suffix[1:offset_start].isdigit()
                                    ):
                                        suffix = suffix[offset_start:]
                                if suffix == "Z":
                                    suffix_valid = True
                                elif (
                                    len(suffix) == 6
                                    and suffix[0] in "+-"
                                    and suffix[1:3].isdigit()
                                    and suffix[3] == ":"
                                    and suffix[4:6].isdigit()
                                    and 0 <= int(suffix[1:3]) <= 23
                                    and 0 <= int(suffix[4:6]) <= 59
                                ):
                                    suffix_valid = True
                                if not suffix_valid:
                                    diagnostics.append(
                                        {
                                            "code": "DurableCallbackDeliveryInvalid",
                                            "message": "callback delivery requires nextRetryAt timestamp",
                                            "path": f"$.deliveries[{index}].nextRetryAt",
                                        }
                                    )
                                else:
                                    if next_retry_at_text.endswith("Z"):
                                        next_retry_at_text = (
                                            f"{next_retry_at_text[:-1]}+00:00"
                                        )
                                    try:
                                        datetime.fromisoformat(next_retry_at_text)
                                    except ValueError:
                                        diagnostics.append(
                                            {
                                                "code": "DurableCallbackDeliveryInvalid",
                                                "message": "callback delivery requires nextRetryAt timestamp",
                                                "path": f"$.deliveries[{index}].nextRetryAt",
                                            }
                                        )
                                    else:
                                        next_retry_at = raw_next_retry_at
                    next_retry_at_values.append(next_retry_at)
                    if (
                        receiver_status is not None
                        and (receiver_status == 429 or receiver_status >= 500)
                        and next_retry_at is not None
                        and status != "failed"
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery retry requires failed status",
                                "path": f"$.deliveries[{index}].status",
                            }
                        )
                    if (
                        receiver_status is not None
                        and 200 <= receiver_status <= 299
                        and status_is_valid
                        and status not in {"delivered", "acknowledged"}
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "2xx callback delivery requires delivered or acknowledged status",
                                "path": f"$.deliveries[{index}].status",
                            }
                        )
                    if (
                        raw_next_retry_at is not None
                        and status
                        in {
                            "delivered",
                            "acknowledged",
                            "dead_lettered",
                            "cancelled",
                            "expired",
                        }
                        and not (
                            receiver_status is not None
                            and (receiver_status == 429 or receiver_status >= 500)
                        )
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "terminal callback delivery must not have nextRetryAt",
                                "path": f"$.deliveries[{index}].nextRetryAt",
                            }
                        )
                    if (
                        subscription_failure_policy == "retry_then_dead_letter"
                        and receiver_status is not None
                        and (receiver_status == 429 or receiver_status >= 500)
                        and status == "failed"
                        and raw_next_retry_at is None
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "retry_then_dead_letter callback delivery requires nextRetryAt",
                                "path": f"$.deliveries[{index}].nextRetryAt",
                            }
                        )
                    if receiver_status == 409 and status != "acknowledged":
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery duplicate 409 requires acknowledged status",
                                "path": f"$.deliveries[{index}].status",
                            }
                        )
                    if (
                        receiver_status == 410
                        and status_is_valid
                        and status != "cancelled"
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "410 callback delivery requires cancelled status",
                                "path": f"$.deliveries[{index}].status",
                            }
                        )
                    if receiver_status == 410 and status == "cancelled":
                        last_error = delivery.get(
                            "lastError", delivery.get("last_error")
                        )
                        if (
                            isinstance(last_error, str)
                            and last_error.strip()
                            and last_error != "subscription_gone"
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "410 callback delivery requires subscription_gone error",
                                    "path": f"$.deliveries[{index}].lastError",
                                }
                            )
                    if (
                        receiver_status is not None
                        and 400 <= receiver_status <= 499
                        and receiver_status not in {409, 410, 429}
                        and status_is_valid
                        and status != "failed"
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "non-retryable 4xx callback delivery requires failed status",
                                "path": f"$.deliveries[{index}].status",
                            }
                        )
                    if (
                        receiver_status is not None
                        and 400 <= receiver_status <= 499
                        and receiver_status not in {409, 410, 429}
                        and status == "failed"
                    ):
                        last_error = delivery.get(
                            "lastError", delivery.get("last_error")
                        )
                        if (
                            isinstance(last_error, str)
                            and last_error.strip()
                            and last_error != "non_retryable"
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "non-retryable 4xx callback delivery requires non_retryable error",
                                    "path": f"$.deliveries[{index}].lastError",
                                }
                            )
                    delivered_at = None
                    if status in {"delivered", "acknowledged"}:
                        raw_delivered_at = delivery.get(
                            "deliveredAt", delivery.get("delivered_at")
                        )
                        if (
                            not isinstance(raw_delivered_at, str)
                            or not raw_delivered_at.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": f"{status} callback delivery requires deliveredAt",
                                    "path": f"$.deliveries[{index}].deliveredAt",
                                }
                            )
                        else:
                            delivered_at_text = raw_delivered_at.strip()
                            if (
                                len(delivered_at_text) <= 10
                                or delivered_at_text[10] != "T"
                            ):
                                diagnostics.append(
                                    {
                                        "code": "DurableCallbackDeliveryInvalid",
                                        "message": f"{status} callback delivery requires deliveredAt",
                                        "path": f"$.deliveries[{index}].deliveredAt",
                                    }
                                )
                            else:
                                suffix = delivered_at_text[19:]
                                suffix_valid = False
                                if suffix.startswith("."):
                                    offset_start = min(
                                        (
                                            position
                                            for position in (
                                                suffix.find("Z"),
                                                suffix.find("+"),
                                                suffix.find("-"),
                                            )
                                            if position >= 0
                                        ),
                                        default=-1,
                                    )
                                    if (
                                        offset_start > 1
                                        and suffix[1:offset_start].isdigit()
                                    ):
                                        suffix = suffix[offset_start:]
                                if suffix == "Z":
                                    suffix_valid = True
                                elif (
                                    len(suffix) == 6
                                    and suffix[0] in "+-"
                                    and suffix[1:3].isdigit()
                                    and suffix[3] == ":"
                                    and suffix[4:6].isdigit()
                                    and 0 <= int(suffix[1:3]) <= 23
                                    and 0 <= int(suffix[4:6]) <= 59
                                ):
                                    suffix_valid = True
                                if not suffix_valid:
                                    diagnostics.append(
                                        {
                                            "code": "DurableCallbackDeliveryInvalid",
                                            "message": f"{status} callback delivery requires deliveredAt",
                                            "path": f"$.deliveries[{index}].deliveredAt",
                                        }
                                    )
                                else:
                                    if delivered_at_text.endswith("Z"):
                                        delivered_at_text = (
                                            f"{delivered_at_text[:-1]}+00:00"
                                        )
                                    try:
                                        delivered_at = datetime.fromisoformat(
                                            delivered_at_text
                                        )
                                    except ValueError:
                                        diagnostics.append(
                                            {
                                                "code": "DurableCallbackDeliveryInvalid",
                                                "message": f"{status} callback delivery requires deliveredAt",
                                                "path": f"$.deliveries[{index}].deliveredAt",
                                            }
                                        )
                    if status == "acknowledged":
                        acknowledged_at = None
                        raw_acknowledged_at = delivery.get(
                            "acknowledgedAt", delivery.get("acknowledged_at")
                        )
                        if (
                            not isinstance(raw_acknowledged_at, str)
                            or not raw_acknowledged_at.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "acknowledged callback delivery requires acknowledgedAt",
                                    "path": f"$.deliveries[{index}].acknowledgedAt",
                                }
                            )
                        else:
                            acknowledged_at_text = raw_acknowledged_at.strip()
                            if (
                                len(acknowledged_at_text) <= 10
                                or acknowledged_at_text[10] != "T"
                            ):
                                diagnostics.append(
                                    {
                                        "code": "DurableCallbackDeliveryInvalid",
                                        "message": "acknowledged callback delivery requires acknowledgedAt",
                                        "path": f"$.deliveries[{index}].acknowledgedAt",
                                    }
                                )
                            else:
                                suffix = acknowledged_at_text[19:]
                                suffix_valid = False
                                if suffix.startswith("."):
                                    offset_start = min(
                                        (
                                            position
                                            for position in (
                                                suffix.find("Z"),
                                                suffix.find("+"),
                                                suffix.find("-"),
                                            )
                                            if position >= 0
                                        ),
                                        default=-1,
                                    )
                                    if (
                                        offset_start > 1
                                        and suffix[1:offset_start].isdigit()
                                    ):
                                        suffix = suffix[offset_start:]
                                if suffix == "Z":
                                    suffix_valid = True
                                elif (
                                    len(suffix) == 6
                                    and suffix[0] in "+-"
                                    and suffix[1:3].isdigit()
                                    and suffix[3] == ":"
                                    and suffix[4:6].isdigit()
                                    and 0 <= int(suffix[1:3]) <= 23
                                    and 0 <= int(suffix[4:6]) <= 59
                                ):
                                    suffix_valid = True
                                if not suffix_valid:
                                    diagnostics.append(
                                        {
                                            "code": "DurableCallbackDeliveryInvalid",
                                            "message": "acknowledged callback delivery requires acknowledgedAt",
                                            "path": f"$.deliveries[{index}].acknowledgedAt",
                                        }
                                    )
                                else:
                                    if acknowledged_at_text.endswith("Z"):
                                        acknowledged_at_text = (
                                            f"{acknowledged_at_text[:-1]}+00:00"
                                        )
                                    try:
                                        acknowledged_at = datetime.fromisoformat(
                                            acknowledged_at_text
                                        )
                                    except ValueError:
                                        diagnostics.append(
                                            {
                                                "code": "DurableCallbackDeliveryInvalid",
                                                "message": "acknowledged callback delivery requires acknowledgedAt",
                                                "path": f"$.deliveries[{index}].acknowledgedAt",
                                            }
                                        )
                        if (
                            delivered_at is not None
                            and acknowledged_at is not None
                            and acknowledged_at < delivered_at
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": "acknowledgedAt must not be before deliveredAt",
                                    "path": f"$.deliveries[{index}].acknowledgedAt",
                                }
                            )
                    if status in {"failed", "dead_lettered", "cancelled", "expired"}:
                        last_error = delivery.get(
                            "lastError", delivery.get("last_error")
                        )
                        if not isinstance(last_error, str) or not last_error.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableCallbackDeliveryInvalid",
                                    "message": f"{status} callback delivery requires lastError",
                                    "path": f"$.deliveries[{index}].lastError",
                                }
                            )
                scheduled_retry_ids = []
                scheduled_retryable_status_ids = []
                delivered_after_2xx_ids = []
                acknowledged_duplicates = []
                subscription_gone_ids = []
                non_retryable_4xx_ids = []
                for position, (_index, delivery) in enumerate(deliveries):
                    receiver_status = receiver_statuses[position]
                    next_retry_at = next_retry_at_values[position]
                    delivery_id = str(
                        delivery.get("deliveryId", delivery.get("delivery_id", ""))
                    )
                    if (
                        receiver_status is not None
                        and receiver_status >= 500
                        and next_retry_at is not None
                    ):
                        scheduled_retry_ids.append(delivery_id)
                    if (
                        receiver_status is not None
                        and (receiver_status == 429 or receiver_status >= 500)
                        and next_retry_at is not None
                    ):
                        scheduled_retryable_status_ids.append(delivery_id)
                    if (
                        receiver_status is not None
                        and 200 <= receiver_status <= 299
                        and str(delivery.get("status", "")) == "delivered"
                    ):
                        delivered_after_2xx_ids.append(delivery_id)
                    if (
                        receiver_status == 409
                        and str(delivery.get("status", "")) == "acknowledged"
                    ):
                        acknowledged_duplicates.append(delivery_id)
                    if (
                        receiver_status == 410
                        and str(delivery.get("status", "")) == "cancelled"
                        and str(
                            delivery.get("lastError", delivery.get("last_error", ""))
                        )
                        == "subscription_gone"
                    ):
                        subscription_gone_ids.append(delivery_id)
                    if (
                        receiver_status is not None
                        and 400 <= receiver_status <= 499
                        and receiver_status not in {409, 410, 429}
                        and str(delivery.get("status", "")) == "failed"
                        and str(
                            delivery.get("lastError", delivery.get("last_error", ""))
                        )
                        == "non_retryable"
                    ):
                        non_retryable_4xx_ids.append(delivery_id)
                observed = {
                    "retryScheduledAfter5xx": bool(scheduled_retry_ids),
                    "retryScheduledAfterRetryableStatus": bool(
                        scheduled_retryable_status_ids
                    ),
                    "deliveredAfter2xx": bool(delivered_after_2xx_ids),
                    "duplicate409Acknowledged": bool(acknowledged_duplicates),
                    "subscriptionGoneAfter410": bool(subscription_gone_ids),
                    "nonRetryable4xxTerminal": bool(non_retryable_4xx_ids),
                    "idempotencyKeysUniquePerSubscriptionEvent": idempotency_keys_unique_per_subscription_event,
                    "deadLetterPreservesEventId": redrive_event_id_preserved,
                    "redriveCreatesApplicationEvent": redrive_creates_application_event,
                    "nonMandatoryOutageBlocksRun": non_mandatory_outage_blocks_run,
                }
            elif kind == "async_callback_resume_guards":
                raw_checks = fixture.get("checks", {})
                raw_resume = fixture.get("resume", {})
                raw_callback = fixture.get("callback", {})
                if (
                    not isinstance(raw_checks, Mapping)
                    or not isinstance(raw_resume, Mapping)
                    or not isinstance(raw_callback, Mapping)
                ):
                    raise ValueError(
                        "durable async_callback_resume_guards case requires checks, callback, and resume"
                    )
                raw_operation = fixture.get("operation")
                operation_deadline_at = None
                if raw_operation is not None and not isinstance(raw_operation, Mapping):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume operation must be object",
                            "path": "$.operation",
                        }
                    )
                if isinstance(raw_operation, Mapping):
                    for key, alias in (
                        ("operationId", "operation_id"),
                        ("runId", "run_id"),
                        ("nodeId", "node_id"),
                        ("attemptId", "attempt_id"),
                        ("idempotencyKey", "idempotency_key"),
                        ("releaseId", "release_id"),
                        ("tenantId", "tenant_id"),
                        ("policySnapshotId", "policy_snapshot_id"),
                    ):
                        path_key = (
                            key
                            if key in raw_operation or alias not in raw_operation
                            else alias
                        )
                        value = raw_operation.get(key, raw_operation.get(alias))
                        if not isinstance(value, str) or not value.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": f"async callback resume operation requires nonblank {key}",
                                    "path": f"$.operation.{path_key}",
                                }
                            )
                        elif value != value.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": f"async callback resume operation {key} must not contain surrounding whitespace",
                                    "path": f"$.operation.{path_key}",
                                }
                            )
                    if (
                        "providerOperationId" in raw_operation
                        or "provider_operation_id" in raw_operation
                    ):
                        provider_operation_id_path = (
                            "providerOperationId"
                            if "providerOperationId" in raw_operation
                            or "provider_operation_id" not in raw_operation
                            else "provider_operation_id"
                        )
                        provider_operation_id = raw_operation.get(
                            "providerOperationId",
                            raw_operation.get("provider_operation_id"),
                        )
                        if (
                            not isinstance(provider_operation_id, str)
                            or not provider_operation_id.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume operation requires nonblank providerOperationId",
                                    "path": f"$.operation.{provider_operation_id_path}",
                                }
                            )
                        elif provider_operation_id != provider_operation_id.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume operation providerOperationId must not contain surrounding whitespace",
                                    "path": f"$.operation.{provider_operation_id_path}",
                                }
                            )
                    if (
                        "state" in raw_operation
                        or "operationState" in raw_operation
                        or "operation_state" in raw_operation
                    ):
                        if "state" in raw_operation:
                            operation_state_path = "state"
                        elif (
                            "operationState" in raw_operation
                            or "operation_state" not in raw_operation
                        ):
                            operation_state_path = "operationState"
                        else:
                            operation_state_path = "operation_state"
                        operation_state = raw_operation.get(
                            "state",
                            raw_operation.get(
                                "operationState", raw_operation.get("operation_state")
                            ),
                        )
                        if (
                            not isinstance(operation_state, str)
                            or operation_state.strip() != "waiting_callback"
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume operation state must be waiting_callback",
                                    "path": f"$.operation.{operation_state_path}",
                                }
                            )
                    resume_token_hash_path = (
                        "resumeTokenHash"
                        if "resumeTokenHash" in raw_operation
                        or "resume_token_hash" not in raw_operation
                        else "resume_token_hash"
                    )
                    resume_token_hash = raw_operation.get(
                        "resumeTokenHash", raw_operation.get("resume_token_hash")
                    )
                    if (
                        not isinstance(resume_token_hash, str)
                        or not resume_token_hash.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation requires resumeTokenHash sha256 digest",
                                "path": f"$.operation.{resume_token_hash_path}",
                            }
                        )
                    elif resume_token_hash != resume_token_hash.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation resumeTokenHash must not contain surrounding whitespace",
                                "path": f"$.operation.{resume_token_hash_path}",
                            }
                        )
                    elif (
                        not resume_token_hash.startswith("sha256:")
                        or len(resume_token_hash.removeprefix("sha256:")) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in resume_token_hash.removeprefix("sha256:")
                        )
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation requires resumeTokenHash sha256 digest",
                                "path": f"$.operation.{resume_token_hash_path}",
                            }
                        )
                    expected_schema_path = (
                        "expectedSchema"
                        if "expectedSchema" in raw_operation
                        or "expected_schema" not in raw_operation
                        else "expected_schema"
                    )
                    expected_schema = raw_operation.get(
                        "expectedSchema", raw_operation.get("expected_schema")
                    )
                    if (
                        not isinstance(expected_schema, str)
                        or not expected_schema.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation requires nonblank expectedSchema",
                                "path": f"$.operation.{expected_schema_path}",
                            }
                        )
                    elif expected_schema != expected_schema.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation expectedSchema must not contain surrounding whitespace",
                                "path": f"$.operation.{expected_schema_path}",
                            }
                        )
                    if "kind" in raw_operation:
                        operation_kind = raw_operation.get("kind")
                        if operation_kind not in {
                            "tool",
                            "sandbox_task",
                            "ci_job",
                            "browser_task",
                            "workspace_trial",
                            "external_provider_job",
                            "document_job",
                            "research_task",
                            "custom",
                        }:
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume operation requires valid operation kind",
                                    "path": "$.operation.kind",
                                }
                            )
                    deadline = raw_operation.get("deadline")
                    if not isinstance(deadline, str) or not deadline.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation requires ISO deadline",
                                "path": "$.operation.deadline",
                            }
                        )
                    elif deadline != deadline.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation deadline must not contain surrounding whitespace",
                                "path": "$.operation.deadline",
                            }
                        )
                    else:
                        deadline_text = deadline
                        if len(deadline_text) <= 10 or deadline_text[10] != "T":
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume operation requires ISO deadline",
                                    "path": "$.operation.deadline",
                                }
                            )
                        else:
                            suffix = deadline_text[19:]
                            suffix_valid = False
                            if suffix.startswith("."):
                                offset_start = min(
                                    (
                                        position
                                        for position in (
                                            suffix.find("Z"),
                                            suffix.find("+"),
                                            suffix.find("-"),
                                        )
                                        if position >= 0
                                    ),
                                    default=-1,
                                )
                                if (
                                    offset_start > 1
                                    and suffix[1:offset_start].isdigit()
                                ):
                                    suffix = suffix[offset_start:]
                            if suffix == "Z":
                                suffix_valid = True
                            elif (
                                len(suffix) == 6
                                and suffix[0] in "+-"
                                and suffix[1:3].isdigit()
                                and suffix[3] == ":"
                                and suffix[4:6].isdigit()
                                and 0 <= int(suffix[1:3]) <= 23
                                and 0 <= int(suffix[4:6]) <= 59
                            ):
                                suffix_valid = True
                            if not suffix_valid:
                                diagnostics.append(
                                    {
                                        "code": "DurableAsyncCallbackResumeInvalid",
                                        "message": "async callback resume operation requires ISO deadline",
                                        "path": "$.operation.deadline",
                                    }
                                )
                            else:
                                if deadline_text.endswith("Z"):
                                    deadline_text = f"{deadline_text[:-1]}+00:00"
                                try:
                                    operation_deadline_at = datetime.fromisoformat(
                                        deadline_text
                                    )
                                    if operation_deadline_at.tzinfo is None:
                                        operation_deadline_at = (
                                            operation_deadline_at.replace(
                                                tzinfo=timezone.utc
                                            )
                                        )
                                    operation_deadline_at = (
                                        operation_deadline_at.astimezone(timezone.utc)
                                    )
                                except ValueError:
                                    diagnostics.append(
                                        {
                                            "code": "DurableAsyncCallbackResumeInvalid",
                                            "message": "async callback resume operation requires ISO deadline",
                                            "path": "$.operation.deadline",
                                        }
                                    )
                    budget_state_path = (
                        "budgetState"
                        if "budgetState" in raw_operation
                        or "budget_state" not in raw_operation
                        else "budget_state"
                    )
                    budget_state = raw_operation.get(
                        "budgetState", raw_operation.get("budget_state")
                    )
                    if not isinstance(budget_state, str) or not budget_state.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation requires nonblank budgetState",
                                "path": f"$.operation.{budget_state_path}",
                            }
                        )
                    elif budget_state != budget_state.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume operation budgetState must not contain surrounding whitespace",
                                "path": f"$.operation.{budget_state_path}",
                            }
                        )
                operation_provider_operation_id = None
                if isinstance(raw_operation, Mapping):
                    raw_operation_provider_operation_id = raw_operation.get(
                        "providerOperationId",
                        raw_operation.get("provider_operation_id"),
                    )
                    if (
                        isinstance(raw_operation_provider_operation_id, str)
                        and raw_operation_provider_operation_id.strip()
                    ):
                        operation_provider_operation_id = (
                            raw_operation_provider_operation_id.strip()
                        )
                callback_receipt_supplied = any(
                    key in raw_callback
                    for key in (
                        "callbackId",
                        "callback_id",
                        "payloadDigest",
                        "payload_digest",
                        "verifiedBy",
                        "verified_by",
                        "idempotencyKey",
                        "idempotency_key",
                        "receivedAt",
                        "received_at",
                        "releaseId",
                        "release_id",
                        "tenantId",
                        "tenant_id",
                        "providerOperationId",
                        "provider_operation_id",
                        "eventType",
                        "event_type",
                        "payloadSchemaValid",
                        "payload_schema_valid",
                        "signatureVerified",
                        "signature_verified",
                    )
                )
                if callback_receipt_supplied:
                    if "eventType" in raw_callback or "event_type" in raw_callback:
                        event_type_path = (
                            "eventType"
                            if "eventType" in raw_callback
                            or "event_type" not in raw_callback
                            else "event_type"
                        )
                        event_type = raw_callback.get(
                            "eventType", raw_callback.get("event_type")
                        )
                        if (
                            not isinstance(event_type, str)
                            or event_type.strip() != "ExternalCallbackReceived"
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback eventType must be ExternalCallbackReceived",
                                    "path": f"$.callback.{event_type_path}",
                                }
                            )
                    if (
                        "signatureVerified" in raw_callback
                        or "signature_verified" in raw_callback
                    ):
                        signature_verified_path = (
                            "signatureVerified"
                            if "signatureVerified" in raw_callback
                            or "signature_verified" not in raw_callback
                            else "signature_verified"
                        )
                        signature_verified = raw_callback.get(
                            "signatureVerified", raw_callback.get("signature_verified")
                        )
                        if signature_verified is not True:
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback signature must verify before receipt",
                                    "path": f"$.callback.{signature_verified_path}",
                                }
                            )
                    if (
                        "payloadSchemaValid" in raw_callback
                        or "payload_schema_valid" in raw_callback
                    ):
                        payload_schema_valid_path = (
                            "payloadSchemaValid"
                            if "payloadSchemaValid" in raw_callback
                            or "payload_schema_valid" not in raw_callback
                            else "payload_schema_valid"
                        )
                        payload_schema_valid = raw_callback.get(
                            "payloadSchemaValid",
                            raw_callback.get("payload_schema_valid"),
                        )
                        if payload_schema_valid is not True:
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback payload must validate against expectedSchema",
                                    "path": f"$.callback.{payload_schema_valid_path}",
                                }
                            )
                    callback_id_path = (
                        "callbackId"
                        if "callbackId" in raw_callback
                        or "callback_id" not in raw_callback
                        else "callback_id"
                    )
                    callback_id = raw_callback.get(
                        "callbackId", raw_callback.get("callback_id")
                    )
                    if not isinstance(callback_id, str) or not callback_id.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires nonblank callbackId",
                                "path": f"$.callback.{callback_id_path}",
                            }
                        )
                    elif callback_id != callback_id.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback callbackId must not contain surrounding whitespace",
                                "path": f"$.callback.{callback_id_path}",
                            }
                        )
                    payload_digest_path = (
                        "payloadDigest"
                        if "payloadDigest" in raw_callback
                        or "payload_digest" not in raw_callback
                        else "payload_digest"
                    )
                    payload_digest = raw_callback.get(
                        "payloadDigest", raw_callback.get("payload_digest")
                    )
                    if (
                        not isinstance(payload_digest, str)
                        or not payload_digest.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires payloadDigest sha256 digest",
                                "path": f"$.callback.{payload_digest_path}",
                            }
                        )
                    elif payload_digest != payload_digest.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback payloadDigest must not contain surrounding whitespace",
                                "path": f"$.callback.{payload_digest_path}",
                            }
                        )
                    elif (
                        not payload_digest.startswith("sha256:")
                        or len(payload_digest.removeprefix("sha256:")) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in payload_digest.removeprefix("sha256:")
                        )
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires payloadDigest sha256 digest",
                                "path": f"$.callback.{payload_digest_path}",
                            }
                        )
                    verified_by_path = (
                        "verifiedBy"
                        if "verifiedBy" in raw_callback
                        or "verified_by" not in raw_callback
                        else "verified_by"
                    )
                    verified_by = raw_callback.get(
                        "verifiedBy", raw_callback.get("verified_by")
                    )
                    if not isinstance(verified_by, str) or not verified_by.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires nonblank verifiedBy",
                                "path": f"$.callback.{verified_by_path}",
                            }
                        )
                    elif verified_by != verified_by.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback verifiedBy must not contain surrounding whitespace",
                                "path": f"$.callback.{verified_by_path}",
                            }
                        )
                    elif verified_by.strip().lower() == "unauthenticated":
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires authenticated verifiedBy",
                                "path": f"$.callback.{verified_by_path}",
                            }
                        )
                    idempotency_key_path = (
                        "idempotencyKey"
                        if "idempotencyKey" in raw_callback
                        or "idempotency_key" not in raw_callback
                        else "idempotency_key"
                    )
                    idempotency_key = raw_callback.get(
                        "idempotencyKey", raw_callback.get("idempotency_key")
                    )
                    if (
                        not isinstance(idempotency_key, str)
                        or not idempotency_key.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires nonblank idempotencyKey",
                                "path": f"$.callback.{idempotency_key_path}",
                            }
                        )
                    elif idempotency_key != idempotency_key.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback idempotencyKey must not contain surrounding whitespace",
                                "path": f"$.callback.{idempotency_key_path}",
                            }
                        )
                    received_at_path = (
                        "receivedAt"
                        if "receivedAt" in raw_callback
                        or "received_at" not in raw_callback
                        else "received_at"
                    )
                    received_at = raw_callback.get(
                        "receivedAt", raw_callback.get("received_at")
                    )
                    callback_received_at = None
                    if not isinstance(received_at, str) or not received_at.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires ISO receivedAt",
                                "path": f"$.callback.{received_at_path}",
                            }
                        )
                    elif received_at != received_at.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback receivedAt must not contain surrounding whitespace",
                                "path": f"$.callback.{received_at_path}",
                            }
                        )
                    else:
                        received_at_text = received_at
                        if len(received_at_text) <= 10 or received_at_text[10] != "T":
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback requires ISO receivedAt",
                                    "path": f"$.callback.{received_at_path}",
                                }
                            )
                        else:
                            suffix = received_at_text[19:]
                            suffix_valid = False
                            if suffix.startswith("."):
                                offset_start = min(
                                    (
                                        position
                                        for position in (
                                            suffix.find("Z"),
                                            suffix.find("+"),
                                            suffix.find("-"),
                                        )
                                        if position >= 0
                                    ),
                                    default=-1,
                                )
                                if (
                                    offset_start > 1
                                    and suffix[1:offset_start].isdigit()
                                ):
                                    suffix = suffix[offset_start:]
                            if suffix == "Z":
                                suffix_valid = True
                            elif (
                                len(suffix) == 6
                                and suffix[0] in "+-"
                                and suffix[1:3].isdigit()
                                and suffix[3] == ":"
                                and suffix[4:6].isdigit()
                                and 0 <= int(suffix[1:3]) <= 23
                                and 0 <= int(suffix[4:6]) <= 59
                            ):
                                suffix_valid = True
                            if not suffix_valid:
                                diagnostics.append(
                                    {
                                        "code": "DurableAsyncCallbackResumeInvalid",
                                        "message": "async callback resume callback requires ISO receivedAt",
                                        "path": f"$.callback.{received_at_path}",
                                    }
                                )
                            else:
                                if received_at_text.endswith("Z"):
                                    received_at_text = f"{received_at_text[:-1]}+00:00"
                                try:
                                    callback_received_at = datetime.fromisoformat(
                                        received_at_text
                                    )
                                    if callback_received_at.tzinfo is None:
                                        callback_received_at = (
                                            callback_received_at.replace(
                                                tzinfo=timezone.utc
                                            )
                                        )
                                    callback_received_at = (
                                        callback_received_at.astimezone(timezone.utc)
                                    )
                                except ValueError:
                                    diagnostics.append(
                                        {
                                            "code": "DurableAsyncCallbackResumeInvalid",
                                            "message": "async callback resume callback requires ISO receivedAt",
                                            "path": f"$.callback.{received_at_path}",
                                        }
                                    )
                    if (
                        callback_received_at is not None
                        and operation_deadline_at is not None
                        and callback_received_at >= operation_deadline_at
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback receivedAt must be before operation deadline",
                                "path": f"$.callback.{received_at_path}",
                            }
                        )
                    release_id_path = (
                        "releaseId"
                        if "releaseId" in raw_callback
                        or "release_id" not in raw_callback
                        else "release_id"
                    )
                    release_id = raw_callback.get(
                        "releaseId", raw_callback.get("release_id")
                    )
                    if not isinstance(release_id, str) or not release_id.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires nonblank releaseId",
                                "path": f"$.callback.{release_id_path}",
                            }
                        )
                    elif release_id != release_id.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback releaseId must not contain surrounding whitespace",
                                "path": f"$.callback.{release_id_path}",
                            }
                        )
                    tenant_id_path = (
                        "tenantId"
                        if "tenantId" in raw_callback or "tenant_id" not in raw_callback
                        else "tenant_id"
                    )
                    tenant_id = raw_callback.get(
                        "tenantId", raw_callback.get("tenant_id")
                    )
                    if not isinstance(tenant_id, str) or not tenant_id.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback requires nonblank tenantId",
                                "path": f"$.callback.{tenant_id_path}",
                            }
                        )
                    elif tenant_id != tenant_id.strip():
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume callback tenantId must not contain surrounding whitespace",
                                "path": f"$.callback.{tenant_id_path}",
                            }
                        )
                    for key, alias in (
                        ("operationId", "operation_id"),
                        ("runId", "run_id"),
                        ("nodeId", "node_id"),
                        ("attemptId", "attempt_id"),
                        ("policySnapshotId", "policy_snapshot_id"),
                    ):
                        path_key = (
                            key
                            if key in raw_callback or alias not in raw_callback
                            else alias
                        )
                        value = raw_callback.get(key, raw_callback.get(alias))
                        if not isinstance(value, str) or not value.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": f"async callback resume callback requires nonblank {key}",
                                    "path": f"$.callback.{path_key}",
                                }
                            )
                        elif value != value.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": f"async callback resume callback {key} must not contain surrounding whitespace",
                                    "path": f"$.callback.{path_key}",
                                }
                            )
                    if operation_provider_operation_id is not None:
                        provider_operation_id_path = (
                            "providerOperationId"
                            if "providerOperationId" in raw_callback
                            or "provider_operation_id" not in raw_callback
                            else "provider_operation_id"
                        )
                        callback_provider_operation_id = raw_callback.get(
                            "providerOperationId",
                            raw_callback.get("provider_operation_id"),
                        )
                        if (
                            not isinstance(callback_provider_operation_id, str)
                            or not callback_provider_operation_id.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback requires providerOperationId",
                                    "path": f"$.callback.{provider_operation_id_path}",
                                }
                            )
                        elif (
                            callback_provider_operation_id
                            != callback_provider_operation_id.strip()
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback providerOperationId must not contain surrounding whitespace",
                                    "path": f"$.callback.{provider_operation_id_path}",
                                }
                            )
                        elif (
                            callback_provider_operation_id
                            != operation_provider_operation_id
                        ):
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume callback providerOperationId must match operation providerOperationId",
                                    "path": f"$.callback.{provider_operation_id_path}",
                                }
                            )
                    if isinstance(raw_operation, Mapping):
                        for key, alias in (
                            ("operationId", "operation_id"),
                            ("runId", "run_id"),
                            ("nodeId", "node_id"),
                            ("attemptId", "attempt_id"),
                            ("releaseId", "release_id"),
                            ("tenantId", "tenant_id"),
                            ("policySnapshotId", "policy_snapshot_id"),
                        ):
                            callback_value = raw_callback.get(
                                key, raw_callback.get(alias)
                            )
                            operation_value = raw_operation.get(
                                key, raw_operation.get(alias)
                            )
                            if (
                                isinstance(callback_value, str)
                                and isinstance(operation_value, str)
                                and callback_value.strip()
                                and operation_value.strip()
                                and callback_value.strip() != operation_value.strip()
                            ):
                                path_key = (
                                    key
                                    if key in raw_callback or alias not in raw_callback
                                    else alias
                                )
                                diagnostics.append(
                                    {
                                        "code": "DurableAsyncCallbackResumeInvalid",
                                        "message": f"async callback resume callback {key} must match operation {key}",
                                        "path": f"$.callback.{path_key}",
                                    }
                                )
                async_resume_guard_values = {}
                for key, alias in (
                    (
                        "signatureFailureRevealsOperation",
                        "signature_failure_reveals_operation",
                    ),
                    ("schemaFailureResumesRun", "schema_failure_resumes_run"),
                    (
                        "timeoutCallbackResumesExpiredOperation",
                        "timeout_callback_resumes_expired_operation",
                    ),
                    (
                        "cancelledCallbackCommitsResult",
                        "cancelled_callback_commits_result",
                    ),
                    ("staleAttemptCanResume", "stale_attempt_can_resume"),
                    (
                        "unauthenticatedCallbackCanResume",
                        "unauthenticated_callback_can_resume",
                    ),
                    (
                        "nonExternalCallbackEventCanBecomeReceipt",
                        "non_external_callback_event_can_become_receipt",
                    ),
                    (
                        "providerOperationMismatchCanResume",
                        "provider_operation_mismatch_can_resume",
                    ),
                ):
                    raw_value_missing = False
                    if key in raw_checks:
                        raw_value = raw_checks[key]
                        path_key = key
                    elif alias in raw_checks:
                        raw_value = raw_checks[alias]
                        path_key = alias
                    else:
                        raw_value = True
                        path_key = key
                        raw_value_missing = True
                    async_resume_guard_values[key] = (
                        raw_value if isinstance(raw_value, bool) else True
                    )
                    if raw_value_missing or not isinstance(raw_value, bool):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": f"async callback resume guard requires boolean {key}",
                                "path": f"$.checks.{path_key}",
                            }
                        )
                callback_journal_sequence_missing = False
                if "journalSequence" in raw_callback:
                    raw_callback_journal_sequence = raw_callback["journalSequence"]
                elif "journal_sequence" in raw_callback:
                    raw_callback_journal_sequence = raw_callback["journal_sequence"]
                else:
                    raw_callback_journal_sequence = 0
                    callback_journal_sequence_missing = True
                if (
                    callback_journal_sequence_missing
                    or isinstance(raw_callback_journal_sequence, bool)
                    or not isinstance(raw_callback_journal_sequence, int)
                    or raw_callback_journal_sequence < 0
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires integer callback journalSequence",
                            "path": "$.callback.journalSequence",
                        }
                    )
                    callback_journal_sequence = 0
                elif raw_callback_journal_sequence == 0:
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires positive integer callback journalSequence",
                            "path": "$.callback.journalSequence",
                        }
                    )
                    callback_journal_sequence = raw_callback_journal_sequence
                else:
                    callback_journal_sequence = raw_callback_journal_sequence
                resume_sequence_missing = False
                if "resumeSequence" in raw_resume:
                    raw_resume_sequence = raw_resume["resumeSequence"]
                elif "resume_sequence" in raw_resume:
                    raw_resume_sequence = raw_resume["resume_sequence"]
                else:
                    raw_resume_sequence = 0
                    resume_sequence_missing = True
                if (
                    resume_sequence_missing
                    or isinstance(raw_resume_sequence, bool)
                    or not isinstance(raw_resume_sequence, int)
                    or raw_resume_sequence < 0
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires integer resumeSequence",
                            "path": "$.resume.resumeSequence",
                        }
                    )
                    resume_sequence = 0
                elif raw_resume_sequence == 0:
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires positive integer resumeSequence",
                            "path": "$.resume.resumeSequence",
                        }
                    )
                    resume_sequence = raw_resume_sequence
                else:
                    resume_sequence = raw_resume_sequence
                if (
                    callback_journal_sequence > 0
                    and resume_sequence > 0
                    and callback_journal_sequence >= resume_sequence
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires callback journalSequence before resumeSequence",
                            "path": "$.resume.resumeSequence",
                        }
                    )
                successful_resume_count_missing = False
                if "successfulResumeCount" in raw_resume:
                    raw_successful_resume_count = raw_resume["successfulResumeCount"]
                elif "successful_resume_count" in raw_resume:
                    raw_successful_resume_count = raw_resume["successful_resume_count"]
                else:
                    raw_successful_resume_count = 0
                    successful_resume_count_missing = True
                if (
                    successful_resume_count_missing
                    or isinstance(raw_successful_resume_count, bool)
                    or not isinstance(raw_successful_resume_count, int)
                    or raw_successful_resume_count < 0
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires integer successfulResumeCount",
                            "path": "$.resume.successfulResumeCount",
                        }
                    )
                    successful_resume_count = 0
                else:
                    successful_resume_count = raw_successful_resume_count
                    if successful_resume_count != 1:
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires successfulResumeCount of 1",
                                "path": "$.resume.successfulResumeCount",
                            }
                        )
                budget_exhaustion_state_path = (
                    "budgetExhaustionState"
                    if "budgetExhaustionState" in raw_resume
                    or "budget_exhaustion_state" not in raw_resume
                    else "budget_exhaustion_state"
                )
                budget_exhaustion_state = raw_resume.get(
                    "budgetExhaustionState",
                    raw_resume.get("budget_exhaustion_state"),
                )
                if (
                    not isinstance(budget_exhaustion_state, str)
                    or budget_exhaustion_state.strip() != "paused_budget"
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires paused_budget budgetExhaustionState",
                            "path": f"$.resume.{budget_exhaustion_state_path}",
                        }
                    )
                resume_reevaluates_missing = "reevaluates" not in raw_resume
                raw_resume_reevaluates = raw_resume.get("reevaluates", ())
                resume_reevaluates = ()
                if (
                    resume_reevaluates_missing
                    or isinstance(raw_resume_reevaluates, (str, bytes))
                    or isinstance(raw_resume_reevaluates, Mapping)
                    or not isinstance(raw_resume_reevaluates, Sequence)
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume requires reevaluates sequence",
                            "path": "$.resume.reevaluates",
                        }
                    )
                else:
                    resume_reevaluates_values = []
                    for reevaluate_index, reevaluate in enumerate(
                        raw_resume_reevaluates
                    ):
                        if not isinstance(reevaluate, str) or not reevaluate.strip():
                            diagnostics.append(
                                {
                                    "code": "DurableAsyncCallbackResumeInvalid",
                                    "message": "async callback resume requires string reevaluates entry",
                                    "path": f"$.resume.reevaluates[{reevaluate_index}]",
                                }
                            )
                        else:
                            resume_reevaluates_values.append(reevaluate.strip())
                    resume_reevaluates = tuple(resume_reevaluates_values)
                    reevaluates_shape_valid = len(resume_reevaluates) == len(
                        raw_resume_reevaluates
                    )
                    if reevaluates_shape_valid and not (
                        set(resume_reevaluates) >= {"policy", "budget", "release"}
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires policy, budget, and release reevaluation",
                                "path": "$.resume.reevaluates",
                            }
                        )
                    if (
                        reevaluates_shape_valid
                        and "idempotency" not in resume_reevaluates
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires idempotency reevaluation",
                                "path": "$.resume.reevaluates",
                            }
                        )
                    if (
                        reevaluates_shape_valid
                        and not diagnostics
                        and "ownership_lease" not in resume_reevaluates
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCallbackResumeInvalid",
                                "message": "async callback resume requires ownership lease reevaluation",
                                "path": "$.resume.reevaluates",
                            }
                        )
                observed = {
                    "signatureFailureRevealsOperation": async_resume_guard_values[
                        "signatureFailureRevealsOperation"
                    ],
                    "schemaFailureResumesRun": async_resume_guard_values[
                        "schemaFailureResumesRun"
                    ],
                    "timeoutCallbackResumesExpiredOperation": async_resume_guard_values[
                        "timeoutCallbackResumesExpiredOperation"
                    ],
                    "cancelledCallbackCommitsResult": async_resume_guard_values[
                        "cancelledCallbackCommitsResult"
                    ],
                    "staleAttemptCanResume": async_resume_guard_values[
                        "staleAttemptCanResume"
                    ],
                    "unauthenticatedCallbackCanResume": async_resume_guard_values[
                        "unauthenticatedCallbackCanResume"
                    ],
                    "nonExternalCallbackEventCanBecomeReceipt": async_resume_guard_values[
                        "nonExternalCallbackEventCanBecomeReceipt"
                    ],
                    "providerOperationMismatchCanResume": async_resume_guard_values[
                        "providerOperationMismatchCanResume"
                    ],
                    "diagnosticCount": len(diagnostics),
                    "receiptJournaledBeforeResume": callback_journal_sequence
                    < resume_sequence,
                    "resumeReevaluatesPolicyBudgetRelease": set(resume_reevaluates)
                    >= {"policy", "budget", "release"},
                    "budgetExhaustionPausesResume": str(
                        raw_resume.get(
                            "budgetExhaustionState",
                            raw_resume.get("budget_exhaustion_state", ""),
                        )
                    )
                    == "paused_budget",
                    "coordinatorFailoverResumesOnce": successful_resume_count == 1,
                }
            elif kind == "async_callback_cancel_race":
                raw_journal = fixture.get("journal", ())
                raw_race = fixture.get("race", {})
                if not isinstance(raw_journal, Sequence) or isinstance(
                    raw_journal, (str, bytes)
                ):
                    raise ValueError(
                        "durable async_callback_cancel_race case requires journal"
                    )
                if not isinstance(raw_race, Mapping):
                    raise ValueError(
                        "durable async_callback_cancel_race case requires race"
                    )
                journal_entries = []
                for entry_index, entry in enumerate(raw_journal):
                    if not isinstance(entry, Mapping):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCancelRaceInvalid",
                                "message": "async cancel race journal entry must be object",
                                "path": f"$.journal[{entry_index}]",
                            }
                        )
                        continue
                    journal_entries.append((entry_index, entry))
                cancel_entries = [
                    entry
                    for _, entry in journal_entries
                    if str(entry.get("kind", "")).lower()
                    in {"cancelrun", "run_cancelled", "cancelled"}
                ]
                has_cancel_entry = bool(cancel_entries)
                callback_entries = [
                    entry
                    for _, entry in journal_entries
                    if str(entry.get("kind", "")).lower()
                    in {"externalcallbackreceived", "external_callback_received"}
                ]
                has_callback_entry = bool(callback_entries)
                journal_sequences = {}
                fences = set()
                for entry_index, entry in journal_entries:
                    ownership_fence_path = (
                        "ownershipFence"
                        if "ownershipFence" in entry or "ownership_fence" not in entry
                        else "ownership_fence"
                    )
                    ownership_fence = entry.get(
                        "ownershipFence", entry.get("ownership_fence")
                    )
                    if (
                        not isinstance(ownership_fence, str)
                        or not ownership_fence.strip()
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCancelRaceInvalid",
                                "message": "async cancel race journal entry requires ownershipFence",
                                "path": f"$.journal[{entry_index}].{ownership_fence_path}",
                            }
                        )
                    else:
                        fences.add(ownership_fence.strip())
                    sequence = entry.get("sequence")
                    if (
                        isinstance(sequence, bool)
                        or not isinstance(sequence, int)
                        or sequence < 0
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCancelRaceInvalid",
                                "message": "async cancel race journal entry requires integer sequence",
                                "path": f"$.journal[{entry_index}].sequence",
                            }
                        )
                    elif sequence == 0:
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCancelRaceInvalid",
                                "message": "async cancel race journal entry requires positive integer sequence",
                                "path": f"$.journal[{entry_index}].sequence",
                            }
                        )
                        journal_sequences[id(entry)] = sequence
                    else:
                        journal_sequences[id(entry)] = sequence
                if len(fences) > 1:
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race journal entries require stable ownershipFence",
                            "path": "$.journal",
                        }
                    )
                if str(raw_race.get("winner", "")) != "cancel":
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race requires cancel winner",
                            "path": "$.race.winner",
                        }
                    )
                cancel_sequence = min(
                    (
                        journal_sequences[id(entry)]
                        for entry in cancel_entries
                        if id(entry) in journal_sequences
                    ),
                    default=0,
                )
                callback_sequence = min(
                    (
                        journal_sequences[id(entry)]
                        for entry in callback_entries
                        if id(entry) in journal_sequences
                    ),
                    default=0,
                )
                if (
                    not diagnostics
                    and str(raw_race.get("winner", "")) == "cancel"
                    and not has_cancel_entry
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race requires cancel journal entry",
                            "path": "$.journal",
                        }
                    )
                if (
                    not diagnostics
                    and raw_race.get(
                        "callbackReceiptRecorded",
                        raw_race.get("callback_receipt_recorded"),
                    )
                    is True
                    and not has_callback_entry
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race requires callback journal entry",
                            "path": "$.journal",
                        }
                    )
                if (
                    str(raw_race.get("winner", "")) == "cancel"
                    and cancel_sequence > 0
                    and callback_sequence > 0
                    and callback_sequence <= cancel_sequence
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race requires callback journal sequence after cancel sequence",
                            "path": "$.journal",
                        }
                    )
                cancel_race_boolean_values = {}
                for key, alias, default in (
                    ("callbackReceiptRecorded", "callback_receipt_recorded", False),
                    ("resumeAttempted", "resume_attempted", True),
                    ("resultCommitted", "result_committed", True),
                    ("usageReconciled", "usage_reconciled", False),
                ):
                    raw_value_missing = False
                    if key in raw_race:
                        raw_value = raw_race[key]
                        path_key = key
                    elif alias in raw_race:
                        raw_value = raw_race[alias]
                        path_key = alias
                    else:
                        raw_value = default
                        path_key = key
                        raw_value_missing = True
                    cancel_race_boolean_values[key] = (
                        raw_value if isinstance(raw_value, bool) else default
                    )
                    if raw_value_missing or not isinstance(raw_value, bool):
                        diagnostics.append(
                            {
                                "code": "DurableAsyncCancelRaceInvalid",
                                "message": f"async cancel race requires boolean {key}",
                                "path": f"$.race.{path_key}",
                            }
                        )
                if (
                    str(raw_race.get("winner", "")) == "cancel"
                    and raw_race.get(
                        "resumeAttempted", raw_race.get("resume_attempted")
                    )
                    is True
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race forbids resume after cancel winner",
                            "path": "$.race.resumeAttempted",
                        }
                    )
                if (
                    str(raw_race.get("winner", "")) == "cancel"
                    and raw_race.get(
                        "resultCommitted", raw_race.get("result_committed")
                    )
                    is True
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race forbids result commit after cancel winner",
                            "path": "$.race.resultCommitted",
                        }
                    )
                if (
                    str(raw_race.get("winner", "")) == "cancel"
                    and raw_race.get(
                        "usageReconciled", raw_race.get("usage_reconciled")
                    )
                    is False
                ):
                    diagnostics.append(
                        {
                            "code": "DurableAsyncCancelRaceInvalid",
                            "message": "async cancel race requires late usage reconciliation",
                            "path": "$.race.usageReconciled",
                        }
                    )
                observed = {
                    "journalOrderingDecidesRace": (
                        str(raw_race.get("winner", "")) == "cancel"
                        and cancel_sequence > 0
                        and callback_sequence > cancel_sequence
                    ),
                    "callbackReceiptRecorded": cancel_race_boolean_values[
                        "callbackReceiptRecorded"
                    ]
                    and bool(callback_entries),
                    "cancelWinsBlocksResume": (
                        str(raw_race.get("winner", "")) == "cancel"
                        and not cancel_race_boolean_values["resumeAttempted"]
                    ),
                    "lateCallbackCommitsResult": cancel_race_boolean_values[
                        "resultCommitted"
                    ],
                    "lateUsageReconciled": cancel_race_boolean_values[
                        "usageReconciled"
                    ],
                    "ownershipFenceStable": len(fences) == 1 and "" not in fences,
                }
            elif kind == "external_operation_reconciliation":
                raw_operation = fixture.get("operation", {})
                raw_late_callback = fixture.get(
                    "lateCallback", fixture.get("late_callback", {})
                )
                raw_usage = fixture.get("usage", {})
                if (
                    not isinstance(raw_operation, Mapping)
                    or not isinstance(raw_late_callback, Mapping)
                    or not isinstance(raw_usage, Mapping)
                ):
                    raise ValueError(
                        "durable external_operation_reconciliation case requires operation, lateCallback, and usage"
                    )
                operation_id_path = (
                    "operationId"
                    if "operationId" in raw_operation
                    or "operation_id" not in raw_operation
                    else "operation_id"
                )
                operation_id = raw_operation.get(
                    "operationId", raw_operation.get("operation_id")
                )
                if not isinstance(operation_id, str) or not operation_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank operationId",
                            "path": f"$.operation.{operation_id_path}",
                        }
                    )
                elif operation_id != operation_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation operationId must not contain surrounding whitespace",
                            "path": f"$.operation.{operation_id_path}",
                        }
                    )
                provider_operation_id_path = (
                    "providerOperationId"
                    if "providerOperationId" in raw_operation
                    or "provider_operation_id" not in raw_operation
                    else "provider_operation_id"
                )
                provider_operation_id = raw_operation.get(
                    "providerOperationId", raw_operation.get("provider_operation_id")
                )
                if (
                    not isinstance(provider_operation_id, str)
                    or not provider_operation_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank providerOperationId",
                            "path": f"$.operation.{provider_operation_id_path}",
                        }
                    )
                elif provider_operation_id != provider_operation_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation providerOperationId must not contain surrounding whitespace",
                            "path": f"$.operation.{provider_operation_id_path}",
                        }
                    )
                operation_idempotency_key_path = (
                    "idempotencyKey"
                    if "idempotencyKey" in raw_operation
                    or "idempotency_key" not in raw_operation
                    else "idempotency_key"
                )
                operation_idempotency_key = raw_operation.get(
                    "idempotencyKey", raw_operation.get("idempotency_key")
                )
                if (
                    not isinstance(operation_idempotency_key, str)
                    or not operation_idempotency_key.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank operation idempotencyKey",
                            "path": f"$.operation.{operation_idempotency_key_path}",
                        }
                    )
                elif operation_idempotency_key != operation_idempotency_key.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation operation idempotencyKey must not contain surrounding whitespace",
                            "path": f"$.operation.{operation_idempotency_key_path}",
                        }
                    )
                resume_token_hash_path = (
                    "resumeTokenHash"
                    if "resumeTokenHash" in raw_operation
                    or "resume_token_hash" not in raw_operation
                    else "resume_token_hash"
                )
                resume_token_hash = raw_operation.get(
                    "resumeTokenHash", raw_operation.get("resume_token_hash")
                )
                if (
                    not isinstance(resume_token_hash, str)
                    or not resume_token_hash.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires resumeTokenHash sha256 digest",
                            "path": f"$.operation.{resume_token_hash_path}",
                        }
                    )
                elif resume_token_hash != resume_token_hash.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation resumeTokenHash must not contain surrounding whitespace",
                            "path": f"$.operation.{resume_token_hash_path}",
                        }
                    )
                elif (
                    not resume_token_hash.startswith("sha256:")
                    or len(resume_token_hash.removeprefix("sha256:")) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in resume_token_hash.removeprefix("sha256:")
                    )
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires resumeTokenHash sha256 digest",
                            "path": f"$.operation.{resume_token_hash_path}",
                        }
                    )
                expected_schema_path = (
                    "expectedSchema"
                    if "expectedSchema" in raw_operation
                    or "expected_schema" not in raw_operation
                    else "expected_schema"
                )
                expected_schema = raw_operation.get(
                    "expectedSchema", raw_operation.get("expected_schema")
                )
                if not isinstance(expected_schema, str) or not expected_schema.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank expectedSchema",
                            "path": f"$.operation.{expected_schema_path}",
                        }
                    )
                elif expected_schema != expected_schema.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation expectedSchema must not contain surrounding whitespace",
                            "path": f"$.operation.{expected_schema_path}",
                        }
                    )
                operation_kind = raw_operation.get("kind")
                if operation_kind not in {
                    "tool",
                    "sandbox_task",
                    "ci_job",
                    "browser_task",
                    "workspace_trial",
                    "external_provider_job",
                    "document_job",
                    "research_task",
                    "custom",
                }:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires valid operation kind",
                            "path": "$.operation.kind",
                        }
                    )
                created_at_path = (
                    "createdAt"
                    if "createdAt" in raw_operation or "created_at" not in raw_operation
                    else "created_at"
                )
                created_at = raw_operation.get(
                    "createdAt", raw_operation.get("created_at")
                )
                created_at_value = None
                if not isinstance(created_at, str) or not created_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires ISO createdAt",
                            "path": f"$.operation.{created_at_path}",
                        }
                    )
                elif created_at != created_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation createdAt must not contain surrounding whitespace",
                            "path": f"$.operation.{created_at_path}",
                        }
                    )
                else:
                    created_at_text = created_at
                    if len(created_at_text) <= 10 or created_at_text[10] != "T":
                        diagnostics.append(
                            {
                                "code": "DurableExternalOperationInvalid",
                                "message": "external operation reconciliation requires ISO createdAt",
                                "path": f"$.operation.{created_at_path}",
                            }
                        )
                    else:
                        suffix = created_at_text[19:]
                        suffix_valid = False
                        if suffix.startswith("."):
                            offset_start = min(
                                (
                                    position
                                    for position in (
                                        suffix.find("Z"),
                                        suffix.find("+"),
                                        suffix.find("-"),
                                    )
                                    if position >= 0
                                ),
                                default=-1,
                            )
                            if offset_start > 1 and suffix[1:offset_start].isdigit():
                                suffix = suffix[offset_start:]
                        if suffix == "Z":
                            suffix_valid = True
                        elif (
                            len(suffix) == 6
                            and suffix[0] in "+-"
                            and suffix[1:3].isdigit()
                            and suffix[3] == ":"
                            and suffix[4:6].isdigit()
                            and 0 <= int(suffix[1:3]) <= 23
                            and 0 <= int(suffix[4:6]) <= 59
                        ):
                            suffix_valid = True
                        if not suffix_valid:
                            diagnostics.append(
                                {
                                    "code": "DurableExternalOperationInvalid",
                                    "message": "external operation reconciliation requires ISO createdAt",
                                    "path": f"$.operation.{created_at_path}",
                                }
                            )
                        else:
                            try:
                                created_at_value = datetime.fromisoformat(
                                    created_at_text.replace("Z", "+00:00")
                                    if created_at_text.endswith("Z")
                                    else created_at_text
                                )
                            except ValueError:
                                diagnostics.append(
                                    {
                                        "code": "DurableExternalOperationInvalid",
                                        "message": "external operation reconciliation requires ISO createdAt",
                                        "path": f"$.operation.{created_at_path}",
                                    }
                                )
                submitted_at_path = (
                    "submittedAt"
                    if "submittedAt" in raw_operation
                    or "submitted_at" not in raw_operation
                    else "submitted_at"
                )
                submitted_at = raw_operation.get(
                    "submittedAt", raw_operation.get("submitted_at")
                )
                submitted_at_value = None
                if not isinstance(submitted_at, str) or not submitted_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires ISO submittedAt",
                            "path": f"$.operation.{submitted_at_path}",
                        }
                    )
                elif submitted_at != submitted_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation submittedAt must not contain surrounding whitespace",
                            "path": f"$.operation.{submitted_at_path}",
                        }
                    )
                else:
                    submitted_at_text = submitted_at
                    if len(submitted_at_text) <= 10 or submitted_at_text[10] != "T":
                        diagnostics.append(
                            {
                                "code": "DurableExternalOperationInvalid",
                                "message": "external operation reconciliation requires ISO submittedAt",
                                "path": f"$.operation.{submitted_at_path}",
                            }
                        )
                    else:
                        suffix = submitted_at_text[19:]
                        suffix_valid = False
                        if suffix.startswith("."):
                            offset_start = min(
                                (
                                    position
                                    for position in (
                                        suffix.find("Z"),
                                        suffix.find("+"),
                                        suffix.find("-"),
                                    )
                                    if position >= 0
                                ),
                                default=-1,
                            )
                            if offset_start > 1 and suffix[1:offset_start].isdigit():
                                suffix = suffix[offset_start:]
                        if suffix == "Z":
                            suffix_valid = True
                        elif (
                            len(suffix) == 6
                            and suffix[0] in "+-"
                            and suffix[1:3].isdigit()
                            and suffix[3] == ":"
                            and suffix[4:6].isdigit()
                            and 0 <= int(suffix[1:3]) <= 23
                            and 0 <= int(suffix[4:6]) <= 59
                        ):
                            suffix_valid = True
                        if not suffix_valid:
                            diagnostics.append(
                                {
                                    "code": "DurableExternalOperationInvalid",
                                    "message": "external operation reconciliation requires ISO submittedAt",
                                    "path": f"$.operation.{submitted_at_path}",
                                }
                            )
                        else:
                            try:
                                submitted_at_value = datetime.fromisoformat(
                                    submitted_at_text.replace("Z", "+00:00")
                                    if submitted_at_text.endswith("Z")
                                    else submitted_at_text
                                )
                            except ValueError:
                                diagnostics.append(
                                    {
                                        "code": "DurableExternalOperationInvalid",
                                        "message": "external operation reconciliation requires ISO submittedAt",
                                        "path": f"$.operation.{submitted_at_path}",
                                    }
                                )
                if (
                    created_at_value is not None
                    and submitted_at_value is not None
                    and submitted_at_value < created_at_value
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation submittedAt must not precede createdAt",
                            "path": f"$.operation.{submitted_at_path}",
                        }
                    )
                expires_at_path = (
                    "expiresAt"
                    if "expiresAt" in raw_operation or "expires_at" not in raw_operation
                    else "expires_at"
                )
                expires_at = raw_operation.get(
                    "expiresAt", raw_operation.get("expires_at")
                )
                expires_at_value = None
                if not isinstance(expires_at, str) or not expires_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires ISO expiresAt",
                            "path": f"$.operation.{expires_at_path}",
                        }
                    )
                elif expires_at != expires_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation expiresAt must not contain surrounding whitespace",
                            "path": f"$.operation.{expires_at_path}",
                        }
                    )
                else:
                    expires_at_text = expires_at
                    if len(expires_at_text) <= 10 or expires_at_text[10] != "T":
                        diagnostics.append(
                            {
                                "code": "DurableExternalOperationInvalid",
                                "message": "external operation reconciliation requires ISO expiresAt",
                                "path": f"$.operation.{expires_at_path}",
                            }
                        )
                    else:
                        suffix = expires_at_text[19:]
                        suffix_valid = False
                        if suffix.startswith("."):
                            offset_start = min(
                                (
                                    position
                                    for position in (
                                        suffix.find("Z"),
                                        suffix.find("+"),
                                        suffix.find("-"),
                                    )
                                    if position >= 0
                                ),
                                default=-1,
                            )
                            if offset_start > 1 and suffix[1:offset_start].isdigit():
                                suffix = suffix[offset_start:]
                        if suffix == "Z":
                            suffix_valid = True
                        elif (
                            len(suffix) == 6
                            and suffix[0] in "+-"
                            and suffix[1:3].isdigit()
                            and suffix[3] == ":"
                            and suffix[4:6].isdigit()
                            and 0 <= int(suffix[1:3]) <= 23
                            and 0 <= int(suffix[4:6]) <= 59
                        ):
                            suffix_valid = True
                        if not suffix_valid:
                            diagnostics.append(
                                {
                                    "code": "DurableExternalOperationInvalid",
                                    "message": "external operation reconciliation requires ISO expiresAt",
                                    "path": f"$.operation.{expires_at_path}",
                                }
                            )
                        else:
                            try:
                                expires_at_value = datetime.fromisoformat(
                                    expires_at_text.replace("Z", "+00:00")
                                    if expires_at_text.endswith("Z")
                                    else expires_at_text
                                )
                            except ValueError:
                                diagnostics.append(
                                    {
                                        "code": "DurableExternalOperationInvalid",
                                        "message": "external operation reconciliation requires ISO expiresAt",
                                        "path": f"$.operation.{expires_at_path}",
                                    }
                                )
                if (
                    submitted_at_value is not None
                    and expires_at_value is not None
                    and expires_at_value <= submitted_at_value
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation expiresAt must be after submittedAt",
                            "path": f"$.operation.{expires_at_path}",
                        }
                    )
                operation_state = raw_operation.get("state")
                if operation_state not in {
                    "created",
                    "submitted",
                    "waiting_callback",
                    "callback_received",
                    "polling",
                    "resuming",
                    "completed",
                    "failed",
                    "cancelled",
                    "expired",
                }:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires valid operation state",
                            "path": "$.operation.state",
                        }
                    )
                elif operation_state in {
                    "created",
                    "submitted",
                    "waiting_callback",
                    "callback_received",
                    "polling",
                    "resuming",
                }:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires terminal operation state",
                            "path": "$.operation.state",
                        }
                    )
                run_id_path = (
                    "runId"
                    if "runId" in raw_operation or "run_id" not in raw_operation
                    else "run_id"
                )
                run_id = raw_operation.get("runId", raw_operation.get("run_id"))
                if not isinstance(run_id, str) or not run_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank runId",
                            "path": f"$.operation.{run_id_path}",
                        }
                    )
                elif run_id != run_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation runId must not contain surrounding whitespace",
                            "path": f"$.operation.{run_id_path}",
                        }
                    )
                node_id_path = (
                    "nodeId"
                    if "nodeId" in raw_operation or "node_id" not in raw_operation
                    else "node_id"
                )
                node_id = raw_operation.get("nodeId", raw_operation.get("node_id"))
                if not isinstance(node_id, str) or not node_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank nodeId",
                            "path": f"$.operation.{node_id_path}",
                        }
                    )
                elif node_id != node_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation nodeId must not contain surrounding whitespace",
                            "path": f"$.operation.{node_id_path}",
                        }
                    )
                attempt_id_path = (
                    "attemptId"
                    if "attemptId" in raw_operation or "attempt_id" not in raw_operation
                    else "attempt_id"
                )
                attempt_id = raw_operation.get(
                    "attemptId", raw_operation.get("attempt_id")
                )
                if not isinstance(attempt_id, str) or not attempt_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank attemptId",
                            "path": f"$.operation.{attempt_id_path}",
                        }
                    )
                elif attempt_id != attempt_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation attemptId must not contain surrounding whitespace",
                            "path": f"$.operation.{attempt_id_path}",
                        }
                    )
                release_id_path = (
                    "releaseId"
                    if "releaseId" in raw_operation or "release_id" not in raw_operation
                    else "release_id"
                )
                release_id = raw_operation.get(
                    "releaseId", raw_operation.get("release_id")
                )
                if not isinstance(release_id, str) or not release_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank releaseId",
                            "path": f"$.operation.{release_id_path}",
                        }
                    )
                elif release_id != release_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation releaseId must not contain surrounding whitespace",
                            "path": f"$.operation.{release_id_path}",
                        }
                    )
                tenant_id_path = (
                    "tenantId"
                    if "tenantId" in raw_operation or "tenant_id" not in raw_operation
                    else "tenant_id"
                )
                tenant_id = raw_operation.get(
                    "tenantId", raw_operation.get("tenant_id")
                )
                if not isinstance(tenant_id, str) or not tenant_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank tenantId",
                            "path": f"$.operation.{tenant_id_path}",
                        }
                    )
                elif tenant_id != tenant_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation tenantId must not contain surrounding whitespace",
                            "path": f"$.operation.{tenant_id_path}",
                        }
                    )
                operation_policy_snapshot_path = (
                    "policySnapshotId"
                    if "policySnapshotId" in raw_operation
                    or "policy_snapshot_id" not in raw_operation
                    else "policy_snapshot_id"
                )
                operation_policy_snapshot_id = raw_operation.get(
                    "policySnapshotId", raw_operation.get("policy_snapshot_id")
                )
                if (
                    not isinstance(operation_policy_snapshot_id, str)
                    or not operation_policy_snapshot_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank operation policySnapshotId",
                            "path": f"$.operation.{operation_policy_snapshot_path}",
                        }
                    )
                elif (
                    operation_policy_snapshot_id != operation_policy_snapshot_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation operation policySnapshotId must not contain surrounding whitespace",
                            "path": f"$.operation.{operation_policy_snapshot_path}",
                        }
                    )
                callback_id_path = (
                    "callbackId"
                    if "callbackId" in raw_late_callback
                    or "callback_id" not in raw_late_callback
                    else "callback_id"
                )
                callback_id = raw_late_callback.get(
                    "callbackId", raw_late_callback.get("callback_id")
                )
                if not isinstance(callback_id, str) or not callback_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank callbackId",
                            "path": f"$.lateCallback.{callback_id_path}",
                        }
                    )
                elif callback_id != callback_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callbackId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_id_path}",
                        }
                    )
                callback_operation_id_path = (
                    "operationId"
                    if "operationId" in raw_late_callback
                    or "operation_id" not in raw_late_callback
                    else "operation_id"
                )
                callback_operation_id = raw_late_callback.get(
                    "operationId", raw_late_callback.get("operation_id")
                )
                if (
                    not isinstance(callback_operation_id, str)
                    or not callback_operation_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback operationId",
                            "path": f"$.lateCallback.{callback_operation_id_path}",
                        }
                    )
                elif callback_operation_id != callback_operation_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback operationId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_operation_id_path}",
                        }
                    )
                elif (
                    isinstance(operation_id, str)
                    and operation_id.strip()
                    and operation_id == operation_id.strip()
                    and callback_operation_id != operation_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback operationId must match operation",
                            "path": f"$.lateCallback.{callback_operation_id_path}",
                        }
                    )
                callback_provider_operation_id_path = (
                    "providerOperationId"
                    if "providerOperationId" in raw_late_callback
                    or "provider_operation_id" not in raw_late_callback
                    else "provider_operation_id"
                )
                callback_provider_operation_id = raw_late_callback.get(
                    "providerOperationId",
                    raw_late_callback.get("provider_operation_id"),
                )
                if (
                    not isinstance(callback_provider_operation_id, str)
                    or not callback_provider_operation_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback providerOperationId",
                            "path": f"$.lateCallback.{callback_provider_operation_id_path}",
                        }
                    )
                elif (
                    callback_provider_operation_id
                    != callback_provider_operation_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback providerOperationId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_provider_operation_id_path}",
                        }
                    )
                elif (
                    isinstance(provider_operation_id, str)
                    and provider_operation_id.strip()
                    and provider_operation_id == provider_operation_id.strip()
                    and callback_provider_operation_id != provider_operation_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback providerOperationId must match operation",
                            "path": f"$.lateCallback.{callback_provider_operation_id_path}",
                        }
                    )
                callback_run_id_path = (
                    "runId"
                    if "runId" in raw_late_callback or "run_id" not in raw_late_callback
                    else "run_id"
                )
                callback_run_id = raw_late_callback.get(
                    "runId", raw_late_callback.get("run_id")
                )
                if not isinstance(callback_run_id, str) or not callback_run_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback runId",
                            "path": f"$.lateCallback.{callback_run_id_path}",
                        }
                    )
                elif callback_run_id != callback_run_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback runId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_run_id_path}",
                        }
                    )
                elif (
                    isinstance(run_id, str)
                    and run_id.strip()
                    and run_id == run_id.strip()
                    and callback_run_id != run_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback runId must match operation",
                            "path": f"$.lateCallback.{callback_run_id_path}",
                        }
                    )
                callback_node_id_path = (
                    "nodeId"
                    if "nodeId" in raw_late_callback
                    or "node_id" not in raw_late_callback
                    else "node_id"
                )
                callback_node_id = raw_late_callback.get(
                    "nodeId", raw_late_callback.get("node_id")
                )
                if (
                    not isinstance(callback_node_id, str)
                    or not callback_node_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback nodeId",
                            "path": f"$.lateCallback.{callback_node_id_path}",
                        }
                    )
                elif callback_node_id != callback_node_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback nodeId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_node_id_path}",
                        }
                    )
                elif (
                    isinstance(node_id, str)
                    and node_id.strip()
                    and node_id == node_id.strip()
                    and callback_node_id != node_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback nodeId must match operation",
                            "path": f"$.lateCallback.{callback_node_id_path}",
                        }
                    )
                callback_attempt_id_path = (
                    "attemptId"
                    if "attemptId" in raw_late_callback
                    or "attempt_id" not in raw_late_callback
                    else "attempt_id"
                )
                callback_attempt_id = raw_late_callback.get(
                    "attemptId", raw_late_callback.get("attempt_id")
                )
                if (
                    not isinstance(callback_attempt_id, str)
                    or not callback_attempt_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback attemptId",
                            "path": f"$.lateCallback.{callback_attempt_id_path}",
                        }
                    )
                elif callback_attempt_id != callback_attempt_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback attemptId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_attempt_id_path}",
                        }
                    )
                elif (
                    isinstance(attempt_id, str)
                    and attempt_id.strip()
                    and attempt_id == attempt_id.strip()
                    and callback_attempt_id != attempt_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback attemptId must match operation",
                            "path": f"$.lateCallback.{callback_attempt_id_path}",
                        }
                    )
                callback_release_id_path = (
                    "releaseId"
                    if "releaseId" in raw_late_callback
                    or "release_id" not in raw_late_callback
                    else "release_id"
                )
                callback_release_id = raw_late_callback.get(
                    "releaseId", raw_late_callback.get("release_id")
                )
                if (
                    not isinstance(callback_release_id, str)
                    or not callback_release_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback releaseId",
                            "path": f"$.lateCallback.{callback_release_id_path}",
                        }
                    )
                elif callback_release_id != callback_release_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback releaseId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_release_id_path}",
                        }
                    )
                elif (
                    isinstance(release_id, str)
                    and release_id.strip()
                    and release_id == release_id.strip()
                    and callback_release_id != release_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback releaseId must match operation",
                            "path": f"$.lateCallback.{callback_release_id_path}",
                        }
                    )
                callback_tenant_id_path = (
                    "tenantId"
                    if "tenantId" in raw_late_callback
                    or "tenant_id" not in raw_late_callback
                    else "tenant_id"
                )
                callback_tenant_id = raw_late_callback.get(
                    "tenantId", raw_late_callback.get("tenant_id")
                )
                if (
                    not isinstance(callback_tenant_id, str)
                    or not callback_tenant_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires callback tenantId",
                            "path": f"$.lateCallback.{callback_tenant_id_path}",
                        }
                    )
                elif callback_tenant_id != callback_tenant_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback tenantId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{callback_tenant_id_path}",
                        }
                    )
                elif (
                    isinstance(tenant_id, str)
                    and tenant_id.strip()
                    and tenant_id == tenant_id.strip()
                    and callback_tenant_id != tenant_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback tenantId must match operation",
                            "path": f"$.lateCallback.{callback_tenant_id_path}",
                        }
                    )
                payload_digest_path = (
                    "payloadDigest"
                    if "payloadDigest" in raw_late_callback
                    or "payload_digest" not in raw_late_callback
                    else "payload_digest"
                )
                payload_digest = raw_late_callback.get(
                    "payloadDigest", raw_late_callback.get("payload_digest")
                )
                if not isinstance(payload_digest, str) or not payload_digest.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires payloadDigest sha256 digest",
                            "path": f"$.lateCallback.{payload_digest_path}",
                        }
                    )
                elif payload_digest != payload_digest.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation payloadDigest must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{payload_digest_path}",
                        }
                    )
                elif (
                    not payload_digest.startswith("sha256:")
                    or len(payload_digest.removeprefix("sha256:")) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in payload_digest.removeprefix("sha256:")
                    )
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires payloadDigest sha256 digest",
                            "path": f"$.lateCallback.{payload_digest_path}",
                        }
                    )
                callback_status = raw_late_callback.get("status")
                if not isinstance(callback_status, str) or not callback_status.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires terminal callback status",
                            "path": "$.lateCallback.status",
                        }
                    )
                elif callback_status != callback_status.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation status must not contain surrounding whitespace",
                            "path": "$.lateCallback.status",
                        }
                    )
                elif callback_status not in (
                    "completed",
                    "failed",
                    "cancelled",
                    "expired",
                    "incomplete",
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires terminal callback status",
                            "path": "$.lateCallback.status",
                        }
                    )
                verified_by_path = (
                    "verifiedBy"
                    if "verifiedBy" in raw_late_callback
                    or "verified_by" not in raw_late_callback
                    else "verified_by"
                )
                verified_by = raw_late_callback.get(
                    "verifiedBy", raw_late_callback.get("verified_by")
                )
                if not isinstance(verified_by, str) or not verified_by.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank verifiedBy",
                            "path": f"$.lateCallback.{verified_by_path}",
                        }
                    )
                elif verified_by != verified_by.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation verifiedBy must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{verified_by_path}",
                        }
                    )
                elif verified_by.strip().lower() == "unauthenticated":
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires authenticated verifiedBy",
                            "path": f"$.lateCallback.{verified_by_path}",
                        }
                    )
                idempotency_key_path = (
                    "idempotencyKey"
                    if "idempotencyKey" in raw_late_callback
                    or "idempotency_key" not in raw_late_callback
                    else "idempotency_key"
                )
                idempotency_key = raw_late_callback.get(
                    "idempotencyKey", raw_late_callback.get("idempotency_key")
                )
                if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank idempotencyKey",
                            "path": f"$.lateCallback.{idempotency_key_path}",
                        }
                    )
                elif idempotency_key != idempotency_key.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation idempotencyKey must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{idempotency_key_path}",
                        }
                    )
                policy_snapshot_path = (
                    "policySnapshotId"
                    if "policySnapshotId" in raw_late_callback
                    or "policy_snapshot_id" not in raw_late_callback
                    else "policy_snapshot_id"
                )
                policy_snapshot_id = raw_late_callback.get(
                    "policySnapshotId", raw_late_callback.get("policy_snapshot_id")
                )
                if (
                    not isinstance(policy_snapshot_id, str)
                    or not policy_snapshot_id.strip()
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires nonblank policySnapshotId",
                            "path": f"$.lateCallback.{policy_snapshot_path}",
                        }
                    )
                elif policy_snapshot_id != policy_snapshot_id.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation policySnapshotId must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{policy_snapshot_path}",
                        }
                    )
                elif (
                    isinstance(operation_policy_snapshot_id, str)
                    and operation_policy_snapshot_id.strip()
                    and operation_policy_snapshot_id
                    == operation_policy_snapshot_id.strip()
                    and policy_snapshot_id != operation_policy_snapshot_id
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation callback policySnapshotId must match operation",
                            "path": f"$.lateCallback.{policy_snapshot_path}",
                        }
                    )
                received_at_path = (
                    "receivedAt"
                    if "receivedAt" in raw_late_callback
                    or "received_at" not in raw_late_callback
                    else "received_at"
                )
                received_at = raw_late_callback.get(
                    "receivedAt", raw_late_callback.get("received_at")
                )
                received_at_value = None
                if not isinstance(received_at, str) or not received_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires ISO receivedAt",
                            "path": f"$.lateCallback.{received_at_path}",
                        }
                    )
                elif received_at != received_at.strip():
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation receivedAt must not contain surrounding whitespace",
                            "path": f"$.lateCallback.{received_at_path}",
                        }
                    )
                else:
                    received_at_text = received_at
                    if len(received_at_text) <= 10 or received_at_text[10] != "T":
                        diagnostics.append(
                            {
                                "code": "DurableExternalOperationInvalid",
                                "message": "external operation reconciliation requires ISO receivedAt",
                                "path": f"$.lateCallback.{received_at_path}",
                            }
                        )
                    else:
                        suffix = received_at_text[19:]
                        suffix_valid = False
                        if suffix.startswith("."):
                            offset_start = min(
                                (
                                    position
                                    for position in (
                                        suffix.find("Z"),
                                        suffix.find("+"),
                                        suffix.find("-"),
                                    )
                                    if position >= 0
                                ),
                                default=-1,
                            )
                            if offset_start > 1 and suffix[1:offset_start].isdigit():
                                suffix = suffix[offset_start:]
                        if suffix == "Z":
                            suffix_valid = True
                        elif (
                            len(suffix) == 6
                            and suffix[0] in "+-"
                            and suffix[1:3].isdigit()
                            and suffix[3] == ":"
                            and suffix[4:6].isdigit()
                            and 0 <= int(suffix[1:3]) <= 23
                            and 0 <= int(suffix[4:6]) <= 59
                        ):
                            suffix_valid = True
                        if not suffix_valid:
                            diagnostics.append(
                                {
                                    "code": "DurableExternalOperationInvalid",
                                    "message": "external operation reconciliation requires ISO receivedAt",
                                    "path": f"$.lateCallback.{received_at_path}",
                                }
                            )
                        else:
                            try:
                                received_at_value = datetime.fromisoformat(
                                    received_at_text.replace("Z", "+00:00")
                                    if received_at_text.endswith("Z")
                                    else received_at_text
                                )
                            except ValueError:
                                diagnostics.append(
                                    {
                                        "code": "DurableExternalOperationInvalid",
                                        "message": "external operation reconciliation requires ISO receivedAt",
                                        "path": f"$.lateCallback.{received_at_path}",
                                    }
                                )
                if (
                    submitted_at_value is not None
                    and received_at_value is not None
                    and received_at_value < submitted_at_value
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation receivedAt must not precede operation submittedAt",
                            "path": f"$.lateCallback.{received_at_path}",
                        }
                    )
                if (
                    submitted_at_value is not None
                    and expires_at_value is not None
                    and received_at_value is not None
                    and expires_at_value > submitted_at_value
                    and received_at_value >= expires_at_value
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation receivedAt must not exceed operation expiresAt",
                            "path": f"$.lateCallback.{received_at_path}",
                        }
                    )
                effect_state_path = (
                    "effectState"
                    if "effectState" in raw_operation
                    or "effect_state" not in raw_operation
                    else "effect_state"
                )
                if (
                    raw_operation.get("effectState", raw_operation.get("effect_state"))
                    != "committed"
                ):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires committed effectState",
                            "path": f"$.operation.{effect_state_path}",
                        }
                    )
                effect_journaled_path = (
                    "effectJournaled"
                    if "effectJournaled" in raw_operation
                    or "effect_journaled" not in raw_operation
                    else "effect_journaled"
                )
                raw_effect_journaled = raw_operation.get(
                    "effectJournaled", raw_operation.get("effect_journaled")
                )
                if not isinstance(raw_effect_journaled, bool):
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires boolean effectJournaled",
                            "path": f"$.operation.{effect_journaled_path}",
                        }
                    )
                elif raw_effect_journaled is False:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires committed effect journal record",
                            "path": f"$.operation.{effect_journaled_path}",
                        }
                    )
                external_reconciliation_values = {}
                for source_name, source, key, alias, default in (
                    (
                        "lateCallback",
                        raw_late_callback,
                        "commitsResult",
                        "commits_result",
                        True,
                    ),
                    (
                        "lateCallback",
                        raw_late_callback,
                        "diagnosticRecorded",
                        "diagnostic_recorded",
                        False,
                    ),
                    (
                        "lateCallback",
                        raw_late_callback,
                        "payloadConvertedToArtifactRef",
                        "payload_converted_to_artifact_ref",
                        False,
                    ),
                    ("usage", raw_usage, "reconciled", "reconciled", False),
                ):
                    raw_value_missing = False
                    if key in source:
                        raw_value = source[key]
                        path_key = key
                    elif alias in source:
                        raw_value = source[alias]
                        path_key = alias
                    else:
                        raw_value = default
                        path_key = key
                        raw_value_missing = True
                    external_reconciliation_values[(source_name, key)] = (
                        raw_value if isinstance(raw_value, bool) else default
                    )
                    if raw_value_missing or not isinstance(raw_value, bool):
                        diagnostics.append(
                            {
                                "code": "DurableExternalOperationInvalid",
                                "message": f"external operation reconciliation requires boolean {key}",
                                "path": f"$.{source_name}.{path_key}",
                            }
                        )
                commits_result_path = (
                    "commitsResult"
                    if "commitsResult" in raw_late_callback
                    or "commits_result" not in raw_late_callback
                    else "commits_result"
                )
                commits_result = raw_late_callback.get(
                    "commitsResult", raw_late_callback.get("commits_result")
                )
                if commits_result is True:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation late callback must not commit result",
                            "path": f"$.lateCallback.{commits_result_path}",
                        }
                    )
                diagnostic_recorded_path = (
                    "diagnosticRecorded"
                    if "diagnosticRecorded" in raw_late_callback
                    or "diagnostic_recorded" not in raw_late_callback
                    else "diagnostic_recorded"
                )
                diagnostic_recorded = raw_late_callback.get(
                    "diagnosticRecorded", raw_late_callback.get("diagnostic_recorded")
                )
                if diagnostic_recorded is False:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires recorded late-callback diagnostic",
                            "path": f"$.lateCallback.{diagnostic_recorded_path}",
                        }
                    )
                payload_artifact_path = (
                    "payloadConvertedToArtifactRef"
                    if "payloadConvertedToArtifactRef" in raw_late_callback
                    or "payload_converted_to_artifact_ref" not in raw_late_callback
                    else "payload_converted_to_artifact_ref"
                )
                payload_converted_to_artifact_ref = raw_late_callback.get(
                    "payloadConvertedToArtifactRef",
                    raw_late_callback.get("payload_converted_to_artifact_ref"),
                )
                if payload_converted_to_artifact_ref is False:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires artifact-backed callback payload",
                            "path": f"$.lateCallback.{payload_artifact_path}",
                        }
                    )
                if raw_usage.get("reconciled") is False:
                    diagnostics.append(
                        {
                            "code": "DurableExternalOperationInvalid",
                            "message": "external operation reconciliation requires late usage reconciliation",
                            "path": "$.usage.reconciled",
                        }
                    )
                raw_provider_usage_records = raw_usage.get(
                    "providerUsageRecords", raw_usage.get("provider_usage_records", ())
                )
                if external_reconciliation_values[("usage", "reconciled")]:
                    if (
                        not isinstance(raw_provider_usage_records, Sequence)
                        or isinstance(raw_provider_usage_records, (str, bytes))
                        or not raw_provider_usage_records
                    ):
                        diagnostics.append(
                            {
                                "code": "DurableExternalOperationInvalid",
                                "message": "external operation reconciliation requires providerUsageRecords when reconciled",
                                "path": "$.usage.providerUsageRecords",
                            }
                        )
                    else:
                        for usage_index, usage_record in enumerate(
                            raw_provider_usage_records
                        ):
                            if not isinstance(usage_record, Mapping):
                                diagnostics.append(
                                    {
                                        "code": "DurableExternalOperationInvalid",
                                        "message": "external operation reconciliation usage record must be object",
                                        "path": f"$.usage.providerUsageRecords[{usage_index}]",
                                    }
                                )
                            else:
                                metric = usage_record.get("metric")
                                if not isinstance(metric, str) or not metric.strip():
                                    diagnostics.append(
                                        {
                                            "code": "DurableExternalOperationInvalid",
                                            "message": "external operation reconciliation usage record requires string metric",
                                            "path": f"$.usage.providerUsageRecords[{usage_index}].metric",
                                        }
                                    )
                                amount = usage_record.get("amount")
                                if isinstance(amount, bool) or not isinstance(
                                    amount, (int, float)
                                ):
                                    diagnostics.append(
                                        {
                                            "code": "DurableExternalOperationInvalid",
                                            "message": "external operation reconciliation usage record requires numeric amount",
                                            "path": f"$.usage.providerUsageRecords[{usage_index}].amount",
                                        }
                                    )
                                elif not isinstance(amount, int):
                                    diagnostics.append(
                                        {
                                            "code": "DurableExternalOperationInvalid",
                                            "message": "external operation reconciliation usage record requires integer amount",
                                            "path": f"$.usage.providerUsageRecords[{usage_index}].amount",
                                        }
                                    )
                                elif amount < 0:
                                    diagnostics.append(
                                        {
                                            "code": "DurableExternalOperationInvalid",
                                            "message": "external operation reconciliation usage record amount must be non-negative",
                                            "path": f"$.usage.providerUsageRecords[{usage_index}].amount",
                                        }
                                    )
                observed = {
                    "sideEffectCommitPreserved": str(
                        raw_operation.get(
                            "effectState", raw_operation.get("effect_state", "")
                        )
                    )
                    == "committed"
                    and raw_effect_journaled is True,
                    "lateCallbackCommitsResult": external_reconciliation_values[
                        ("lateCallback", "commitsResult")
                    ],
                    "lateCallbackRecordedDiagnostic": external_reconciliation_values[
                        ("lateCallback", "diagnosticRecorded")
                    ],
                    "lateUsageReconciled": external_reconciliation_values[
                        ("usage", "reconciled")
                    ],
                    "largePayloadUsesArtifactRef": external_reconciliation_values[
                        ("lateCallback", "payloadConvertedToArtifactRef")
                    ],
                    "diagnosticCount": len(diagnostics),
                }
            else:
                diagnostics.append(
                    {
                        "code": "DurableKindUnknown",
                        "message": f"durable TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "DurableExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        if expected_diagnostics is not None:
            actual_diagnostics = tuple(dict(diagnostic) for diagnostic in diagnostics)
            diagnostics_match = actual_diagnostics == expected_diagnostics
            observed["expectedDiagnosticsMatched"] = diagnostics_match
            diagnostics = []
            if not diagnostics_match:
                diagnostics.append(
                    {
                        "code": "DurableExpectedDiagnosticsMismatch",
                        "message": "durable diagnostics did not match expected diagnostics",
                        "path": "$.expectedDiagnostics",
                    }
                )

        for key, expected_value in expected.items():
            if str(key) in expected_keys_with_structural_diagnostics:
                continue
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "DurableExpectedMismatch",
                        "message": f"durable observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_orchestration_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.orchestration_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "OrchestrationExpectedInvalid",
                    "message": "orchestration TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        def step_from_mapping(mapping: Mapping[str, object]) -> TaskStep:
            return TaskStep(
                step_id=str(mapping.get("stepId", mapping.get("step_id", ""))),
                description=str(mapping.get("description", "")),
                depends_on=_string_tuple(
                    mapping.get("dependsOn", mapping.get("depends_on", ()))
                ),
                metadata=dict(mapping.get("metadata", {}))
                if isinstance(mapping.get("metadata", {}), Mapping)
                else {},
            )

        def access_from_mapping(mapping: Mapping[str, object]) -> TaskContextAccess:
            return TaskContextAccess(
                step_id=str(mapping.get("stepId", mapping.get("step_id", ""))),
                resource_id=str(
                    mapping.get("resourceId", mapping.get("resource_id", ""))
                ),
                mode=str(mapping.get("mode", "")),
                reason=str(mapping["reason"])
                if mapping.get("reason") is not None
                else None,
            )

        def plan_from_mapping(mapping: Mapping[str, object]) -> TaskPlan:
            raw_steps = mapping.get("steps", [])
            if not isinstance(raw_steps, list):
                raise ValueError("orchestration task plan steps must be a list")
            raw_context_access = mapping.get(
                "contextAccess", mapping.get("context_access", [])
            )
            if not isinstance(raw_context_access, list):
                raise ValueError("orchestration task plan contextAccess must be a list")
            return TaskPlan(
                plan_id=str(mapping.get("planId", mapping.get("plan_id", ""))),
                objective=str(mapping.get("objective", "")),
                steps=tuple(
                    step_from_mapping(step)
                    for step in raw_steps
                    if isinstance(step, Mapping)
                ),
                revision=int(mapping.get("revision", 1)),
                metadata=dict(mapping.get("metadata", {}))
                if isinstance(mapping.get("metadata", {}), Mapping)
                else {},
                context_resources=_string_tuple(
                    mapping.get(
                        "contextResources", mapping.get("context_resources", ())
                    )
                ),
                context_access=tuple(
                    access_from_mapping(access)
                    for access in raw_context_access
                    if isinstance(access, Mapping)
                ),
            )

        def patch_from_mapping(mapping: Mapping[str, object]) -> TaskPlanPatch:
            raw_upserts = mapping.get("upsertSteps", mapping.get("upsert_steps", []))
            if not isinstance(raw_upserts, list):
                raise ValueError(
                    "orchestration task plan patch upsertSteps must be a list"
                )
            return TaskPlanPatch(
                patch_id=str(mapping.get("patchId", mapping.get("patch_id", ""))),
                base_plan_id=str(
                    mapping.get("basePlanId", mapping.get("base_plan_id", ""))
                ),
                base_revision=int(
                    mapping.get("baseRevision", mapping.get("base_revision", 0))
                ),
                upsert_steps=tuple(
                    step_from_mapping(step)
                    for step in raw_upserts
                    if isinstance(step, Mapping)
                ),
                remove_step_ids=_string_tuple(
                    mapping.get("removeStepIds", mapping.get("remove_step_ids", ()))
                ),
                created_at=str(mapping.get("createdAt", mapping.get("created_at", ""))),
                metadata=dict(mapping.get("metadata", {}))
                if isinstance(mapping.get("metadata", {}), Mapping)
                else {},
            )

        def usage_amounts(raw_amounts: object) -> list[UsageAmount]:
            amounts: list[UsageAmount] = []
            if isinstance(raw_amounts, list):
                for raw_amount in raw_amounts:
                    if isinstance(raw_amount, Mapping):
                        amounts.append(
                            UsageAmount(
                                kind=str(raw_amount.get("kind", "")),
                                amount=Decimal(str(raw_amount.get("amount", "0"))),
                                unit=str(raw_amount.get("unit", "")),
                            )
                        )
            return amounts

        def usage_contract(amounts: list[UsageAmount]) -> list[dict[str, str]]:
            return [
                {
                    "kind": amount.kind,
                    "amount": str(amount.amount),
                    "unit": amount.unit,
                }
                for amount in amounts
            ]

        observed: dict[str, object] = {}
        try:
            if kind == "task_plan_patch":
                raw_base = fixture.get("base", {})
                raw_patch = fixture.get("patch", {})
                if not isinstance(raw_base, Mapping) or not isinstance(
                    raw_patch, Mapping
                ):
                    raise ValueError(
                        "orchestration task_plan_patch case requires base and patch"
                    )
                updated = plan_from_mapping(raw_base).apply_patch(
                    patch_from_mapping(raw_patch)
                )
                noop = updated.apply_patch(
                    TaskPlanPatch("noop", updated.plan_id, updated.revision)
                )
                observed = {
                    "revision": updated.revision,
                    "stepIds": [step.step_id for step in updated.steps],
                    "draftDescription": updated.step("draft").description,
                    "noopDigestStable": updated.content_digest()
                    == noop.content_digest(),
                }
            elif kind == "task_plan_errors":
                missing_dependency_error = None
                missing_dependency_step = None
                missing_dependency_id = None
                try:
                    raw_plan = fixture.get(
                        "missingDependencyPlan",
                        fixture.get("missing_dependency_plan", {}),
                    )
                    if not isinstance(raw_plan, Mapping):
                        raise ValueError(
                            "orchestration task_plan_errors case requires missingDependencyPlan"
                        )
                    plan_from_mapping(raw_plan)
                except TaskPlanDependencyError as error:
                    missing_dependency_error = "task_dependency_missing"
                    missing_dependency_step = error.step_id
                    missing_dependency_id = error.dependency_id

                cycle_error = None
                cycle: list[str] = []
                try:
                    raw_base = fixture.get("cycleBase", fixture.get("cycle_base", {}))
                    raw_patch = fixture.get(
                        "cyclePatch", fixture.get("cycle_patch", {})
                    )
                    if not isinstance(raw_base, Mapping) or not isinstance(
                        raw_patch, Mapping
                    ):
                        raise ValueError(
                            "orchestration task_plan_errors case requires cycleBase and cyclePatch"
                        )
                    plan_from_mapping(raw_base).apply_patch(
                        patch_from_mapping(raw_patch)
                    )
                except TaskPlanCycleError as error:
                    cycle_error = "task_cycle"
                    cycle = list(error.cycle)

                observed = {
                    "missingDependencyError": missing_dependency_error,
                    "missingDependencyStep": missing_dependency_step,
                    "missingDependencyId": missing_dependency_id,
                    "cycleError": cycle_error,
                    "cycle": cycle,
                }
            elif kind == "context_access":
                raw_left = fixture.get("left", {})
                raw_right = fixture.get("right", {})
                raw_invalid = fixture.get("invalid", {})
                if (
                    not isinstance(raw_left, Mapping)
                    or not isinstance(raw_right, Mapping)
                    or not isinstance(raw_invalid, Mapping)
                ):
                    raise ValueError(
                        "orchestration context_access case requires left, right, and invalid"
                    )
                left = plan_from_mapping(raw_left)
                right = plan_from_mapping(raw_right)
                invalid_error = None
                try:
                    plan_from_mapping(raw_invalid)
                except TaskPlanContextAccessError as error:
                    invalid_error = f"context_access_{error.reason}"
                observed = {
                    "sameDigest": left.content_digest() == right.content_digest(),
                    "orderedAccess": [
                        f"{access.step_id}:{access.resource_id}:{access.mode}"
                        for access in left.context_access
                    ],
                    "invalidError": invalid_error,
                }
            elif kind == "model_pool":
                raw_pool = fixture.get("pool", {})
                raw_worker = fixture.get("worker", {})
                raw_request = fixture.get("request", {})
                raw_invalid = fixture.get(
                    "invalidRequest", fixture.get("invalid_request", {})
                )
                if not isinstance(raw_pool, Mapping) or not isinstance(
                    raw_worker, Mapping
                ):
                    raise ValueError(
                        "orchestration model_pool case requires pool and worker"
                    )
                raw_models = raw_pool.get("models", [])
                if not isinstance(raw_models, list):
                    raise ValueError("orchestration model_pool models must be a list")
                models = []
                for raw_model in raw_models:
                    if not isinstance(raw_model, Mapping):
                        continue
                    model = ModelProfile(
                        profile_id=str(
                            raw_model.get("profileId", raw_model.get("profile_id", ""))
                        ),
                        connection=str(raw_model.get("connection", "")),
                    )
                    model = model.with_capabilities(
                        _string_tuple(raw_model.get("capabilities"))
                    )
                    model = model.with_allowed_sensitivity(
                        _string_tuple(
                            raw_model.get(
                                "allowedSensitivity",
                                raw_model.get("allowed_sensitivity", ()),
                            )
                        )
                    )
                    model = model.with_regions(_string_tuple(raw_model.get("regions")))
                    if (
                        raw_model.get("qualityTier", raw_model.get("quality_tier"))
                        is not None
                    ):
                        model = model.with_quality_tier(
                            str(
                                raw_model.get(
                                    "qualityTier", raw_model.get("quality_tier")
                                )
                            )
                        )
                    if (
                        raw_model.get("costClass", raw_model.get("cost_class"))
                        is not None
                    ):
                        model = model.with_cost_class(
                            str(raw_model.get("costClass", raw_model.get("cost_class")))
                        )
                    if (
                        raw_model.get("latencyClass", raw_model.get("latency_class"))
                        is not None
                    ):
                        model = model.with_latency_class(
                            str(
                                raw_model.get(
                                    "latencyClass", raw_model.get("latency_class")
                                )
                            )
                        )
                    model = model.with_usage_report(
                        bool(
                            raw_model.get(
                                "supportsUsageReport",
                                raw_model.get("supports_usage_report", False),
                            )
                        )
                    )
                    model = model.with_cancellation(
                        bool(
                            raw_model.get(
                                "supportsCancellation",
                                raw_model.get("supports_cancellation", False),
                            )
                        )
                    )
                    models.append(model)
                pool = ModelPool(
                    pool_id=str(raw_pool.get("poolId", raw_pool.get("pool_id", ""))),
                    selection_policy_ref=str(
                        raw_pool.get(
                            "selectionPolicyRef",
                            raw_pool.get("selection_policy_ref", ""),
                        )
                    ),
                ).with_models(models)
                worker = WorkerProfile(
                    profile_id=str(
                        raw_worker.get("profileId", raw_worker.get("profile_id", ""))
                    ),
                )
                worker = worker.with_required_capabilities(
                    _string_tuple(
                        raw_worker.get(
                            "requiredCapabilities",
                            raw_worker.get("required_capabilities", ()),
                        )
                    )
                )
                worker = worker.with_allowed_tools(
                    _string_tuple(
                        raw_worker.get(
                            "allowedTools", raw_worker.get("allowed_tools", ())
                        )
                    )
                )
                if (
                    raw_worker.get("modelPoolRef", raw_worker.get("model_pool_ref"))
                    is not None
                ):
                    worker = worker.with_model_pool_ref(
                        str(
                            raw_worker.get(
                                "modelPoolRef", raw_worker.get("model_pool_ref")
                            )
                        )
                    )
                if (
                    raw_worker.get(
                        "sensitivityCeiling", raw_worker.get("sensitivity_ceiling")
                    )
                    is not None
                ):
                    worker = worker.with_sensitivity_ceiling(
                        str(
                            raw_worker.get(
                                "sensitivityCeiling",
                                raw_worker.get("sensitivity_ceiling"),
                            )
                        )
                    )
                if (
                    raw_worker.get(
                        "defaultBudgetRef", raw_worker.get("default_budget_ref")
                    )
                    is not None
                ):
                    worker = worker.with_default_budget_ref(
                        str(
                            raw_worker.get(
                                "defaultBudgetRef", raw_worker.get("default_budget_ref")
                            )
                        )
                    )
                if not isinstance(raw_request, Mapping):
                    raw_request = {}
                request = ModelSelectionRequest(worker)
                request = request.with_required_tools(
                    _string_tuple(
                        raw_request.get(
                            "requiredTools", raw_request.get("required_tools", ())
                        )
                    )
                )
                request = request.with_required_capabilities(
                    _string_tuple(
                        raw_request.get(
                            "requiredCapabilities",
                            raw_request.get("required_capabilities", ()),
                        )
                    )
                )
                if raw_request.get("sensitivity") is not None:
                    request = request.with_sensitivity(
                        str(raw_request.get("sensitivity"))
                    )
                if raw_request.get("region") is not None:
                    request = request.with_region(str(raw_request.get("region")))
                selected = pool.select_model(request)
                invalid_error = None
                invalid_tool = None
                try:
                    if not isinstance(raw_invalid, Mapping):
                        raw_invalid = {}
                    invalid_request = ModelSelectionRequest(worker).with_required_tools(
                        _string_tuple(
                            raw_invalid.get(
                                "requiredTools", raw_invalid.get("required_tools", ())
                            )
                        )
                    )
                    pool.select_model(invalid_request)
                except ModelToolNotAllowedError as error:
                    invalid_error = "tool_not_allowed"
                    invalid_tool = error.tool_name
                observed = {
                    "selectedModel": selected.profile_id,
                    "selectedConnection": selected.connection,
                    "supportsUsageReport": selected.supports_usage_report,
                    "supportsCancellation": selected.supports_cancellation,
                    "invalidError": invalid_error,
                    "invalidTool": invalid_tool,
                }
            elif kind == "lease_pool":
                raw_pool = fixture.get("pool", {})
                if not isinstance(raw_pool, Mapping):
                    raise ValueError("orchestration lease_pool case requires pool")
                lease_pool = LeasePool(
                    pool_id=str(raw_pool.get("poolId", raw_pool.get("pool_id", ""))),
                    resource_kind=str(
                        raw_pool.get("resourceKind", raw_pool.get("resource_kind", ""))
                    ),
                    capacity_units=int(
                        raw_pool.get("capacityUnits", raw_pool.get("capacity_units", 0))
                    ),
                )
                raw_requests = fixture.get("requests", [])
                if not isinstance(raw_requests, list) or len(raw_requests) < 2:
                    raise ValueError(
                        "orchestration lease_pool case requires at least two requests"
                    )
                first_request = (
                    raw_requests[0] if isinstance(raw_requests[0], Mapping) else {}
                )
                second_request = (
                    raw_requests[1] if isinstance(raw_requests[1], Mapping) else {}
                )
                lease_pool, first_grant = lease_pool.acquire(
                    LeaseRequest(
                        request_id=str(
                            first_request.get(
                                "requestId", first_request.get("request_id", "")
                            )
                        ),
                        holder=PolicyResourceRef(str(first_request.get("holder", ""))),
                        resource_kind=str(
                            first_request.get(
                                "resourceKind", first_request.get("resource_kind", "")
                            )
                        ),
                        units=int(first_request.get("units", 1)),
                    ),
                    lease_id=str(
                        first_request.get("leaseId", first_request.get("lease_id", ""))
                    ),
                    acquired_at=str(
                        first_request.get(
                            "acquiredAt", first_request.get("acquired_at", "")
                        )
                    ),
                    expires_at=str(
                        first_request.get(
                            "expiresAt", first_request.get("expires_at", "")
                        )
                    ),
                )
                second_error = None
                try:
                    lease_pool.acquire(
                        LeaseRequest(
                            request_id=str(
                                second_request.get(
                                    "requestId", second_request.get("request_id", "")
                                )
                            ),
                            holder=PolicyResourceRef(
                                str(second_request.get("holder", ""))
                            ),
                            resource_kind=str(
                                second_request.get(
                                    "resourceKind",
                                    second_request.get("resource_kind", ""),
                                )
                            ),
                            units=int(second_request.get("units", 1)),
                        ),
                        lease_id=str(
                            second_request.get(
                                "leaseId", second_request.get("lease_id", "")
                            )
                        ),
                        acquired_at=str(
                            second_request.get(
                                "acquiredAt", second_request.get("acquired_at", "")
                            )
                        ),
                        expires_at=str(
                            second_request.get(
                                "expiresAt", second_request.get("expires_at", "")
                            )
                        ),
                    )
                except LeasePoolExhaustedError:
                    second_error = "lease_pool_exhausted"
                raw_release = fixture.get("release", {})
                if not isinstance(raw_release, Mapping):
                    raw_release = {}
                release_error = None
                expected_epoch = None
                actual_epoch = None
                try:
                    lease_pool.release(
                        str(
                            raw_release.get("leaseId", raw_release.get("lease_id", ""))
                        ),
                        fencing_epoch=int(
                            raw_release.get(
                                "fencingEpoch", raw_release.get("fencing_epoch", 0)
                            )
                        ),
                    )
                except LeaseEpochMismatchError as error:
                    release_error = "lease_epoch_mismatch"
                    expected_epoch = error.expected_epoch
                    actual_epoch = error.actual_epoch
                after_expiry = str(
                    fixture.get("afterExpiry", fixture.get("after_expiry", ""))
                )
                reaped_pool = (
                    lease_pool.reap_expired(after_expiry)
                    if after_expiry
                    else lease_pool
                )
                available_after_expiry = reaped_pool.available_units
                raw_post_expiry_request = fixture.get(
                    "postExpiryRequest", fixture.get("post_expiry_request", {})
                )
                post_expiry_grant = None
                if isinstance(raw_post_expiry_request, Mapping):
                    reaped_pool, post_expiry_grant = reaped_pool.acquire(
                        LeaseRequest(
                            request_id=str(
                                raw_post_expiry_request.get(
                                    "requestId",
                                    raw_post_expiry_request.get("request_id", ""),
                                )
                            ),
                            holder=PolicyResourceRef(
                                str(raw_post_expiry_request.get("holder", ""))
                            ),
                            resource_kind=str(
                                raw_post_expiry_request.get(
                                    "resourceKind",
                                    raw_post_expiry_request.get("resource_kind", ""),
                                )
                            ),
                            units=int(raw_post_expiry_request.get("units", 1)),
                        ),
                        lease_id=str(
                            raw_post_expiry_request.get(
                                "leaseId", raw_post_expiry_request.get("lease_id", "")
                            )
                        ),
                        acquired_at=str(
                            raw_post_expiry_request.get(
                                "acquiredAt",
                                raw_post_expiry_request.get("acquired_at", ""),
                            )
                        ),
                        expires_at=str(
                            raw_post_expiry_request.get(
                                "expiresAt",
                                raw_post_expiry_request.get("expires_at", ""),
                            )
                        ),
                    )
                released_pool = reaped_pool
                if post_expiry_grant is not None:
                    released_pool = reaped_pool.release(
                        post_expiry_grant.lease_id,
                        fencing_epoch=post_expiry_grant.fencing_epoch,
                    )
                observed = {
                    "firstLeaseEpoch": first_grant.fencing_epoch,
                    "secondError": second_error,
                    "availableAfterFirst": lease_pool.available_units,
                    "releaseError": release_error,
                    "expectedEpoch": expected_epoch,
                    "actualEpoch": actual_epoch,
                    "availableAfterExpiry": available_after_expiry,
                    "postExpiryLeaseEpoch": post_expiry_grant.fencing_epoch
                    if post_expiry_grant is not None
                    else None,
                    "availableAfterPostExpiryAcquire": reaped_pool.available_units,
                    "availableAfterValidRelease": released_pool.available_units,
                }
            elif kind == "child_budget_delegation":
                raw_parent = fixture.get(
                    "parentPermit", fixture.get("parent_permit", {})
                )
                raw_delegation = fixture.get("delegation", {})
                if not isinstance(raw_parent, Mapping) or not isinstance(
                    raw_delegation, Mapping
                ):
                    raise ValueError(
                        "orchestration child_budget_delegation case requires parentPermit and delegation"
                    )
                parent = BudgetPermit(
                    permit_id=str(
                        raw_parent.get("permitId", raw_parent.get("permit_id", ""))
                    ),
                    reservation_refs=tuple(
                        str(item)
                        for item in raw_parent.get(
                            "reservationRefs", raw_parent.get("reservation_refs", [])
                        )
                    ),
                    owner=PolicyResourceRef(str(raw_parent.get("owner", ""))),
                    atomic_unit=PolicyResourceRef(
                        str(
                            raw_parent.get(
                                "atomicUnit", raw_parent.get("atomic_unit", "")
                            )
                        ),
                    ),
                    admission_epoch=int(
                        raw_parent.get(
                            "admissionEpoch", raw_parent.get("admission_epoch", 0)
                        )
                    ),
                    authorized_amounts=usage_amounts(
                        raw_parent.get(
                            "authorizedAmounts",
                            raw_parent.get("authorized_amounts", []),
                        )
                    ),
                    continuation_profile=(
                        str(
                            raw_parent.get(
                                "continuationProfile",
                                raw_parent.get("continuation_profile"),
                            )
                        )
                        if raw_parent.get(
                            "continuationProfile",
                            raw_parent.get("continuation_profile"),
                        )
                        is not None
                        else None
                    ),
                    policy_snapshot_digest=str(
                        raw_parent.get(
                            "policySnapshotDigest",
                            raw_parent.get("policy_snapshot_digest", ""),
                        )
                    ),
                    expires_at=str(
                        raw_parent.get("expiresAt", raw_parent.get("expires_at", ""))
                    ),
                    fencing_tokens=dict(
                        raw_parent.get(
                            "fencingTokens", raw_parent.get("fencing_tokens", {})
                        )
                    )
                    if isinstance(
                        raw_parent.get(
                            "fencingTokens", raw_parent.get("fencing_tokens", {})
                        ),
                        Mapping,
                    )
                    else {},
                )
                delegation = ChildBudgetDelegation(
                    delegation_id=str(
                        raw_delegation.get(
                            "delegationId", raw_delegation.get("delegation_id", "")
                        )
                    ),
                    parent_permit=parent,
                    child_owner=PolicyResourceRef(
                        str(
                            raw_delegation.get(
                                "childOwner", raw_delegation.get("child_owner", "")
                            )
                        ),
                    ),
                    amounts=usage_amounts(raw_delegation.get("amounts", [])),
                    expires_at=str(
                        raw_delegation.get(
                            "expiresAt", raw_delegation.get("expires_at", "")
                        )
                    ),
                    continuation_profile=(
                        str(
                            raw_delegation.get(
                                "continuationProfile",
                                raw_delegation.get("continuation_profile"),
                            )
                        )
                        if raw_delegation.get(
                            "continuationProfile",
                            raw_delegation.get("continuation_profile"),
                        )
                        is not None
                        else None
                    ),
                )
                child = delegation.create_child_permit(
                    str(
                        fixture.get("childPermitId", fixture.get("child_permit_id", ""))
                    )
                )
                observed = {
                    "permitId": child.permit_id,
                    "owner": child.owner.resource_id,
                    "authorizedAmounts": usage_contract(child.authorized_amounts),
                    "continuationProfile": child.continuation_profile,
                    "reservationRefs": list(child.reservation_refs),
                    "fencingTokens": dict(child.fencing_tokens),
                }
            else:
                diagnostics.append(
                    {
                        "code": "OrchestrationKindUnknown",
                        "message": f"orchestration TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "OrchestrationExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "OrchestrationExpectedMismatch",
                        "message": f"orchestration observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_rag_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.rag_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "RagExpectedInvalid",
                    "message": "rag TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )
        if kind == "freshness":
            retrieval_fixture = fixture.get("retrieval", {})
            if not isinstance(retrieval_fixture, Mapping):
                retrieval_fixture = {}
                diagnostics.append(
                    {
                        "code": "RagRetrievalInvalid",
                        "message": "rag TCK retrieval must be a mapping",
                        "path": "$.retrieval",
                    }
                )
            raw_hits = retrieval_fixture.get("hits", [])
            hits: list[SearchHit] = []
            if not isinstance(raw_hits, list):
                diagnostics.append(
                    {
                        "code": "RagRetrievalHitsInvalid",
                        "message": "rag TCK retrieval hits must be a list",
                        "path": "$.retrieval.hits",
                    }
                )
                raw_hits = []
            for hit_index, raw_hit in enumerate(raw_hits):
                if not isinstance(raw_hit, Mapping):
                    diagnostics.append(
                        {
                            "code": "RagRetrievalHitInvalid",
                            "message": "rag TCK retrieval hit must be a mapping",
                            "path": f"$.retrieval.hits[{hit_index}]",
                        }
                    )
                    continue
                hit_id = str(
                    raw_hit.get("hitId", raw_hit.get("hit_id", f"hit-{hit_index + 1}"))
                )
                item_id = str(
                    raw_hit.get(
                        "itemId", raw_hit.get("item_id", f"doc-{hit_index + 1}")
                    )
                )
                raw_rank = raw_hit.get("rank", hit_index + 1)
                rank = (
                    raw_rank
                    if isinstance(raw_rank, int) and not isinstance(raw_rank, bool)
                    else hit_index + 1
                )
                source_modified_at = raw_hit.get(
                    "sourceModifiedAt", raw_hit.get("source_modified_at")
                )
                metadata: dict[str, object] = {}
                if source_modified_at is not None:
                    metadata["source_modified_at"] = source_modified_at
                source = SourceRef(source_id=item_id, source_kind="document_chunk")
                item = KnowledgeItemRef(
                    item_id,
                    "document_chunk",
                    source,
                    metadata=dict(metadata),
                )
                hits.append(
                    SearchHit(
                        hit_id=hit_id,
                        item=item,
                        rank=rank,
                        retriever=str(raw_hit.get("retriever", "local-test")),
                        metadata=metadata,
                    )
                )
            minimum_source_modified_at = retrieval_fixture.get(
                "minimumSourceModifiedAt",
                retrieval_fixture.get("minimum_source_modified_at"),
            )
            retrieval_metadata: dict[str, object] = {}
            if minimum_source_modified_at is not None:
                retrieval_metadata["minimum_source_modified_at"] = (
                    minimum_source_modified_at
                )
            top_k = retrieval_fixture.get(
                "topK", retrieval_fixture.get("top_k", len(hits))
            )
            if not isinstance(top_k, int) or isinstance(top_k, bool):
                top_k = len(hits)
            retrieval = RetrievalResult(
                retrieval_id=str(
                    retrieval_fixture.get(
                        "retrievalId",
                        retrieval_fixture.get("retrieval_id", case.case_id),
                    )
                ),
                request=SearchRequest(
                    str(retrieval_fixture.get("query", "")), top_k=top_k
                ),
                hits=hits,
                total_candidates=len(hits),
                metadata=retrieval_metadata,
            )
            context = build_context_pack(
                str(fixture.get("contextId", fixture.get("context_id", "ctx-1"))),
                hits,
                token_budget=int(
                    fixture.get("tokenBudget", fixture.get("token_budget", 1024))
                ),
                minimum_source_modified_at=(
                    str(minimum_source_modified_at)
                    if isinstance(minimum_source_modified_at, str)
                    else None
                ),
            )
            metrics = {
                metric.name: metric
                for metric in evaluate_retrieval_metrics(retrieval, set(), k=top_k)
            }
            freshness_value = metrics["freshness_satisfaction"].value
            observed = {
                "selectedHitIds": [hit.hit_id for hit in context.hits],
                "droppedHitIds": list(context.metadata.get("dropped_hit_ids", [])),
                "dropReasons": dict(context.metadata.get("drop_reasons", {})),
                "freshnessSatisfaction": (
                    None if freshness_value is None else str(freshness_value)
                ),
            }
            for key, expected_value in expected.items():
                if observed.get(str(key)) != expected_value:
                    diagnostics.append(
                        {
                            "code": "RagExpectedMismatch",
                            "message": f"rag observed {key} did not match expected value",
                            "path": f"$.expected.{key}",
                        }
                    )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )
        context_fixture = fixture.get("context", {})
        if not isinstance(context_fixture, Mapping):
            context_fixture = {}
            diagnostics.append(
                {
                    "code": "RagContextInvalid",
                    "message": "rag TCK context must be a mapping",
                    "path": "$.context",
                }
            )
        answer_fixture = fixture.get("answer", {})
        if not isinstance(answer_fixture, Mapping):
            answer_fixture = {}
            diagnostics.append(
                {
                    "code": "RagAnswerInvalid",
                    "message": "rag TCK answer must be a mapping",
                    "path": "$.answer",
                }
            )

        context_id = str(
            context_fixture.get("contextId", context_fixture.get("context_id", "ctx-1"))
        )
        context_text = context_fixture.get("text")
        if isinstance(context_text, str):
            source_uri = str(
                context_fixture.get(
                    "sourceUri",
                    context_fixture.get("source_uri", "file:///tmp/context.txt"),
                )
            )
            observed_at = str(
                context_fixture.get(
                    "observedAt",
                    context_fixture.get("observed_at", "2026-06-23T00:00:00Z"),
                )
            )
            asset, revision = create_local_text_revision(
                source_uri, context_text, observed_at
            )
            document = parse_plain_text_document(asset, revision, context_text)
            chunks = chunk_document_by_lines(document, revision, max_elements=1)
            retriever = InMemoryChunkRetriever(
                chunks,
                retriever_id=str(
                    context_fixture.get(
                        "retrieverId", context_fixture.get("retriever_id", "local-test")
                    )
                ),
            )
            query = str(context_fixture.get("query", ""))
            top_k = int(context_fixture.get("topK", context_fixture.get("top_k", 1)))
            context = ContextPack(
                context_id=context_id, hits=retriever.search(query, top_k=top_k)
            )
        else:
            context = ContextPack(context_id=context_id, hits=[])

        claims: list[Claim] = []
        raw_claims = answer_fixture.get("claims", [])
        if isinstance(raw_claims, list):
            for claim_index, raw_claim in enumerate(raw_claims):
                if not isinstance(raw_claim, Mapping):
                    diagnostics.append(
                        {
                            "code": "RagClaimInvalid",
                            "message": "rag TCK claim must be a mapping",
                            "path": f"$.answer.claims[{claim_index}]",
                        }
                    )
                    continue
                raw_citation_ids = raw_claim.get(
                    "citationIds", raw_claim.get("citation_ids", [])
                )
                citation_ids = (
                    [str(item) for item in raw_citation_ids]
                    if isinstance(raw_citation_ids, list)
                    else []
                )
                claims.append(
                    Claim(
                        claim_id=str(
                            raw_claim.get("claimId", raw_claim.get("claim_id", ""))
                        ),
                        text=str(raw_claim.get("text", "")),
                        citation_ids=citation_ids,
                    )
                )
        citations: list[Citation] = []
        raw_citations = answer_fixture.get("citations", [])
        if isinstance(raw_citations, list):
            for citation_index, raw_citation in enumerate(raw_citations):
                if not isinstance(raw_citation, Mapping):
                    diagnostics.append(
                        {
                            "code": "RagCitationInvalid",
                            "message": "rag TCK citation must be a mapping",
                            "path": f"$.answer.citations[{citation_index}]",
                        }
                    )
                    continue
                source_hit_index = raw_citation.get(
                    "sourceHitIndex", raw_citation.get("source_hit_index", 0)
                )
                if not isinstance(source_hit_index, int) or isinstance(
                    source_hit_index, bool
                ):
                    diagnostics.append(
                        {
                            "code": "RagCitationSourceInvalid",
                            "message": "rag TCK citation sourceHitIndex must be an integer",
                            "path": f"$.answer.citations[{citation_index}].sourceHitIndex",
                        }
                    )
                    continue
                if source_hit_index < 0 or source_hit_index >= len(context.hits):
                    diagnostics.append(
                        {
                            "code": "RagCitationSourceMissing",
                            "message": "rag TCK citation sourceHitIndex does not point to context",
                            "path": f"$.answer.citations[{citation_index}].sourceHitIndex",
                        }
                    )
                    continue
                claim_id = raw_citation.get("claimId", raw_citation.get("claim_id"))
                cited_text = raw_citation.get(
                    "citedText", raw_citation.get("cited_text")
                )
                citations.append(
                    Citation(
                        citation_id=str(
                            raw_citation.get(
                                "citationId", raw_citation.get("citation_id", "")
                            )
                        ),
                        source=context.hits[source_hit_index].item.source,
                        claim_id=claim_id if isinstance(claim_id, str) else None,
                        cited_text=cited_text if isinstance(cited_text, str) else None,
                    )
                )

        answer = Answer(
            answer_id=str(
                answer_fixture.get(
                    "answerId", answer_fixture.get("answer_id", "answer-1")
                )
            ),
            text=str(answer_fixture.get("text", "")),
            claims=claims,
            citations=citations,
        )
        result = validate_answer_grounding(
            answer,
            context,
            require_citations=bool(
                fixture.get("requireCitations", fixture.get("require_citations", True))
            ),
            failure_policy=str(
                fixture.get("failurePolicy", fixture.get("failure_policy", "abstain"))
            ),
        )
        observed = {
            "ok": result.ok,
            "issueCodes": [issue.code for issue in result.issues],
            "warningCodes": [
                issue.code for issue in result.issues if issue.severity == "warning"
            ],
            "abstentionReason": None
            if result.abstention is None
            else result.abstention.reason,
            "repaired": result.repaired_answer is not None,
            "contextHitCount": len(context.hits),
        }

        if kind != "grounding":
            diagnostics.append(
                {
                    "code": "RagKindUnknown",
                    "message": f"rag TCK kind {kind!r} is not supported",
                    "path": "$.kind",
                }
            )
        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "RagExpectedMismatch",
                        "message": f"rag observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_retry_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.retry_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "RetryExpectedInvalid",
                    "message": "retry TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        attempts = {"count": 0}
        seen_idempotency_keys: list[str | None] = []
        registry = RuntimeRegistry(allow_untyped=True)
        block_id = str(fixture.get("block", "test.flaky_write@1"))
        node_id = str(fixture.get("nodeId", fixture.get("node_id", "write")))
        max_attempts = int(fixture.get("maxAttempts", fixture.get("max_attempts", 1)))
        failures_before_success = int(
            fixture.get(
                "failuresBeforeSuccess", fixture.get("failures_before_success", 0)
            )
        )
        cancel_on_attempt = fixture.get(
            "cancelOnAttempt", fixture.get("cancel_on_attempt")
        )
        cancel_reason = str(
            fixture.get("cancelReason", fixture.get("cancel_reason", "policy_stop"))
        )
        idempotency_key = fixture.get("idempotencyKey", fixture.get("idempotency_key"))

        def retry_block(
            inputs: dict[str, object],
            config: dict[str, object],
            context: dict[str, object],
        ) -> dict[str, object]:
            attempts["count"] += 1
            seen_idempotency_keys.append(
                str(context.get("idempotency_key"))
                if context.get("idempotency_key") is not None
                else None
            )
            if (
                isinstance(cancel_on_attempt, int)
                and not isinstance(cancel_on_attempt, bool)
                and attempts["count"] == cancel_on_attempt
            ):
                token = context.get("cancellation_token")
                if isinstance(token, CancellationToken):
                    token.cancel(cancel_reason)
            if attempts["count"] <= failures_before_success:
                raise RuntimeError(str(fixture.get("error", "temporary failure")))
            return {
                "value": fixture.get(
                    "outputValue", fixture.get("output_value", "committed")
                )
            }

        registry.register(block_id, retry_block)
        retry_config: dict[str, object] = {"maxAttempts": max_attempts}
        if idempotency_key is not None:
            retry_config["idempotencyKey"] = str(idempotency_key)
        raw_effects = fixture.get("effects", [])
        if isinstance(raw_effects, str):
            raw_effects = [raw_effects]
        graph = {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {
                "name": "retry-tck-"
                + "".join(
                    character if character.isalnum() or character in "._-" else "-"
                    for character in case.case_id
                )
            },
            "spec": {
                "nodes": {
                    node_id: {
                        "block": block_id,
                        "effects": list(raw_effects)
                        if isinstance(raw_effects, list)
                        else [],
                        "flow": {"retry": retry_config},
                        "outputs": {"value": "$output.value"},
                    }
                }
            },
        }

        try:
            result = InProcessRuntime(registry).run(graph, {})
            retry_idempotency_keys = [
                record.payload.get("idempotencyKey")
                for record in result.journal.records
                if record.kind == "node_retry"
            ]
            observed: dict[str, object] = {
                "status": result.status,
                "terminalKind": result.journal.terminal_kind,
                "attempts": attempts["count"],
                "retryCount": len(retry_idempotency_keys),
                "retryIdempotencyKeys": retry_idempotency_keys,
                "contextIdempotencyKeys": seen_idempotency_keys,
                "outputs": result.outputs,
                "journalKinds": [record.kind for record in result.journal.records],
                "compileError": None,
            }
        except ValueError as error:
            observed = {
                "status": "compile_failed",
                "terminalKind": "compile_error",
                "attempts": attempts["count"],
                "retryCount": 0,
                "retryIdempotencyKeys": [],
                "contextIdempotencyKeys": seen_idempotency_keys,
                "outputs": {},
                "journalKinds": [],
                "compileError": str(error),
            }

        if kind not in {"node_retry", "cancelled_before_retry"}:
            diagnostics.append(
                {
                    "code": "RetryKindUnknown",
                    "message": f"retry TCK kind {kind!r} is not supported",
                    "path": "$.kind",
                }
            )
        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "RetryExpectedMismatch",
                        "message": f"retry observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_tool_lifecycle_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.tool_lifecycle_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "ToolLifecycleExpectedInvalid",
                    "message": "tool-lifecycle TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )
        observed: dict[str, object] = {}

        if kind == "incremental_arguments":
            draft = ToolCallDraft.proposed(
                str(fixture.get("responseId", "response-1")),
                str(fixture.get("toolCallId", "call-1")),
                str(fixture.get("toolName", "knowledge.search")),
            )
            statuses = [draft.status]
            fragments = fixture.get("fragments", [])
            if not isinstance(fragments, list):
                fragments = []
                diagnostics.append(
                    {
                        "code": "ToolLifecycleFragmentsInvalid",
                        "message": "incremental argument case requires fragments",
                        "path": "$.fragments",
                    }
                )
            for fragment in fragments:
                draft = draft.append_argument_fragment(str(fragment))
                statuses.append(draft.status)
            try:
                draft.into_tool_call(
                    str(fixture.get("resolvedToolId", "resolved-tool-1")),
                    created_at=str(fixture.get("createdAt", "2026-06-23T00:00:00Z")),
                )
                finalized_before_complete = True
            except ToolCallError:
                finalized_before_complete = False
            draft = draft.complete_arguments()
            statuses.append(draft.status)
            try:
                call = draft.into_tool_call(
                    str(fixture.get("resolvedToolId", "resolved-tool-1")),
                    created_at=str(fixture.get("createdAt", "2026-06-23T00:00:00Z")),
                )
                finalized_after_complete = True
                observed_arguments = call.arguments
                observed_status = call.status
            except ToolCallError as error:
                finalized_after_complete = False
                observed_arguments = None
                observed_status = f"error:{type(error).__name__}"
            observed = {
                "statuses": statuses,
                "finalizedBeforeComplete": finalized_before_complete,
                "finalizedAfterComplete": finalized_after_complete,
                "callStatus": observed_status,
                "arguments": observed_arguments,
            }
        elif kind in {
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
        }:
            schema_id = str(fixture.get("schemaId", "schemas/ProcessRun@1"))
            tool_name = str(fixture.get("toolName", "process.run"))
            catalog = ToolCatalog(
                definitions=(
                    ToolDefinition(tool_name, "Run an approved process.", schema_id),
                ),
                bindings=(
                    ToolBinding(
                        "binding-process",
                        tool_name,
                        BlockToolImplementation("blocks.process"),
                        effects=frozenset({"process"}),
                        approval="always",
                        idempotency="required",
                    ),
                ),
            )
            resolved_tool = catalog.resolve(
                ToolResolutionScope(),
                effective_policy_snapshot_id="policy-snapshot-1",
            )[0]
            if kind == "admission_expired_resolved_tool":
                resolved_tool = replace(
                    resolved_tool,
                    valid_until=str(
                        fixture.get("resolvedToolValidUntil", "1970-01-01T00:00:01Z")
                    ),
                )
            schemas = (
                ToolSchemaRegistry(())
                if kind == "admission_missing_schema"
                else ToolSchemaRegistry(
                    (
                        JsonSchema(
                            schema_id,
                            JsonSchemaNode.object().required_property(
                                "cmd",
                                JsonSchemaNode.array(JsonSchemaNode.string()),
                            ),
                        ),
                    )
                )
            )
            draft = ToolCallDraft.proposed(
                "response-1",
                "call-1",
                str(fixture.get("callToolName", tool_name)),
            )
            draft = draft.append_argument_fragment(
                json.dumps(fixture.get("arguments", {}), sort_keys=True)
            )
            call = draft.complete_arguments().into_tool_call(
                str(fixture.get("callResolvedToolId", resolved_tool.resolved_tool_id)),
                created_at="2026-06-23T00:00:00Z",
            )
            if kind == "admission_arguments_digest_mismatch":
                object.__setattr__(
                    call,
                    "arguments_digest",
                    str(fixture.get("argumentsDigest", "sha256:stale")),
                )
            policy_decision = PolicyDecision(
                decision_id="decision-allow-tool",
                effect="allow",
                reason_codes=("allow-process",),
                policy_refs=("allow-process",),
                evaluated_at="2026-06-23T00:00:01Z",
                valid_until=(
                    str(fixture["policyValidUntil"])
                    if kind == "admission_expired_policy_decision"
                    and fixture.get("policyValidUntil") is not None
                    else None
                ),
                input_digest="sha256:before-tool",
            )
            if kind == "admission_policy_input_digest_mismatch":
                policy_decision = replace(
                    policy_decision,
                    input_digest=str(
                        fixture.get(
                            "actualPolicyInputDigest", "sha256:stale-before-tool"
                        )
                    ),
                )
            if kind == "admission_policy_input_digest_missing":
                policy_decision = replace(
                    policy_decision,
                    input_digest=str(fixture.get("actualPolicyInputDigest", "")),
                )
            if kind == "admission_policy_denied":
                raw_reason_codes = fixture.get("reasonCodes", ())
                reason_codes = (
                    tuple(str(reason_code) for reason_code in raw_reason_codes)
                    if isinstance(raw_reason_codes, list)
                    else ()
                )
                policy_decision = replace(
                    policy_decision,
                    decision_id=str(fixture.get("decisionId", "decision-deny-tool")),
                    effect="deny",
                    reason_codes=reason_codes,
                )
            if kind == "admission_policy_deferred":
                raw_reason_codes = fixture.get("reasonCodes", ())
                reason_codes = (
                    tuple(str(reason_code) for reason_code in raw_reason_codes)
                    if isinstance(raw_reason_codes, list)
                    else ()
                )
                policy_decision = replace(
                    policy_decision,
                    decision_id=str(fixture.get("decisionId", "decision-defer-tool")),
                    effect="defer",
                    reason_codes=reason_codes,
                )
            output_policy_state = fixture.get("outputPolicyState")
            if not isinstance(output_policy_state, Mapping):
                output_policy_state = None
            approval = None
            if kind in {
                "admission_missing_required_idempotency_key",
                "admission_blank_idempotency_key",
                "admission_expired_approval",
            }:
                request = ToolApprovalRequest.for_call(
                    str(fixture.get("approvalId", "approval-1")),
                    resolved_tool,
                    call,
                    principal_id="user-1",
                    requested_at=int(fixture.get("requestedAtUnixMs", 1000)),
                    expires_at=int(fixture.get("expiresAtUnixMs", 2000)),
                )
                approval = ToolApprovalRecord.approve(
                    request,
                    approver_id="admin-1",
                    decided_at=int(fixture.get("decidedAtUnixMs", 1100)),
                )
            try:
                admit_tool_call(
                    call,
                    resolved_tool,
                    schemas,
                    policy_decision=policy_decision,
                    expected_policy_input_digest=str(
                        fixture.get(
                            "expectedPolicyInputDigest", policy_decision.input_digest
                        )
                    ),
                    output_policy_state=output_policy_state,
                    approval=approval,
                    principal_id="user-1",
                    idempotency_key=(
                        None
                        if kind == "admission_missing_required_idempotency_key"
                        else str(fixture.get("idempotencyKey", " "))
                        if kind
                        in {
                            "admission_blank_idempotency_key",
                            "admission_missing_approval",
                            "admission_expired_approval",
                        }
                        else "idem-1"
                    ),
                    admitted_at=str(fixture.get("admittedAt", "2026-06-23T00:00:02Z")),
                    now=int(fixture.get("admittedAtUnixMs", 1200)),
                )
                observed = {
                    "admitted": True,
                    "error": None,
                    "schemaRejectedBeforeApproval": False,
                    "schemaMissingBeforeApproval": False,
                    "resolvedToolMismatchBeforeSchema": False,
                    "toolNameMismatchBeforeSchema": False,
                    "argumentsDigestRejectedBeforeSchema": False,
                    "policyStoppedBeforeApproval": False,
                    "policyExpiredBeforeApproval": False,
                    "resolvedToolExpiredBeforeApproval": False,
                    "policyDigestRejectedBeforeApproval": False,
                    "policyDigestMissingBeforeApproval": False,
                    "policyDeniedBeforeApproval": False,
                    "policyDeferredBeforeApproval": False,
                    "approvalRequiredBeforeIdempotency": False,
                    "expiredApprovalRejectedBeforeIdempotency": False,
                    "idempotencyRejectedAfterApproval": False,
                    "blankIdempotencyRejectedAfterApproval": False,
                }
            except Exception as error:
                message = str(error)
                observed = {
                    "admitted": False,
                    "error": message,
                    "schemaRejectedBeforeApproval": (
                        "arguments invalid" in message
                        and "requires approval" not in message
                    ),
                    "schemaMissingBeforeApproval": (
                        "schema" in message
                        and "not registered" in message
                        and "requires approval" not in message
                    ),
                    "resolvedToolMismatchBeforeSchema": (
                        "resolved tool" in message
                        and "requires approval" not in message
                    ),
                    "toolNameMismatchBeforeSchema": (
                        "name" in message and "requires approval" not in message
                    ),
                    "argumentsDigestRejectedBeforeSchema": (
                        "digest" in message and "requires approval" not in message
                    ),
                    "policyStoppedBeforeApproval": (
                        "policy stopped" in message
                        and "requires approval" not in message
                    ),
                    "policyExpiredBeforeApproval": (
                        "expired" in message and "requires approval" not in message
                    ),
                    "resolvedToolExpiredBeforeApproval": (
                        "resolved tool" in message
                        and "expired" in message
                        and "requires approval" not in message
                    ),
                    "policyDigestRejectedBeforeApproval": (
                        "input digest" in message and "requires approval" not in message
                    ),
                    "policyDigestMissingBeforeApproval": (
                        "input digest" in message and "requires approval" not in message
                    ),
                    "policyDeniedBeforeApproval": (
                        "denied" in message and "requires approval" not in message
                    ),
                    "policyDeferredBeforeApproval": (
                        "deferred" in message and "requires approval" not in message
                    ),
                    "approvalRequiredBeforeIdempotency": (
                        "requires approval" in message and "idempotency" not in message
                    ),
                    "expiredApprovalRejectedBeforeIdempotency": (
                        "not valid" in message and "idempotency" not in message
                    ),
                    "idempotencyRejectedAfterApproval": (
                        "idempotency" in message and "requires approval" not in message
                    ),
                    "blankIdempotencyRejectedAfterApproval": (
                        "idempotency" in message and "requires approval" not in message
                    ),
                }
        elif kind == "approval_argument_mutation":
            schema_id = str(fixture.get("schemaId", "schemas/ProcessRun@1"))
            tool_name = str(fixture.get("toolName", "process.run"))
            catalog = ToolCatalog(
                definitions=(
                    ToolDefinition(tool_name, "Run an approved process.", schema_id),
                ),
                bindings=(
                    ToolBinding(
                        "binding-process",
                        tool_name,
                        BlockToolImplementation("blocks.process"),
                        effects=frozenset({"process"}),
                        approval="always",
                        idempotency="required",
                    ),
                ),
            )
            resolved_tool = catalog.resolve(
                ToolResolutionScope(),
                effective_policy_snapshot_id="policy-snapshot-1",
            )[0]
            schemas = ToolSchemaRegistry(
                (
                    JsonSchema(
                        schema_id,
                        JsonSchemaNode.object().required_property(
                            "cmd",
                            JsonSchemaNode.array(JsonSchemaNode.string()),
                        ),
                    ),
                )
            )
            draft = ToolCallDraft.proposed("response-1", "call-1", tool_name)
            draft = draft.append_argument_fragment(
                json.dumps(fixture.get("initialArguments", {}), sort_keys=True)
            )
            call = draft.complete_arguments().into_tool_call(
                resolved_tool.resolved_tool_id,
                created_at="2026-06-23T00:00:00Z",
            )
            request = ToolApprovalRequest.for_call(
                "approval-1",
                resolved_tool,
                call,
                principal_id="user-1",
                requested_at=1000,
                expires_at=2000,
            )
            approval = ToolApprovalRecord.approve(
                request, approver_id="admin-1", decided_at=1100
            )
            revised = call.revise_arguments(fixture.get("mutatedArguments", {}))
            initial_valid = approval.is_valid_for(
                resolved_tool, call, principal_id="user-1", now=1500
            )
            revised_valid = approval.is_valid_for(
                resolved_tool, revised, principal_id="user-1", now=1500
            )
            policy_decision = PolicyDecision(
                decision_id="decision-allow-tool",
                effect="allow",
                reason_codes=("allow-process",),
                policy_refs=("allow-process",),
                evaluated_at="2026-06-23T00:00:01Z",
                input_digest="sha256:before-tool",
            )
            try:
                admit_tool_call(
                    revised,
                    resolved_tool,
                    schemas,
                    policy_decision=policy_decision,
                    expected_policy_input_digest=policy_decision.input_digest,
                    approval=approval,
                    principal_id="user-1",
                    idempotency_key="idem-1",
                    admitted_at="2026-06-23T00:00:02Z",
                    now=1500,
                )
                admission_with_stale_approval = True
                error_message = None
            except Exception as error:
                admission_with_stale_approval = False
                error_message = str(error)
            observed = {
                "initialApprovalValid": initial_valid,
                "mutatedApprovalValid": revised_valid,
                "digestChanged": revised.arguments_digest != call.arguments_digest,
                "revisedRevision": revised.revision,
                "admissionWithStaleApproval": admission_with_stale_approval,
                "error": error_message,
            }
        else:
            diagnostics.append(
                {
                    "code": "ToolLifecycleKindUnknown",
                    "message": f"tool-lifecycle TCK kind {kind!r} is not supported",
                    "path": "$.kind",
                }
            )

        error_contains = expected.get("errorContains")
        for key, expected_value in expected.items():
            if key == "errorContains":
                if expected_value is not None and str(expected_value) not in str(
                    observed.get("error")
                ):
                    diagnostics.append(
                        {
                            "code": "ToolLifecycleErrorMismatch",
                            "message": "tool-lifecycle observed error did not contain expected text",
                            "path": "$.expected.errorContains",
                        }
                    )
                continue
            if observed.get(key) != expected_value:
                diagnostics.append(
                    {
                        "code": "ToolLifecycleExpectedMismatch",
                        "message": f"tool-lifecycle observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        if error_contains is None and observed.get("error") is not None:
            diagnostics.append(
                {
                    "code": "ToolLifecycleUnexpectedError",
                    "message": "tool-lifecycle case produced an unexpected error",
                    "path": "$.observed.error",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _tool_result_schema_node_from_fixture(self, raw_node: object) -> JsonSchemaNode:
        if not isinstance(raw_node, Mapping):
            return JsonSchemaNode.any()
        raw_type = raw_node.get(
            "type", raw_node.get("expectedType", raw_node.get("expected_type"))
        )
        if raw_type == "string":
            node = JsonSchemaNode.string()
        elif raw_type == "integer":
            node = JsonSchemaNode.integer()
        elif raw_type == "number":
            node = JsonSchemaNode.number()
        elif raw_type == "boolean":
            node = JsonSchemaNode.boolean()
        elif raw_type == "array":
            node = JsonSchemaNode.array(
                self._tool_result_schema_node_from_fixture(raw_node.get("items", {}))
            )
        elif raw_type == "object":
            node = JsonSchemaNode.object()
        else:
            node = JsonSchemaNode.any()
        raw_required = raw_node.get("required", ())
        required = (
            {str(item) for item in raw_required}
            if isinstance(raw_required, list | tuple)
            else set()
        )
        raw_properties = raw_node.get("properties", {})
        if isinstance(raw_properties, Mapping):
            for property_name, property_schema in raw_properties.items():
                if str(property_name) in required:
                    node = node.required_property(
                        str(property_name),
                        self._tool_result_schema_node_from_fixture(property_schema),
                    )
                else:
                    node = node.property(
                        str(property_name),
                        self._tool_result_schema_node_from_fixture(property_schema),
                    )
        return node

    def _tool_result_content_part_from_fixture(self, raw_part: object) -> ContentPart:
        if not isinstance(raw_part, Mapping):
            raise ValueError("tool-result output part must be a mapping")
        kind = str(raw_part.get("kind", "text"))
        metadata = raw_part.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("tool-result output part metadata must be a mapping")
        if kind == "text":
            text = raw_part.get("text")
            if not isinstance(text, str):
                raise ValueError("tool-result text output part requires text")
            return ContentPart(kind="text", text=text, metadata=dict(metadata))
        if kind in {"json", "artifact_ref"}:
            data = raw_part.get("data")
            if not isinstance(data, Mapping):
                raise ValueError(f"tool-result {kind} output part requires object data")
            return ContentPart(kind=kind, data=dict(data), metadata=dict(metadata))
        raise ValueError(f"tool-result output part has unsupported kind {kind!r}")

    def _run_tool_result_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.tool_result_fixture
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "ToolResultExpectedInvalid",
                    "message": "tool-result TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        if fixture.get("kind") == "stream_state":
            try:
                stream = ToolResultStreamState()
                accepted: list[dict[str, object]] = []
                errors: list[dict[str, object]] = []
                tool_call_ids: set[str] = set()
                operations = fixture.get("operations", [])
                if not isinstance(operations, list):
                    raise ValueError(
                        "tool-result stream_state operations must be a list"
                    )
                for operation_index, operation in enumerate(operations):
                    try:
                        if not isinstance(operation, Mapping):
                            raise ValueError(
                                "tool-result stream_state operation must be a mapping"
                            )
                        raw_event = operation.get("event")
                        if not isinstance(raw_event, Mapping):
                            raise ValueError(
                                "tool-result stream_state accept operation requires event"
                            )
                        event_kind = str(raw_event.get("kind", ""))
                        tool_call_id = str(
                            raw_event.get(
                                "toolCallId", raw_event.get("tool_call_id", "")
                            )
                        )
                        sequence = int(raw_event.get("sequence", 0))
                        tool_call_ids.add(tool_call_id)
                        if event_kind == "started":
                            result_event = ToolResultEvent.started(
                                tool_call_id,
                                sequence,
                                started_at=str(
                                    raw_event.get(
                                        "startedAt", raw_event.get("started_at", "")
                                    )
                                ),
                            )
                        elif event_kind == "delta":
                            raw_output = raw_event.get("output", [])
                            if not isinstance(raw_output, list):
                                raise ValueError(
                                    "tool-result stream_state delta output must be a list"
                                )
                            result_event = ToolResultEvent.delta(
                                tool_call_id,
                                sequence,
                                tuple(
                                    self._tool_result_content_part_from_fixture(part)
                                    for part in raw_output
                                ),
                            )
                        elif event_kind == "completed":
                            raw_result = raw_event.get("result", {})
                            if not isinstance(raw_result, Mapping):
                                raise ValueError(
                                    "tool-result stream_state completed event requires result"
                                )
                            raw_output = raw_result.get("output", [])
                            if not isinstance(raw_output, list):
                                raise ValueError(
                                    "tool-result stream_state completed output must be a list"
                                )
                            result = ToolResult.completed(
                                tool_call_id,
                                tuple(
                                    self._tool_result_content_part_from_fixture(part)
                                    for part in raw_output
                                ),
                                started_at=str(
                                    raw_result.get(
                                        "startedAt", raw_result.get("started_at", "")
                                    )
                                ),
                                completed_at=str(
                                    raw_result.get(
                                        "completedAt",
                                        raw_result.get("completed_at", ""),
                                    )
                                ),
                            )
                            result_event = ToolResultEvent.completed(
                                tool_call_id, sequence, result
                            )
                        elif event_kind == "denied":
                            raw_result = raw_event.get("result", {})
                            if not isinstance(raw_result, Mapping):
                                raise ValueError(
                                    "tool-result stream_state denied event requires result"
                                )
                            raw_error = raw_result.get("error", {})
                            if not isinstance(raw_error, Mapping):
                                raw_error = {
                                    "code": "tool.denied",
                                    "message": str(raw_error),
                                }
                            result = ToolResult.denied(
                                tool_call_id,
                                error=dict(raw_error),
                                completed_at=str(
                                    raw_result.get(
                                        "completedAt",
                                        raw_result.get("completed_at", ""),
                                    )
                                ),
                            )
                            effect_outcome = raw_result.get(
                                "effectOutcome", raw_result.get("effect_outcome")
                            )
                            if effect_outcome is not None:
                                result = result.with_effect_outcome(str(effect_outcome))
                            result_event = ToolResultEvent.denied(
                                tool_call_id, sequence, result
                            )
                        else:
                            raise ValueError(
                                f"tool-result stream_state event kind {event_kind!r} is not supported"
                            )
                    except (TypeError, ValueError):
                        errors.append(
                            {"operation": operation_index, "code": "InvalidEvent"}
                        )
                        continue

                    try:
                        accepted_event = stream.accept(result_event)
                        accepted.append(
                            {
                                "toolCallId": accepted_event.tool_call_id,
                                "kind": accepted_event.kind,
                            }
                        )
                    except ToolResultStreamError as error:
                        message = str(error)
                        if "before started" in message:
                            code = "EventBeforeStarted"
                        elif "already received started" in message:
                            code = "DuplicateStarted"
                        elif "non-monotonic sequence" in message:
                            code = "NonMonotonicSequence"
                        elif "is final" in message:
                            code = "EventAfterFinalResult"
                        else:
                            code = type(error).__name__
                        errors.append({"operation": operation_index, "code": code})

                observed = {
                    "accepted": accepted,
                    "errors": errors,
                    "finalStatuses": {
                        tool_call_id: final_result.status
                        for tool_call_id in sorted(tool_call_ids)
                        if (final_result := stream.final_result_for(tool_call_id))
                        is not None
                    },
                    "lastSequences": {
                        tool_call_id: last_sequence
                        for tool_call_id in sorted(tool_call_ids)
                        if (last_sequence := stream.last_sequence_for(tool_call_id))
                        is not None
                    },
                }
            except Exception as error:
                observed = {
                    "ok": False,
                    "error": str(error),
                    "errorType": type(error).__name__,
                }

            for key, expected_value in expected.items():
                if observed.get(str(key)) != expected_value:
                    diagnostics.append(
                        {
                            "code": "ToolResultExpectedMismatch",
                            "message": f"tool-result observed {key} did not match expected value",
                            "path": f"$.expected.{key}",
                        }
                    )
            if observed.get("error") is not None:
                diagnostics.append(
                    {
                        "code": "ToolResultUnexpectedError",
                        "message": "tool-result case produced an unexpected error",
                        "path": "$.observed.error",
                    }
                )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )

        observed: dict[str, object]
        try:
            raw_tool = fixture.get("tool", {})
            if not isinstance(raw_tool, Mapping):
                raise ValueError("tool-result tool must be a mapping")
            tool_name = str(raw_tool.get("name", "knowledge.search"))
            output_schema = raw_tool.get("outputSchema", raw_tool.get("output_schema"))
            definition = ToolDefinition(
                tool_name,
                str(raw_tool.get("description", "Execute a tool.")),
                str(
                    raw_tool.get(
                        "inputSchema",
                        raw_tool.get("input_schema", "schemas/ToolRequest@1"),
                    )
                ),
                output_schema=str(output_schema) if output_schema is not None else None,
            )
            binding = ToolBinding(
                str(
                    raw_tool.get(
                        "bindingId", raw_tool.get("binding_id", "binding-tool")
                    )
                ),
                tool_name,
                BlockToolImplementation(str(raw_tool.get("block", "blocks.tool"))),
                effects=frozenset(
                    str(effect) for effect in raw_tool.get("effects", ())
                ),
                approval=str(raw_tool.get("approval", "never")),
                idempotency=str(raw_tool.get("idempotency", "not_applicable")),
                result_mode=str(
                    raw_tool.get("resultMode", raw_tool.get("result_mode", "value"))
                ),
            )
            resolved_tool = ResolvedTool.from_definition_and_binding(
                resolved_tool_id=str(raw_tool.get("resolvedToolId", "resolved-tool-1")),
                definition=definition,
                binding=binding,
                effective_policy_snapshot_id=str(
                    raw_tool.get("policySnapshotId", "policy-snapshot-1")
                ),
                allowed_for_principal=True,
            )
            arguments = fixture.get("arguments", {})
            draft = ToolCallDraft.proposed("response-1", "call-1", tool_name)
            call = (
                draft.append_argument_fragment(json.dumps(arguments, sort_keys=True))
                .complete_arguments()
                .into_tool_call(
                    resolved_tool.resolved_tool_id,
                    created_at="2026-06-23T00:00:00Z",
                )
            )

            schemas: list[JsonSchema] = []
            raw_schemas = fixture.get("schemas", [])
            if isinstance(raw_schemas, list):
                for schema_index, raw_schema in enumerate(raw_schemas):
                    if not isinstance(raw_schema, Mapping):
                        raise ValueError(
                            f"tool-result schemas[{schema_index}] must be a mapping"
                        )
                    schema_id = raw_schema.get("schemaId", raw_schema.get("schema_id"))
                    if not isinstance(schema_id, str):
                        raise ValueError(
                            f"tool-result schemas[{schema_index}] requires schemaId"
                        )
                    schemas.append(
                        JsonSchema(
                            schema_id,
                            self._tool_result_schema_node_from_fixture(
                                raw_schema.get("root", raw_schema)
                            ),
                        )
                    )
            schema_registry = ToolSchemaRegistry(tuple(schemas))

            raw_result = fixture.get("result", {})
            if not isinstance(raw_result, Mapping):
                raise ValueError("tool-result result must be a mapping")
            raw_output = raw_result.get("output", [])
            if not isinstance(raw_output, list):
                raise ValueError("tool-result output must be a list")
            output = tuple(
                self._tool_result_content_part_from_fixture(part) for part in raw_output
            )
            result = ToolResult.completed(
                "call-1",
                output,
                started_at=str(raw_result.get("startedAt", "2026-06-23T00:00:01Z")),
                completed_at=str(raw_result.get("completedAt", "2026-06-23T00:00:02Z")),
            )
            mutation = raw_result.get("mutateAfterDigest")
            if isinstance(mutation, Mapping):
                part_index = int(mutation.get("part", 0))
                if "data" in mutation and result.output[part_index].data is not None:
                    result.output[part_index].data.clear()
                    replacement = mutation["data"]
                    if isinstance(replacement, Mapping):
                        result.output[part_index].data.update(dict(replacement))

            content_policy = fixture.get(
                "contentPolicy", fixture.get("content_policy", {})
            )
            if not isinstance(content_policy, Mapping):
                content_policy = {}
            model_output = validate_tool_result_for_model(
                call,
                result,
                resolved_tool,
                schema_registry,
                max_output_bytes=(
                    int(content_policy["maxOutputBytes"])
                    if content_policy.get("maxOutputBytes") is not None
                    else None
                ),
                redactions=tuple(
                    dict(item)
                    for item in content_policy.get("redactions", ())
                    if isinstance(item, Mapping)
                ),
                capture_policy=(
                    dict(content_policy["capturePolicy"])
                    if isinstance(content_policy.get("capturePolicy"), Mapping)
                    else (
                        dict(content_policy["capture_policy"])
                        if isinstance(content_policy.get("capture_policy"), Mapping)
                        else None
                    )
                ),
                trust_designation=str(
                    content_policy.get("trustDesignation", "untrusted_external")
                ),
                prompt_injection_label=str(
                    content_policy.get("promptInjectionLabel", "untrusted_tool_output")
                ),
                content_classification=str(
                    content_policy.get("contentClassification", "external_tool_output")
                ),
            )
            observed = {
                "ok": True,
                "outputKinds": [part.kind for part in model_output],
                "texts": [part.text for part in model_output if part.text is not None],
                "jsonOutputs": [
                    dict(part.data)
                    for part in model_output
                    if part.kind == "json" and part.data is not None
                ],
                "trustDesignations": [
                    part.metadata.get("trust_designation") for part in model_output
                ],
                "promptInjectionLabels": [
                    part.metadata.get("prompt_injection_label") for part in model_output
                ],
                "contentClassifications": [
                    part.metadata.get("content_classification") for part in model_output
                ],
                "captureModes": [
                    part.metadata.get("capture", {}).get("mode")
                    for part in model_output
                    if isinstance(part.metadata.get("capture"), Mapping)
                ],
                "redactionCounts": [
                    part.metadata.get("capture", {}).get("redaction_count")
                    for part in model_output
                    if isinstance(part.metadata.get("capture"), Mapping)
                ],
            }
        except Exception as error:
            observed = {
                "ok": False,
                "error": str(error),
                "errorType": type(error).__name__,
            }

        for key, expected_value in expected.items():
            if key == "errorContains":
                if expected_value is not None and str(expected_value) not in str(
                    observed.get("error")
                ):
                    diagnostics.append(
                        {
                            "code": "ToolResultErrorMismatch",
                            "message": "tool-result observed error did not contain expected text",
                            "path": "$.expected.errorContains",
                        }
                    )
                continue
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "ToolResultExpectedMismatch",
                        "message": f"tool-result observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        if expected.get("errorContains") is None and observed.get("error") is not None:
            diagnostics.append(
                {
                    "code": "ToolResultUnexpectedError",
                    "message": "tool-result case produced an unexpected error",
                    "path": "$.observed.error",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_tool_execution_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.tool_execution_fixture
        response_id = str(fixture.get("responseId", "response-1"))
        effect_key_template = fixture.get("effectKeyTemplate")
        raw_calls = fixture.get("calls", [])
        planned_calls: list[ToolPlanCall] = []
        call_construction_error: ToolExecutionPlanError | None = None
        for call_index, raw_call in enumerate(
            raw_calls if isinstance(raw_calls, list) else []
        ):
            if not isinstance(raw_call, Mapping):
                diagnostics.append(
                    {
                        "code": "ToolExecutionCallInvalid",
                        "message": "tool-execution TCK call must be a mapping",
                        "path": f"$.calls[{call_index}]",
                    }
                )
                continue
            raw_arguments = raw_call.get("arguments", {})
            if not isinstance(raw_arguments, Mapping):
                diagnostics.append(
                    {
                        "code": "ToolExecutionArgumentsInvalid",
                        "message": "tool-execution TCK call arguments must be a mapping",
                        "path": f"$.calls[{call_index}].arguments",
                    }
                )
                continue
            try:
                draft = ToolCallDraft.proposed(
                    response_id,
                    str(raw_call.get("toolCallId", "")),
                    str(raw_call.get("toolName", "tool.run")),
                )
                draft = draft.append_argument_fragment(
                    json.dumps(raw_arguments, sort_keys=True)
                )
                call = draft.complete_arguments().into_tool_call(
                    str(raw_call.get("resolvedToolId", "resolved-tool-1")),
                    created_at=str(raw_call.get("createdAt", "2026-06-23T00:00:00Z")),
                )
                depends_on = raw_call.get("dependsOn", raw_call.get("depends_on", ()))
                if isinstance(depends_on, str):
                    depends_on = (depends_on,)
                call = replace(
                    call,
                    depends_on=tuple(
                        str(dependency) for dependency in depends_on or ()
                    ),
                )
                raw_effects = raw_call.get("effects", [])
                if isinstance(raw_effects, str):
                    raw_effects = [raw_effects]
                planned_call = ToolPlanCall(
                    call,
                    effect_key=(
                        str(raw_call["effectKey"])
                        if raw_call.get("effectKey") is not None
                        else None
                    ),
                    effects=frozenset(str(effect) for effect in raw_effects or ()),
                    cancellation=str(raw_call.get("cancellation", "cooperative")),
                )
                if (
                    effect_key_template is not None
                    and raw_call.get("effectKey") is None
                ):
                    planned_call = planned_call.with_effect_key_template(
                        str(effect_key_template)
                    )
                planned_calls.append(planned_call)
            except ToolExecutionPlanError as error:
                call_construction_error = error
                break
            except ValueError as error:
                call_construction_error = ToolExecutionPlanError(str(error))
                break

        observed: dict[str, object] = {"operations": []}
        expected_creation_error = fixture.get("expectedCreationError")
        try:
            if call_construction_error is not None:
                raise call_construction_error
            plan = ToolExecutionPlan(
                plan_id=str(fixture.get("planId", "plan-1")),
                response_id=response_id,
                calls=tuple(planned_calls),
                maximum_parallelism=int(fixture.get("maximumParallelism", 1)),
                failure_policy=str(
                    fixture.get("failurePolicy", "return_failures_to_model")
                ),
                cancellation_policy=str(
                    fixture.get("cancellationPolicy", "cancel_dependents")
                ),
            )
            observed["creationError"] = None
        except ToolExecutionPlanError as error:
            creation_error = _tool_execution_error_code(error)
            observed["creationError"] = creation_error
            if expected_creation_error != creation_error:
                diagnostics.append(
                    {
                        "code": "ToolExecutionCreationErrorMismatch",
                        "message": "tool-execution plan creation error did not match expected result",
                        "path": "$.expectedCreationError",
                    }
                )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )

        if expected_creation_error is not None:
            diagnostics.append(
                {
                    "code": "ToolExecutionCreationUnexpectedSuccess",
                    "message": "tool-execution TCK case expected creation failure but plan was created",
                    "path": "$.expectedCreationError",
                }
            )

        operations = fixture.get("operations", [])
        if not isinstance(operations, list):
            operations = []
            diagnostics.append(
                {
                    "code": "ToolExecutionOperationsInvalid",
                    "message": "tool-execution TCK operations must be a list",
                    "path": "$.operations",
                }
            )
        operation_observations: list[dict[str, object]] = []
        for operation_index, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                diagnostics.append(
                    {
                        "code": "ToolExecutionOperationInvalid",
                        "message": "tool-execution TCK operation must be a mapping",
                        "path": f"$.operations[{operation_index}]",
                    }
                )
                continue
            op = str(operation.get("op", ""))
            if op == "ready":
                ready = plan.ready_call_ids()
                operation_observations.append({"op": "ready", "ready": ready})
                expected_ready = operation.get("expect", [])
                if ready != [str(call_id) for call_id in expected_ready or ()]:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionReadyMismatch",
                            "message": "tool-execution ready call ids did not match expected result",
                            "path": f"$.operations[{operation_index}].expect",
                        }
                    )
            elif op == "start":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_started(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "start", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution start operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "complete":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_completed(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {
                        "op": "complete",
                        "toolCallId": tool_call_id,
                        "error": actual_error,
                    }
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution complete operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "fail":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_failed(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "fail", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution fail operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "deny":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_denied(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "deny", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution deny operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "expire":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_expired(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "expire", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution expire operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "cancel":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_cancelled(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {"op": "cancel", "toolCallId": tool_call_id, "error": actual_error}
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution cancel operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "policy_stopped":
                tool_call_id = str(
                    operation.get("toolCallId", operation.get("tool_call_id", ""))
                )
                expected_error = operation.get("expectError")
                actual_error = None
                try:
                    plan.record_policy_stopped(tool_call_id)
                except ToolExecutionPlanError as error:
                    actual_error = _tool_execution_error_code(error)
                operation_observations.append(
                    {
                        "op": "policy_stopped",
                        "toolCallId": tool_call_id,
                        "error": actual_error,
                    }
                )
                if expected_error is not None:
                    if actual_error != expected_error:
                        diagnostics.append(
                            {
                                "code": "ToolExecutionOperationErrorMismatch",
                                "message": "tool-execution operation error did not match expected result",
                                "path": f"$.operations[{operation_index}].expectError",
                            }
                        )
                elif actual_error is not None:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionOperationUnexpectedError",
                            "message": "tool-execution policy_stopped operation failed unexpectedly",
                            "path": f"$.operations[{operation_index}]",
                        }
                    )
            elif op == "policy_stop":
                pending_tool_calls = str(operation.get("pendingToolCalls", "deny"))
                affected = plan.apply_policy_stop(pending_tool_calls)
                operation_observations.append(
                    {
                        "op": "policy_stop",
                        "pendingToolCalls": pending_tool_calls,
                        "affected": affected,
                    }
                )
                expected_affected = operation.get("expectAffected", [])
                if affected != [
                    str(tool_call_id) for tool_call_id in expected_affected or ()
                ]:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionPolicyStopAffectedMismatch",
                            "message": "tool-execution policy_stop affected calls did not match expected result",
                            "path": f"$.operations[{operation_index}].expectAffected",
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "code": "ToolExecutionOperationUnknown",
                        "message": f"tool-execution TCK operation {op!r} is not supported",
                        "path": f"$.operations[{operation_index}].op",
                    }
                )

        expected_states = fixture.get("expectedStates", {})
        observed_states = {
            planned_call.call.tool_call_id: plan.state(planned_call.call.tool_call_id)
            for planned_call in planned_calls
        }
        if isinstance(expected_states, Mapping):
            for tool_call_id, expected_state in expected_states.items():
                if observed_states.get(str(tool_call_id)) != expected_state:
                    diagnostics.append(
                        {
                            "code": "ToolExecutionStateMismatch",
                            "message": "tool-execution final state did not match expected result",
                            "path": f"$.expectedStates.{tool_call_id}",
                        }
                    )
        else:
            diagnostics.append(
                {
                    "code": "ToolExecutionExpectedStatesInvalid",
                    "message": "tool-execution expectedStates must be a mapping",
                    "path": "$.expectedStates",
                }
            )
        observed["operations"] = operation_observations
        observed["states"] = observed_states
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_usage_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.usage_fixture
        ledger = InMemoryUsageLedger()
        append_results: list[str] = []
        observed_errors: list[dict[str, object]] = []
        operations = fixture.get("operations", [])
        if not isinstance(operations, list):
            operations = []
            diagnostics.append(
                {
                    "code": "UsageOperationsInvalid",
                    "message": "usage TCK operations must be a list",
                    "path": "$.operations",
                }
            )

        for operation_index, operation in enumerate(operations):
            if not isinstance(operation, Mapping):
                diagnostics.append(
                    {
                        "code": "UsageOperationInvalid",
                        "message": "usage TCK operation must be a mapping",
                        "path": f"$.operations[{operation_index}]",
                    }
                )
                continue
            op = str(operation.get("op", ""))
            expected_error_value = operation.get("expectError")
            if expected_error_value is not None and not isinstance(
                expected_error_value, str
            ):
                diagnostics.append(
                    {
                        "code": "UsageExpectedErrorInvalid",
                        "message": "usage operation expectError must be a string",
                        "path": f"$.operations[{operation_index}].expectError",
                    }
                )
                continue
            expected_error = (
                expected_error_value if isinstance(expected_error_value, str) else None
            )
            actual_error: str | None = None
            try:
                if op == "append":
                    record_mapping = operation.get("record")
                    if not isinstance(record_mapping, Mapping):
                        diagnostics.append(
                            {
                                "code": "UsageRecordMissing",
                                "message": "append operation requires a usage record",
                                "path": f"$.operations[{operation_index}].record",
                            }
                        )
                        continue
                    raw_amounts = record_mapping.get("amounts", [])
                    if not isinstance(raw_amounts, list):
                        diagnostics.append(
                            {
                                "code": "UsageAmountsInvalid",
                                "message": "usage record amounts must be a list",
                                "path": f"$.operations[{operation_index}].record.amounts",
                            }
                        )
                        continue
                    amounts: list[UsageAmount] = []
                    for amount_index, amount in enumerate(raw_amounts):
                        if not isinstance(amount, Mapping):
                            diagnostics.append(
                                {
                                    "code": "UsageAmountInvalid",
                                    "message": "usage amount must be a mapping",
                                    "path": f"$.operations[{operation_index}].record.amounts[{amount_index}]",
                                }
                            )
                            continue
                        dimensions = amount.get("dimensions", {})
                        if not isinstance(dimensions, Mapping):
                            diagnostics.append(
                                {
                                    "code": "UsageAmountInvalid",
                                    "message": "usage amount dimensions must be a mapping",
                                    "path": f"$.operations[{operation_index}].record.amounts[{amount_index}].dimensions",
                                }
                            )
                            continue
                        amounts.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=Decimal(str(amount.get("amount", "0"))),
                                unit=str(amount.get("unit", "")),
                                dimensions={
                                    str(key): str(value)
                                    for key, value in dimensions.items()
                                },
                            )
                        )
                    metadata = record_mapping.get("metadata", {})
                    if not isinstance(metadata, Mapping):
                        diagnostics.append(
                            {
                                "code": "UsageMetadataInvalid",
                                "message": "usage record metadata must be a mapping",
                                "path": f"$.operations[{operation_index}].record.metadata",
                            }
                        )
                        continue
                    optional_fields: dict[str, object] = {}
                    for field_name, keys in (
                        ("run_id", ("runId", "run_id")),
                        ("attempt_id", ("attemptId", "attempt_id")),
                        (
                            "provider_response_id",
                            ("providerResponseId", "provider_response_id"),
                        ),
                        ("pricing_ref", ("pricingRef", "pricing_ref")),
                        ("quota_window_id", ("quotaWindowId", "quota_window_id")),
                        ("execution_scope", ("executionScope", "execution_scope")),
                        (
                            "reconciliation_of",
                            ("reconciliationOf", "reconciliation_of"),
                        ),
                    ):
                        value = _first_mapping_value(record_mapping, *keys)
                        if value is not None:
                            optional_fields[field_name] = str(value)
                    record = UsageRecord(
                        record_id=str(
                            _first_mapping_value(
                                record_mapping, "recordId", "record_id"
                            )
                        ),
                        source=str(record_mapping.get("source", "")),
                        confidence=str(record_mapping.get("confidence", "")),
                        amounts=tuple(amounts),
                        occurred_at=str(
                            _first_mapping_value(
                                record_mapping, "occurredAt", "occurred_at"
                            )
                        ),
                        metadata={str(key): value for key, value in metadata.items()},
                        **optional_fields,
                    )
                    append_results.append(ledger.append(record).record_id)
                elif op == "reconcile":
                    raw_amounts = operation.get("amounts", [])
                    if not isinstance(raw_amounts, list):
                        diagnostics.append(
                            {
                                "code": "UsageAmountsInvalid",
                                "message": "usage reconcile amounts must be a list",
                                "path": f"$.operations[{operation_index}].amounts",
                            }
                        )
                        continue
                    amounts = []
                    for amount_index, amount in enumerate(raw_amounts):
                        if not isinstance(amount, Mapping):
                            diagnostics.append(
                                {
                                    "code": "UsageAmountInvalid",
                                    "message": "usage amount must be a mapping",
                                    "path": f"$.operations[{operation_index}].amounts[{amount_index}]",
                                }
                            )
                            continue
                        dimensions = amount.get("dimensions", {})
                        if not isinstance(dimensions, Mapping):
                            diagnostics.append(
                                {
                                    "code": "UsageAmountInvalid",
                                    "message": "usage amount dimensions must be a mapping",
                                    "path": f"$.operations[{operation_index}].amounts[{amount_index}].dimensions",
                                }
                            )
                            continue
                        amounts.append(
                            UsageAmount(
                                kind=str(amount.get("kind", "")),
                                amount=Decimal(str(amount.get("amount", "0"))),
                                unit=str(amount.get("unit", "")),
                                dimensions={
                                    str(key): str(value)
                                    for key, value in dimensions.items()
                                },
                            )
                        )
                    reconciled = ledger.reconcile(
                        str(
                            _first_mapping_value(
                                operation, "sourceRecordId", "source_record_id"
                            )
                        ),
                        amounts=amounts,
                        occurred_at=str(
                            _first_mapping_value(operation, "occurredAt", "occurred_at")
                        ),
                        record_id=(
                            str(
                                _first_mapping_value(operation, "recordId", "record_id")
                            )
                            if _first_mapping_value(operation, "recordId", "record_id")
                            is not None
                            else None
                        ),
                    )
                    append_results.append(reconciled.record_id)
                else:
                    actual_error = f"usage TCK operation {op!r} is not supported"
            except Exception as error:
                actual_error = str(error)

            if actual_error is not None:
                observed_errors.append(
                    {"operation": operation_index, "message": actual_error}
                )
            if expected_error is not None:
                if actual_error != expected_error:
                    diagnostics.append(
                        {
                            "code": "UsageOperationExpectedErrorMismatch",
                            "message": "usage operation error did not match expected error",
                            "path": f"$.operations[{operation_index}].expectError",
                        }
                    )
            elif actual_error is not None:
                diagnostics.append(
                    {
                        "code": (
                            "UsageOperationUnknown"
                            if op not in {"append", "reconcile"}
                            else "UsageOperationUnexpectedError"
                        ),
                        "message": actual_error,
                        "path": f"$.operations[{operation_index}]",
                    }
                )

        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "UsageExpectedInvalid",
                    "message": "usage TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )
        run_id = str(expected.get("runId", expected.get("run_id", "")))
        records = ledger.records_for_run(run_id) if run_id else []
        totals = ledger.totals_for_run(run_id) if run_id else []
        observed_totals = [
            {
                "kind": amount.kind,
                "amount": (
                    int(amount.amount)
                    if amount.amount == amount.amount.to_integral_value()
                    else str(amount.amount)
                ),
                "unit": amount.unit,
                "dimensions": dict(amount.dimensions),
            }
            for amount in totals
        ]
        observed = {
            "appendResults": append_results,
            "recordIds": [record.record_id for record in records],
            "totals": observed_totals,
            "errors": observed_errors,
        }
        for key, path in (
            ("appendResults", "$.expected.appendResults"),
            ("recordIds", "$.expected.recordIds"),
            ("totals", "$.expected.totals"),
        ):
            expected_value = expected.get(key)
            if expected_value is not None and observed[key] != expected_value:
                diagnostics.append(
                    {
                        "code": "UsageExpectedMismatch",
                        "message": f"usage observed {key} did not match expected value",
                        "path": path,
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_voice_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        fixture = case.voice_fixture
        kind = str(fixture.get("kind", ""))
        expected = fixture.get("expected", {})
        if not isinstance(expected, Mapping):
            expected = {}
            diagnostics.append(
                {
                    "code": "VoiceExpectedInvalid",
                    "message": "voice TCK expected result must be a mapping",
                    "path": "$.expected",
                }
            )

        try:
            voice = importlib.import_module("graphblocks.voice")
        except ModuleNotFoundError as error:
            diagnostics.append(
                {
                    "code": "VoicePackageMissing",
                    "message": str(error),
                    "path": "$",
                }
            )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="failed",
                diagnostics=tuple(diagnostics),
                observed={},
            )

        observed: dict[str, object] = {}
        try:
            if kind == "session_request":
                raw_transport = fixture.get("transport", {})
                raw_session = fixture.get("session", {})
                raw_request = fixture.get("request", {})
                if (
                    not isinstance(raw_transport, Mapping)
                    or not isinstance(raw_session, Mapping)
                    or not isinstance(raw_request, Mapping)
                ):
                    raise ValueError(
                        "voice session_request case requires transport, session, and request"
                    )
                metadata = raw_session.get("metadata", {})
                if not isinstance(metadata, Mapping):
                    metadata = {}
                transport = voice.VoiceTransport(
                    str(raw_transport.get("kind", "")),
                    uri=(
                        str(raw_transport.get("uri"))
                        if raw_transport.get("uri") is not None
                        else None
                    ),
                    codec=str(raw_transport.get("codec", "pcm16")),
                    sample_rate_hz=int(
                        raw_transport.get(
                            "sampleRateHz", raw_transport.get("sample_rate_hz", 24_000)
                        )
                    ),
                    channels=int(raw_transport.get("channels", 1)),
                )
                session = voice.DuplexSession(
                    str(
                        raw_session.get("sessionId", raw_session.get("session_id", ""))
                    ),
                    transport,
                    started_at_ms=int(
                        raw_session.get(
                            "startedAtMs", raw_session.get("started_at_ms", 0)
                        )
                    ),
                    metadata={str(key): str(value) for key, value in metadata.items()},
                ).begin_turn(
                    str(raw_session.get("turnId", raw_session.get("turn_id", "")))
                )
                raw_modalities = raw_request.get("modalities", ())
                if not isinstance(raw_modalities, list):
                    raw_modalities = []
                request = voice.RealtimeSessionRequest(
                    session=session,
                    model=str(raw_request.get("model", "")),
                    instructions=str(raw_request.get("instructions", "")),
                    modalities=tuple(str(item) for item in raw_modalities),
                )
                raw_tools = raw_request.get("tools", [])
                if not isinstance(raw_tools, list):
                    raw_tools = []
                for tool_name in raw_tools:
                    request = request.with_tool(str(tool_name))
                contract = request.provider_contract()
                observed = {
                    "sessionState": session.state,
                    "currentTurnId": session.current_turn_id,
                    "contractSessionId": contract["sessionId"],
                    "transportKind": contract["transport"]["kind"],
                    "transportSampleRateHz": contract["transport"]["sampleRateHz"],
                    "modalities": contract["modalities"],
                    "tools": contract["tools"],
                }
            elif kind == "vad_interruption":
                raw_authority = fixture.get("authority", {})
                raw_frames = fixture.get("frames", [])
                raw_playback = fixture.get("playback", [])
                raw_classifier = fixture.get("classifier", {})
                if (
                    not isinstance(raw_authority, Mapping)
                    or not isinstance(raw_frames, list)
                    or not isinstance(raw_playback, list)
                    or not isinstance(raw_classifier, Mapping)
                ):
                    raise ValueError(
                        "voice vad_interruption case requires authority, frames, playback, and classifier"
                    )
                authority = voice.VadAuthority(
                    str(
                        raw_authority.get(
                            "authorityId", raw_authority.get("authority_id", "")
                        )
                    ),
                    speech_threshold=float(
                        raw_authority.get(
                            "speechThreshold",
                            raw_authority.get("speech_threshold", 0.5),
                        )
                    ),
                )
                decisions = []
                for raw_frame in raw_frames:
                    if not isinstance(raw_frame, Mapping):
                        raise ValueError("voice frame must be a mapping")
                    decisions.append(
                        authority.evaluate(
                            voice.AudioFrame(
                                str(
                                    raw_frame.get(
                                        "streamId", raw_frame.get("stream_id", "")
                                    )
                                ),
                                sequence=int(raw_frame.get("sequence", 0)),
                                start_ms=int(
                                    raw_frame.get(
                                        "startMs", raw_frame.get("start_ms", 0)
                                    )
                                ),
                                duration_ms=int(
                                    raw_frame.get(
                                        "durationMs", raw_frame.get("duration_ms", 0)
                                    )
                                ),
                                speech_probability=float(
                                    raw_frame.get(
                                        "speechProbability",
                                        raw_frame.get("speech_probability", 0),
                                    )
                                ),
                            ),
                            already_in_speech=bool(
                                raw_frame.get(
                                    "alreadyInSpeech",
                                    raw_frame.get("already_in_speech", False),
                                )
                            ),
                        )
                    )
                playback = voice.PlaybackLedger()
                for raw_entry in raw_playback:
                    if not isinstance(raw_entry, Mapping):
                        raise ValueError("voice playback entry must be a mapping")
                    playback = playback.append(
                        voice.PlaybackEntry(
                            str(
                                raw_entry.get(
                                    "playbackId", raw_entry.get("playback_id", "")
                                )
                            ),
                            sequence=int(raw_entry.get("sequence", 0)),
                            status=str(raw_entry.get("status", "")),
                            audio_ref=(
                                str(
                                    raw_entry.get(
                                        "audioRef", raw_entry.get("audio_ref")
                                    )
                                )
                                if raw_entry.get("audioRef", raw_entry.get("audio_ref"))
                                is not None
                                else None
                            ),
                            started_at_ms=(
                                int(
                                    raw_entry.get(
                                        "startedAtMs", raw_entry.get("started_at_ms")
                                    )
                                )
                                if raw_entry.get(
                                    "startedAtMs", raw_entry.get("started_at_ms")
                                )
                                is not None
                                else None
                            ),
                            completed_at_ms=(
                                int(
                                    raw_entry.get(
                                        "completedAtMs",
                                        raw_entry.get("completed_at_ms"),
                                    )
                                )
                                if raw_entry.get(
                                    "completedAtMs", raw_entry.get("completed_at_ms")
                                )
                                is not None
                                else None
                            ),
                            reason=(
                                str(raw_entry.get("reason"))
                                if raw_entry.get("reason") is not None
                                else None
                            ),
                        )
                    )
                decision = voice.InterruptionClassifier(
                    str(
                        raw_classifier.get(
                            "classifierId", raw_classifier.get("classifier_id", "")
                        )
                    ),
                    provider_authority_id=str(
                        raw_classifier.get(
                            "providerAuthorityId",
                            raw_classifier.get("provider_authority_id", "provider"),
                        )
                    ),
                ).classify(
                    session_id=str(
                        raw_classifier.get(
                            "sessionId", raw_classifier.get("session_id", "")
                        )
                    ),
                    vad_decision=decisions[-1],
                    playback=playback,
                    occurred_at_ms=int(
                        raw_classifier.get(
                            "occurredAtMs", raw_classifier.get("occurred_at_ms", 0)
                        )
                    ),
                    provider_decision=(
                        voice.ProviderInterruptionDecision(
                            authority_id=str(
                                raw_classifier["providerDecision"].get(
                                    "authorityId",
                                    raw_classifier["providerDecision"].get(
                                        "authority_id", ""
                                    ),
                                )
                            ),
                            session_id=str(
                                raw_classifier["providerDecision"].get(
                                    "sessionId",
                                    raw_classifier["providerDecision"].get(
                                        "session_id", ""
                                    ),
                                )
                            ),
                            kind=str(
                                raw_classifier["providerDecision"].get("kind", "")
                            ),
                            occurred_at_ms=int(
                                raw_classifier["providerDecision"].get(
                                    "occurredAtMs",
                                    raw_classifier["providerDecision"].get(
                                        "occurred_at_ms", 0
                                    ),
                                )
                            ),
                            reason=(
                                str(raw_classifier["providerDecision"].get("reason"))
                                if raw_classifier["providerDecision"].get("reason")
                                is not None
                                else None
                            ),
                        )
                        if isinstance(raw_classifier.get("providerDecision"), Mapping)
                        else None
                    ),
                )
                observed = {
                    "decisionKinds": [decision.kind for decision in decisions],
                    "interruptionKind": decision.kind,
                    "interruptedPlaybackIds": list(decision.interrupted_playback_ids),
                    "interruptionReason": decision.reason,
                }
            elif kind == "playback_interrupt":
                raw_entries = fixture.get("entries", [])
                raw_interrupt = fixture.get("interrupt", {})
                if not isinstance(raw_entries, list) or not isinstance(
                    raw_interrupt, Mapping
                ):
                    raise ValueError(
                        "voice playback_interrupt case requires entries and interrupt"
                    )
                ledger = voice.PlaybackLedger()
                for raw_entry in raw_entries:
                    if not isinstance(raw_entry, Mapping):
                        raise ValueError("voice playback entry must be a mapping")
                    ledger = ledger.append(
                        voice.PlaybackEntry(
                            str(
                                raw_entry.get(
                                    "playbackId", raw_entry.get("playback_id", "")
                                )
                            ),
                            sequence=int(raw_entry.get("sequence", 0)),
                            status=str(raw_entry.get("status", "")),
                            audio_ref=(
                                str(
                                    raw_entry.get(
                                        "audioRef", raw_entry.get("audio_ref")
                                    )
                                )
                                if raw_entry.get("audioRef", raw_entry.get("audio_ref"))
                                is not None
                                else None
                            ),
                            started_at_ms=(
                                int(
                                    raw_entry.get(
                                        "startedAtMs", raw_entry.get("started_at_ms")
                                    )
                                )
                                if raw_entry.get(
                                    "startedAtMs", raw_entry.get("started_at_ms")
                                )
                                is not None
                                else None
                            ),
                            completed_at_ms=(
                                int(
                                    raw_entry.get(
                                        "completedAtMs",
                                        raw_entry.get("completed_at_ms"),
                                    )
                                )
                                if raw_entry.get(
                                    "completedAtMs", raw_entry.get("completed_at_ms")
                                )
                                is not None
                                else None
                            ),
                            reason=(
                                str(raw_entry.get("reason"))
                                if raw_entry.get("reason") is not None
                                else None
                            ),
                        )
                    )
                active_before = list(ledger.active_playback_ids())
                interrupted = ledger.interrupt_active(
                    occurred_at_ms=int(
                        raw_interrupt.get(
                            "occurredAtMs", raw_interrupt.get("occurred_at_ms", 0)
                        )
                    ),
                    reason=str(raw_interrupt.get("reason", "")),
                )
                observed = {
                    "activeBefore": active_before,
                    "statuses": [entry.status for entry in interrupted.entries],
                    "completedAtMs": [
                        entry.completed_at_ms for entry in interrupted.entries
                    ],
                    "reasons": [entry.reason for entry in interrupted.entries],
                    "digest": interrupted.content_digest(),
                }
            elif kind == "validation_errors":
                raw_transport = fixture.get(
                    "invalidTransport", fixture.get("invalid_transport", {})
                )
                raw_session = fixture.get(
                    "invalidSession", fixture.get("invalid_session", {})
                )
                raw_frame = fixture.get(
                    "invalidFrame", fixture.get("invalid_frame", {})
                )
                if (
                    not isinstance(raw_transport, Mapping)
                    or not isinstance(raw_session, Mapping)
                    or not isinstance(raw_frame, Mapping)
                ):
                    raise ValueError(
                        "voice validation_errors case requires invalidTransport, invalidSession, and invalidFrame"
                    )
                transport_error = None
                session_error = None
                frame_error = None
                try:
                    voice.VoiceTransport(
                        str(raw_transport.get("kind", "")),
                        uri=(
                            str(raw_transport.get("uri"))
                            if raw_transport.get("uri") is not None
                            else None
                        ),
                        sample_rate_hz=int(
                            raw_transport.get(
                                "sampleRateHz",
                                raw_transport.get("sample_rate_hz", 24_000),
                            )
                        ),
                    )
                except voice.VoiceContractError:
                    transport_error = "voice_contract_error"
                try:
                    voice.DuplexSession(
                        str(
                            raw_session.get(
                                "sessionId", raw_session.get("session_id", "")
                            )
                        ),
                        voice.VoiceTransport.websocket(
                            "wss://voice.example.com/session"
                        ),
                        started_at_ms=int(
                            raw_session.get(
                                "startedAtMs", raw_session.get("started_at_ms", 0)
                            )
                        ),
                    ).close(
                        occurred_at_ms=int(
                            raw_session.get(
                                "closeAtMs", raw_session.get("close_at_ms", 0)
                            )
                        )
                    )
                except voice.VoiceContractError:
                    session_error = "voice_contract_error"
                try:
                    voice.AudioFrame(
                        str(raw_frame.get("streamId", raw_frame.get("stream_id", ""))),
                        sequence=int(raw_frame.get("sequence", 0)),
                        start_ms=int(
                            raw_frame.get("startMs", raw_frame.get("start_ms", 0))
                        ),
                        duration_ms=int(
                            raw_frame.get("durationMs", raw_frame.get("duration_ms", 0))
                        ),
                        speech_probability=float(
                            raw_frame.get(
                                "speechProbability",
                                raw_frame.get("speech_probability", 0),
                            )
                        ),
                    )
                except voice.VoiceContractError:
                    frame_error = "voice_contract_error"
                observed = {
                    "transportError": transport_error,
                    "sessionError": session_error,
                    "frameError": frame_error,
                }
            else:
                diagnostics.append(
                    {
                        "code": "VoiceKindUnknown",
                        "message": f"voice TCK kind {kind!r} is not supported",
                        "path": "$.kind",
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "VoiceExecutionError",
                    "message": str(error),
                    "path": "$",
                }
            )

        for key, expected_value in expected.items():
            if observed.get(str(key)) != expected_value:
                diagnostics.append(
                    {
                        "code": "VoiceExpectedMismatch",
                        "message": f"voice observed {key} did not match expected value",
                        "path": f"$.expected.{key}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_policy_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        delivery = case.policy_delivery
        mode = delivery.get("mode", "bounded_holdback")
        if mode == "buffer_until_commit":
            policy = OutputDeliveryPolicy.buffer_until_commit(
                on_violation="abort_response"
            )
        elif mode == "bounded_holdback":
            policy = OutputDeliveryPolicy.bounded_holdback(
                on_violation="abort_response"
            )
        elif mode == "immediate_draft":
            policy = OutputDeliveryPolicy.immediate_draft(
                on_violation="abort_response",
                delivered_draft_disposition="retract",
            )
        else:
            policy = OutputDeliveryPolicy.bounded_holdback(
                on_violation="abort_response"
            )
            diagnostics.append(
                {
                    "code": "PolicyDeliveryModeUnknown",
                    "message": f"policy TCK delivery mode {mode!r} is not supported",
                    "path": "$.delivery.mode",
                }
            )
        for source, target in (
            ("holdbackMaxTokens", "holdback_max_tokens"),
            ("holdbackMaxBytes", "holdback_max_bytes"),
            ("holdbackMaxDurationMs", "holdback_max_duration_ms"),
        ):
            value = delivery.get(source)
            if value is not None:
                policy = replace(policy, **{target: value})
        flush_boundaries = delivery.get(
            "flushBoundaries", delivery.get("flush_boundaries")
        )
        if flush_boundaries is not None:
            if isinstance(flush_boundaries, list) and all(
                isinstance(boundary, str) for boundary in flush_boundaries
            ):
                policy = replace(policy, flush_boundaries=frozenset(flush_boundaries))
            else:
                diagnostics.append(
                    {
                        "code": "PolicyFlushBoundariesInvalid",
                        "message": "policy TCK flush boundaries must be a list of strings",
                        "path": "$.delivery.flushBoundaries",
                    }
                )
        gate = OutputDeliveryGate(
            case.policy_stream_id, case.policy_response_id, delivery_policy=policy
        )

        for operation_index, operation in enumerate(case.policy_operations):
            op = operation.get("op")
            expected_error = operation.get("expectError")
            actual_error = None
            try:
                decision: OutputPolicyDecision | None = None
                expected_cutoff: object = None
                expected_update: object = operation.get(
                    "expectedUpdate", operation.get("expected_update")
                )
                if op == "chunk":
                    result = gate.record_chunk(
                        GenerationChunk.text(
                            case.policy_stream_id,
                            case.policy_response_id,
                            int(operation.get("sequence", -1)),
                            str(operation.get("text", "")),
                        )
                    )
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in result]
                elif op == "allow":
                    accepted_through = operation.get("acceptedThrough")
                    decision = OutputPolicyDecision.allow(
                        str(operation.get("decisionId", "")),
                        accepted_through_sequence=int(accepted_through)
                        if accepted_through is not None
                        else None,
                        input_digest=str(operation.get("inputDigest", "")),
                    )
                elif op == "hold":
                    decision = OutputPolicyDecision.hold(
                        str(operation.get("decisionId", "")),
                        input_digest=str(operation.get("inputDigest", "")),
                    )
                elif op in {"redact", "replace"}:
                    accepted_through = operation.get("acceptedThrough")
                    accepted_sequence = (
                        int(accepted_through) if accepted_through is not None else None
                    )
                    if op == "redact":
                        redactions = []
                        for redaction in operation.get("redactions", []):
                            if isinstance(redaction, Mapping):
                                redactions.append(
                                    {
                                        "path": str(redaction.get("path", "")),
                                        "start": int(redaction.get("start", -1)),
                                        "end": int(redaction.get("end", -1)),
                                        "replacement": str(
                                            redaction.get("replacement", "")
                                        ),
                                    }
                                )
                        decision = OutputPolicyDecision.redact(
                            str(operation.get("decisionId", "")),
                            accepted_through_sequence=accepted_sequence,
                            redactions=tuple(redactions),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    else:
                        replacement_parts = []
                        for chunk in operation.get("replacementChunks", []):
                            if isinstance(chunk, Mapping):
                                replacement_parts.append(
                                    ContentPart(
                                        kind="text", text=str(chunk.get("text", ""))
                                    )
                                )
                        decision = OutputPolicyDecision.replace(
                            str(operation.get("decisionId", "")),
                            accepted_through_sequence=accepted_sequence,
                            replacement_parts=tuple(replacement_parts),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                elif op in {"abort_response", "abort_turn", "deny_commit"}:
                    if op == "abort_turn":
                        decision = OutputPolicyDecision.abort_turn(
                            str(operation.get("decisionId", "")),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    elif op == "deny_commit":
                        decision = OutputPolicyDecision.deny_commit(
                            str(operation.get("decisionId", "")),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    else:
                        decision = OutputPolicyDecision.abort_response(
                            str(operation.get("decisionId", "")),
                            input_digest=str(operation.get("inputDigest", "")),
                        )
                    accepted_through = operation.get("acceptedThrough")
                    if accepted_through is not None:
                        decision = decision.with_accepted_through_sequence(
                            int(accepted_through)
                        )
                    provider_cancellation = operation.get("providerCancellation")
                    if isinstance(provider_cancellation, str):
                        decision = decision.with_provider_cancellation(
                            provider_cancellation
                        )
                    draft_disposition = operation.get("draftDisposition")
                    if isinstance(draft_disposition, str):
                        decision = decision.with_draft_disposition(draft_disposition)
                    pending_tool_calls = operation.get("pendingToolCalls")
                    if isinstance(pending_tool_calls, str):
                        decision = decision.with_pending_tool_calls(pending_tool_calls)
                    expected_cutoff = operation.get("cutoff")
                elif op == "commit":
                    result = gate.commit_accepted_output()
                    actual_deliver = [(chunk.sequence, chunk.text) for chunk in result]
                else:
                    diagnostics.append(
                        {
                            "code": "PolicyOperationUnknown",
                            "message": f"policy TCK operation {op!r} is not supported",
                            "path": f"$.operations[{operation_index}].op",
                        }
                    )
                    continue
                if decision is not None:
                    evaluated_at = operation.get(
                        "evaluatedAt", operation.get("evaluated_at")
                    )
                    if evaluated_at is not None:
                        if isinstance(evaluated_at, bool):
                            raise ValueError("invalid_evaluated_at_unix_ms")
                        if isinstance(evaluated_at, int):
                            if evaluated_at <= 0:
                                raise ValueError("invalid_evaluated_at_unix_ms")
                            evaluated_at = (
                                datetime.fromtimestamp(
                                    evaluated_at / 1000,
                                    tz=timezone.utc,
                                )
                                .isoformat(timespec="milliseconds")
                                .replace("+00:00", "Z")
                            )
                        elif not isinstance(evaluated_at, str):
                            raise ValueError("invalid_evaluated_at_unix_ms")
                        decision = decision.evaluated_at_time(evaluated_at)
                    occurred_at = operation.get("occurredAt", "")
                    if isinstance(occurred_at, bool):
                        raise ValueError("missing_occurred_at_unix_ms")
                    if isinstance(occurred_at, int):
                        if occurred_at <= 0:
                            raise ValueError("missing_occurred_at_unix_ms")
                        occurred_at = (
                            datetime.fromtimestamp(
                                occurred_at / 1000,
                                tz=timezone.utc,
                            )
                            .isoformat(timespec="milliseconds")
                            .replace("+00:00", "Z")
                        )
                    elif not isinstance(occurred_at, str):
                        raise ValueError("missing_occurred_at_unix_ms")
                    update = gate.apply_decision(decision, occurred_at=occurred_at)
                    actual_deliver = [
                        (chunk.sequence, chunk.text) for chunk in update.deliverable
                    ]
                    if isinstance(expected_update, Mapping):
                        update_fields = {
                            "pendingToolCalls": update.pending_tool_calls,
                            "providerCancellation": update.provider_cancellation,
                        }
                        for field_name, actual_value in update_fields.items():
                            if (
                                field_name in expected_update
                                and expected_update[field_name] != actual_value
                            ):
                                diagnostics.append(
                                    {
                                        "code": "PolicyUpdateMismatch",
                                        "message": "policy TCK update field did not match expected value",
                                        "path": f"$.operations[{operation_index}].expectedUpdate.{field_name}",
                                    }
                                )
                    if isinstance(expected_cutoff, Mapping):
                        if update.cutoff is None:
                            diagnostics.append(
                                {
                                    "code": "PolicyCutoffMissing",
                                    "message": "policy TCK operation expected an output cutoff",
                                    "path": f"$.operations[{operation_index}].cutoff",
                                }
                            )
                        else:
                            cutoff_fields = {
                                "lastGeneratedSequence": update.cutoff.last_generated_sequence,
                                "lastPolicyAcceptedSequence": update.cutoff.last_policy_accepted_sequence,
                                "lastClientDeliveredSequence": update.cutoff.last_client_delivered_sequence,
                                "terminalReason": update.cutoff.terminal_reason,
                                "draftDisposition": update.cutoff.draft_disposition,
                                "durableResult": update.cutoff.durable_result,
                                "policyDecisionId": update.cutoff.policy_decision_id,
                                "occurredAt": update.cutoff.occurred_at,
                            }
                            for field_name, actual_value in cutoff_fields.items():
                                if (
                                    field_name in expected_cutoff
                                    and expected_cutoff[field_name] != actual_value
                                ):
                                    diagnostics.append(
                                        {
                                            "code": "PolicyCutoffMismatch",
                                            "message": "policy TCK cutoff field did not match expected value",
                                            "path": f"$.operations[{operation_index}].cutoff.{field_name}",
                                        }
                                    )
            except (OutputGateError, ValueError) as error:
                message = str(error)
                if message == "output gate is policy stopped":
                    actual_error = "policy_stopped"
                elif message == "invalid_evaluated_at_unix_ms":
                    actual_error = "invalid_evaluated_at_unix_ms"
                elif (
                    message == "missing_occurred_at_unix_ms"
                    or message == "output gate occurred_at must not be empty"
                ):
                    actual_error = "missing_occurred_at_unix_ms"
                elif "exceeds" in message and "bytes" in message:
                    actual_error = "bounded_holdback_bytes"
                elif "exceeds" in message and "tokens" in message:
                    actual_error = "bounded_holdback_tokens"
                elif message.startswith("accepted sequence"):
                    actual_error = "accepted_sequence_beyond_generated"
                elif "must be next after" in message:
                    actual_error = "non_contiguous_sequence"
                elif "must be greater than" in message:
                    actual_error = "non_monotonic_sequence"
                elif message == "generation chunk sequence must be positive":
                    actual_error = "invalid_generation_sequence"
                else:
                    actual_error = type(error).__name__
                actual_deliver = []

            if expected_error is not None:
                if actual_error != expected_error:
                    diagnostics.append(
                        {
                            "code": "PolicyExpectedErrorMismatch",
                            "message": "policy TCK operation error did not match expected error",
                            "path": f"$.operations[{operation_index}].expectError",
                        }
                    )
                continue
            if actual_error is not None:
                diagnostics.append(
                    {
                        "code": "PolicyUnexpectedError",
                        "message": f"policy TCK operation failed with {actual_error}",
                        "path": f"$.operations[{operation_index}]",
                    }
                )
                continue

            expected_deliver = [
                (int(chunk.get("sequence", -1)), str(chunk.get("text", "")))
                for chunk in operation.get("deliver", [])
                if isinstance(chunk, Mapping)
            ]
            if actual_deliver != expected_deliver:
                diagnostics.append(
                    {
                        "code": "PolicyDeliverableMismatch",
                        "message": "policy TCK delivered chunks did not match expected chunks",
                        "path": f"$.operations[{operation_index}].deliver",
                    }
                )

        observed = {
            "lastGeneratedSequence": gate.last_generated_sequence,
            "lastPolicyAcceptedSequence": gate.last_policy_accepted_sequence,
            "lastClientDeliveredSequence": gate.last_client_delivered_sequence,
            "stopped": gate.cutoff is not None,
        }
        for field_name, expected_value in case.expected_gate_state.items():
            if observed.get(field_name) != expected_value:
                diagnostics.append(
                    {
                        "code": "PolicyGateStateMismatch",
                        "message": "policy TCK final gate state did not match expected value",
                        "path": f"$.expected.{field_name}",
                    }
                )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )

    def _run_sequence_case(self, case: TckCase) -> TckResult:
        diagnostics: list[dict[str, str]] = []
        observed: dict[str, object] = {}
        capacity = case.sequence_capacity or 0
        if capacity < 1:
            observed["creation_error"] = "invalid_capacity"
            if case.expected_sequence_creation_error != "invalid_capacity":
                diagnostics.append(
                    {
                        "code": "SequenceCreationErrorMismatch",
                        "message": "sequence creation error did not match expected result",
                        "path": "$.expected.creation_error",
                    }
                )
            return TckResult(
                case_id=case.case_id,
                kind=case.kind,
                status="passed" if not diagnostics else "failed",
                diagnostics=tuple(diagnostics),
                observed=observed,
            )
        if case.expected_sequence_creation_error is not None:
            diagnostics.append(
                {
                    "code": "SequenceCreationUnexpectedSuccess",
                    "message": "sequence TCK case expected a creation error but sequence was created",
                    "path": "$.expected.creation_error",
                }
            )

        state = "open"
        buffer: list[str] = []
        for operation_index, operation in enumerate(case.sequence_operations):
            op = operation.get("op")
            if op == "send":
                if "value" not in operation:
                    diagnostics.append(
                        {
                            "code": "SequenceOperationInvalid",
                            "message": "sequence send operation requires value",
                            "path": f"$.operations[{operation_index}].value",
                        }
                    )
                    actual = "invalid_operation"
                elif state != "open":
                    actual = f"closed_{state}"
                elif len(buffer) >= capacity:
                    actual = "full"
                else:
                    buffer.append(str(operation["value"]))
                    actual = "ok"
                expected = operation.get("expect")
                if expected is not None and actual != expected:
                    diagnostics.append(
                        {
                            "code": "SequenceSendResultMismatch",
                            "message": "sequence send result did not match expected result",
                            "path": f"$.operations[{operation_index}].expect",
                        }
                    )
            elif op == "recv":
                actual_value = buffer.pop(0) if buffer else None
                expected_value = operation.get("value")
                if actual_value != expected_value:
                    diagnostics.append(
                        {
                            "code": "SequenceReceiveValueMismatch",
                            "message": "sequence receive value did not match expected value",
                            "path": f"$.operations[{operation_index}].value",
                        }
                    )
            elif op == "complete":
                if state == "open":
                    state = "completed"
                    actual = "ok"
                else:
                    actual = f"already_terminal_{state}"
                expected = operation.get("expect")
                if expected is not None and actual != expected:
                    diagnostics.append(
                        {
                            "code": "SequenceCompleteResultMismatch",
                            "message": "sequence complete result did not match expected result",
                            "path": f"$.operations[{operation_index}].expect",
                        }
                    )
            else:
                diagnostics.append(
                    {
                        "code": "SequenceOperationUnknown",
                        "message": f"sequence TCK operation {op!r} is not supported",
                        "path": f"$.operations[{operation_index}].op",
                    }
                )
                continue
            expected_len = operation.get("len")
            if expected_len is not None and len(buffer) != expected_len:
                diagnostics.append(
                    {
                        "code": "SequenceLengthMismatch",
                        "message": "sequence buffer length did not match expected length",
                        "path": f"$.operations[{operation_index}].len",
                    }
                )

        observed["state"] = state
        observed["len"] = len(buffer)
        if (
            case.expected_sequence_state is not None
            and state != case.expected_sequence_state
        ):
            diagnostics.append(
                {
                    "code": "SequenceStateMismatch",
                    "message": "sequence final state did not match expected state",
                    "path": "$.expected.state",
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
            if self.profile == "native":
                if case.native_node_outputs:
                    run_id = "tck-" + "".join(
                        character if character.isalnum() else "-"
                        for character in case.case_id.strip()
                    ).strip("-")
                    run_store_path: str | None = None
                    journal_store_path: str | None = None
                    if self.evidence_dir is not None:
                        self.evidence_dir.mkdir(parents=True, exist_ok=True)
                        run_store_path = str(
                            self.evidence_dir / f"{run_id}-runs.sqlite3"
                        )
                        journal_store_path = str(
                            self.evidence_dir / f"{run_id}-journal.sqlite3"
                        )
                    try:
                        native_result = run_native_test_graph(
                            case.graph,
                            case.inputs,
                            case.native_node_outputs,
                            run_id=run_id,
                            run_store_path=run_store_path,
                            journal_store_path=journal_store_path,
                        )
                    except (
                        ImportError,
                        ModuleNotFoundError,
                        RuntimeError,
                    ) as native_error:
                        message = str(native_error)
                        if (
                            isinstance(native_error, (ImportError, ModuleNotFoundError))
                            or "native extension is not built" in message
                            or "native extension is not available" in message
                        ):
                            result = LocalRuntime(self.registry).run(
                                case.graph, case.inputs
                            )
                            observed = {
                                "status": result.status,
                                "outputs": _runtime_mutable_json_like(result.outputs),
                                "terminal_kind": result.journal.terminal_kind,
                                "runtime": "local",
                                "native_fallback_reason": "native_runtime_unavailable",
                            }
                        else:
                            raise
                    else:
                        journal = native_result.get("journal", [])
                        journal_records = journal if isinstance(journal, list) else []
                        terminal_kind = next(
                            (
                                record.get("kind")
                                for record in reversed(journal_records)
                                if isinstance(record, Mapping)
                                and bool(record.get("terminal"))
                            ),
                            None,
                        )
                        if terminal_kind is None:
                            terminal_kind = next(
                                (
                                    record.get("kind")
                                    for record in reversed(journal_records)
                                    if isinstance(record, Mapping)
                                    and isinstance(record.get("kind"), str)
                                    and str(record.get("kind")).startswith("run_")
                                ),
                                None,
                            )
                        observed = {
                            "status": native_result.get("status"),
                            "outputs": native_result.get("outputs", {}),
                            "terminal_kind": terminal_kind,
                            "run_id": native_result.get(
                                "runId", native_result.get("run_id", run_id)
                            ),
                            "runtime": "native",
                            "journal_kinds": [
                                record["kind"]
                                for record in journal_records
                                if isinstance(record, Mapping)
                                and isinstance(record.get("kind"), str)
                            ],
                        }
                        if run_store_path is not None:
                            observed["run_store_path"] = run_store_path
                        if journal_store_path is not None:
                            observed["journal_store_path"] = journal_store_path
                else:
                    result = LocalRuntime(self.registry).run(case.graph, case.inputs)
                    observed = {
                        "status": result.status,
                        "outputs": _runtime_mutable_json_like(result.outputs),
                        "terminal_kind": result.journal.terminal_kind,
                        "runtime": "local",
                        "native_fallback_reason": "missing_native_node_outputs",
                    }
            else:
                result = LocalRuntime(self.registry).run(case.graph, case.inputs)
                if not isinstance(result, LocalRunResult) or not isinstance(
                    result.journal, LocalExecutionJournal
                ):
                    raise RuntimeError(
                        "local runtime TCK cases must use the stable C1 facade"
                    )
                observed = {
                    "status": result.status,
                    "outputs": _runtime_mutable_json_like(result.outputs),
                    "terminal_kind": result.journal.terminal_kind,
                    "runtime": "local",
                    "runtime_facade": "LocalRuntime",
                    "result_facade": "LocalRunResult",
                    "journal_facade": "LocalExecutionJournal",
                }
        except (
            Exception
        ) as error:  # pragma: no cover - exercised by conformance fixtures.
            observed = {
                "status": "error",
                "error": type(error).__name__,
                "message": str(error),
            }
        diagnostics: list[dict[str, str]] = []
        if observed.get("status") != case.expected_status:
            diagnostics.append(
                {
                    "code": "StatusMismatch",
                    "message": "runtime status did not match expected status",
                    "path": "$.expected_status",
                }
            )
        if (
            case.expected_outputs is not None
            and observed.get("outputs") != case.expected_outputs
        ):
            diagnostics.append(
                {
                    "code": "OutputMismatch",
                    "message": "runtime outputs did not match expected outputs",
                    "path": "$.expected_outputs",
                }
            )
        if (
            case.expected_terminal_kind is not None
            and observed.get("terminal_kind") != case.expected_terminal_kind
        ):
            diagnostics.append(
                {
                    "code": "TerminalKindMismatch",
                    "message": "runtime terminal kind did not match expected terminal kind",
                    "path": "$.expected_terminal_kind",
                }
            )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="passed" if not diagnostics else "failed",
            diagnostics=tuple(diagnostics),
            observed=observed,
        )


class _NormativeCompilerTckRunner(TckRunner):
    __slots__ = ()

    def _compile_compiler_graph(
        self,
        document: dict[str, object],
        block_catalog: BlockCatalog,
        *,
        allow_unknown_blocks: bool,
    ) -> Plan:
        normative_compile = facade_dependency(
            "_compile_graph_normative",
            _compile_graph_normative,
        )
        return normative_compile(
            document,
            block_catalog=block_catalog,
            allow_unknown_blocks=allow_unknown_blocks,
        )
