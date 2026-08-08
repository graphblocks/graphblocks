"""Acceptance manifests, application exercises, and gate runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from graphblocks.canonical import (
    canonical_dumps_reference as canonical_dumps,
    canonical_hash_reference as canonical_hash,
)
from graphblocks.cli import main as graphblocks_cli_main
from graphblocks.conversation import (
    ContentPart,
    Conversation,
    ConversationConflictError,
    InMemoryConversationStore,
    Message,
)
from graphblocks.deployment import (
    CanaryMetricThreshold,
    GraphRelease,
    GraphReleaseGraph,
    PromptLock,
    ReleaseAttestation,
    ReleaseBundle,
    ReleaseLockRef,
    RolloutAnalysisResult,
    RolloutPlan,
    RolloutStep,
    SupplyChainLock,
    evaluate_canary_metrics,
    evaluate_rollback_and_drain,
    verify_release_attestation,
)
from graphblocks.blob_store import BlobKey, LocalBlobStore, PutOptions
from graphblocks.document_parsers import (
    DocumentParserRegistry,
)
from graphblocks.documents import (
    chunk_document_by_lines,
    create_local_text_revision,
    parse_plain_text_document,
)
from graphblocks.evaluation import (
    ChangeSet,
    CheckResult,
    ResourceSnapshotRef,
    ResultBundle,
    evaluate_gate,
)
from graphblocks.budget import (
    InMemoryBudgetLedger,
    SQLiteBudgetLedger,
    UsageAmount,
)
from graphblocks.integrations.pdf import (
    PdfPageText,
    PdfParserError,
    marker_pdf_parser_descriptor,
    pdf_parser_descriptor,
)
from graphblocks.loader import load_documents
from graphblocks.orchestration import (
    ChildBudgetDelegation,
    LeasePool,
    LeaseRequest,
    TaskContextAccess,
    TaskExecutionContract,
    TaskPlan,
    TaskPlanLimits,
    TaskPlanPatch,
    TaskPlanPatchMismatchError,
    TaskStep,
)
from graphblocks.policy import PrincipalRef, ResourceRef as PolicyResourceRef
from graphblocks.rag import (
    Answer,
    AuthContext,
    Citation,
    Claim,
    ContextPack,
    InMemoryChunkRetriever,
    InMemoryKnowledgeIndex,
    authorize_search_hits,
    resolve_citation_source_trace,
    validate_answer_citations,
    validate_answer_grounding,
)
from graphblocks.review import (
    InMemoryReviewerCredentialProvider,
    ReviewRequest,
    ReviewWorkflow,
    ReviewerCredential,
)
from graphblocks.server import (
    GraphBlocksServerApp,
    ServerAsyncCallbackSubmission,
    ServerRequest,
    StaticBearerAuthHook,
)
from graphblocks.runtime import (
    SQLiteExecutionJournal,
    stdlib_registry,
)
from graphblocks.usage import SQLiteUsageLedger, UsageRecord
from graphblocks.workspace import (
    InMemoryWorkspaceStore,
    WorkspaceMutationPolicy,
    WorkspaceSnapshot,
    WorkspaceTrialPlan,
)

from .acceptance_models import (
    AcceptanceApplication,
    AcceptanceApplicationExpectation,
    AcceptanceApplicationReport,
    AcceptanceCoverageIssue,
    AcceptanceCoverageResult,
    AcceptanceGateDiagnostic,
    AcceptanceGateResult,
    AcceptanceRunReport,
    _acceptance_scenario_path_beneath_root,
)


@dataclass(frozen=True, slots=True)
class AcceptanceManifest:
    applications: tuple[AcceptanceApplication, ...]

    def __post_init__(self) -> None:
        applications = tuple(
            sorted(
                self.applications, key=lambda application: application.application_id
            )
        )
        seen: set[str] = set()
        for application in applications:
            if application.application_id in seen:
                raise ValueError(
                    f"duplicate acceptance application id {application.application_id!r}"
                )
            seen.add(application.application_id)
        object.__setattr__(self, "applications", applications)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> AcceptanceManifest:
        if document.get("kind") != "AcceptanceApplicationSet":
            raise ValueError(
                "acceptance manifest kind must be AcceptanceApplicationSet"
            )
        spec = document.get("spec")
        if not isinstance(spec, Mapping):
            raise ValueError("acceptance manifest spec must be a mapping")
        raw_applications = spec.get("applications", ())
        if not isinstance(raw_applications, list):
            raise ValueError("acceptance manifest spec.applications must be a list")
        applications = []
        for index, raw_application in enumerate(raw_applications):
            if not isinstance(raw_application, Mapping):
                raise ValueError(
                    f"acceptance manifest application {index} must be a mapping"
                )
            applications.append(AcceptanceApplication.from_mapping(raw_application))
        return cls(tuple(applications))

    def application_ids(self) -> tuple[str, ...]:
        return tuple(application.application_id for application in self.applications)

    def by_id(self, application_id: str) -> AcceptanceApplication:
        for application in self.applications:
            if application.application_id == application_id:
                return application
        raise KeyError(application_id)

    def coverage_for_conformance(
        self,
        conformance_document: Mapping[str, object],
        *,
        root: Path | None = None,
    ) -> AcceptanceCoverageResult:
        applications_by_id = {
            application.application_id: application for application in self.applications
        }
        issues: list[AcceptanceCoverageIssue] = []
        expectations: list[AcceptanceApplicationExpectation] = []
        spec = conformance_document.get("spec", {})
        profiles = spec.get("profiles", ()) if isinstance(spec, Mapping) else ()
        for profile_index, profile in enumerate(profiles):
            if not isinstance(profile, Mapping):
                continue
            profile_id = str(profile.get("id", ""))
            acceptance_applications = profile.get("acceptanceApplications", ())
            for application_index, raw_application_id in enumerate(
                acceptance_applications or ()
            ):
                application_id = str(raw_application_id)
                application = applications_by_id.get(application_id)
                if application is None:
                    issues.append(
                        AcceptanceCoverageIssue(
                            code="AcceptanceApplicationMissing",
                            application_id=application_id,
                            profile_id=profile_id,
                            path=f"$.spec.profiles[{profile_index}].acceptanceApplications[{application_index}]",
                            message="profile references an acceptance application with no manifest entry",
                        )
                    )
                    continue
                if profile_id not in application.profiles:
                    issues.append(
                        AcceptanceCoverageIssue(
                            code="AcceptanceProfileNotDeclared",
                            application_id=application_id,
                            profile_id=profile_id,
                            path=f"$.spec.profiles[{profile_index}].acceptanceApplications[{application_index}]",
                            message="acceptance application does not declare the referencing conformance profile",
                        )
                    )
        for application in self.applications:
            scenario_digest: str | None = None
            scenario_path: Path | None = None
            scenario_outside_root = False
            if root is not None:
                try:
                    scenario_path = _acceptance_scenario_path_beneath_root(
                        root,
                        application.scenario_path,
                    )
                except ValueError:
                    scenario_outside_root = True
                    issues.append(
                        AcceptanceCoverageIssue(
                            code="AcceptanceFixtureOutsideRoot",
                            application_id=application.application_id,
                            profile_id="",
                            path=(
                                f"$.spec.applications[{application.application_id}]."
                                "scenarioPath"
                            ),
                            message="acceptance application scenario path must remain beneath root",
                        )
                    )
            if root is None:
                issues.append(
                    AcceptanceCoverageIssue(
                        code="AcceptanceScenarioDigestMissing",
                        application_id=application.application_id,
                        profile_id="",
                        path=(
                            f"$.spec.applications[{application.application_id}]."
                            "scenarioPath"
                        ),
                        message="acceptance evidence requires a root to digest the scenario",
                    )
                )
            elif scenario_outside_root:
                pass
            elif scenario_path is not None and not scenario_path.exists():
                issues.append(
                    AcceptanceCoverageIssue(
                        code="AcceptanceFixtureMissing",
                        application_id=application.application_id,
                        profile_id="",
                        path=f"$.spec.applications[{application.application_id}].scenarioPath",
                        message="acceptance application scenario path does not exist",
                    )
                )
            elif scenario_path is not None:
                try:
                    scenario_digest = canonical_hash(load_documents(scenario_path))
                except (OSError, TypeError, ValueError, yaml.YAMLError):
                    issues.append(
                        AcceptanceCoverageIssue(
                            code="AcceptanceFixtureInvalid",
                            application_id=application.application_id,
                            profile_id="",
                            path=(
                                f"$.spec.applications[{application.application_id}]."
                                "scenarioPath"
                            ),
                            message="acceptance application scenario could not be loaded",
                        )
                    )
            if not application.gates:
                issues.append(
                    AcceptanceCoverageIssue(
                        code="AcceptanceGateMissing",
                        application_id=application.application_id,
                        profile_id="",
                        path=f"$.spec.applications[{application.application_id}].gates",
                        message="acceptance application must declare at least one verification gate",
                    )
                )
            expectations.append(
                AcceptanceApplicationExpectation(
                    application_id=application.application_id,
                    scenario_path=application.scenario_path,
                    application_digest=canonical_hash(
                        application.application_contract()
                    ),
                    scenario_digest=scenario_digest,
                    gates=application.gates,
                )
            )
        return AcceptanceCoverageResult(
            issues=tuple(issues),
            application_ids=self.application_ids(),
            manifest_digest=self.content_digest(),
            expectations=tuple(expectations),
        )

    def manifest_contract(self) -> dict[str, object]:
        return {
            "applications": [
                application.application_contract() for application in self.applications
            ],
        }

    def content_digest(self) -> str:
        return canonical_hash(self.manifest_contract())


AcceptanceGateHandler = Callable[[AcceptanceApplication, Path], tuple[int, str]]


def _exercise_bounded_research_orchestrator(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "bounded-research-orchestrator":
        raise RuntimeError(
            "bounded orchestration gate requires the bounded-research-orchestrator application"
        )
    documents = load_documents(scenario_path)
    if len(documents) != 1:
        raise RuntimeError("bounded research scenario must contain exactly one graph")
    graph = documents[0]
    graph_spec = graph.get("spec")
    if not isinstance(graph_spec, Mapping) or not isinstance(
        graph_spec.get("nodes"), Mapping
    ):
        raise RuntimeError("bounded research graph nodes must be a mapping")
    nodes = graph_spec["nodes"]
    expected_blocks = {
        "snapshot": "resource.snapshot@1",
        "resolveWorkers": "orchestration.resolve_worker_pool@1",
        "plan": "orchestration.plan@1",
        "validate": "orchestration.validate_plan@1",
        "execute": "orchestration.execute_task_plan@1",
        "detectGaps": "research.detect_gaps@1",
        "patch": "orchestration.replan@1",
        "verify": "check.run_suite@1",
        "gate": "gate.evaluate@1",
        "bundle": "result.bundle@1",
    }
    if any(
        not isinstance(nodes.get(node_id), Mapping)
        or nodes[node_id].get("block") != block
        for node_id, block in expected_blocks.items()
    ):
        raise RuntimeError(
            "bounded research orchestration block identities do not match"
        )
    plan_node = nodes["plan"]
    validate_node = nodes["validate"]
    execute_node = nodes["execute"]
    patch_node = nodes["patch"]
    verify_node = nodes["verify"]
    plan_config = plan_node.get("config")
    validate_config = validate_node.get("config")
    execute_config = execute_node.get("config")
    patch_config = patch_node.get("config")
    verify_config = verify_node.get("config")
    if (
        plan_node.get("inputs")
        != {"objective": "$input.objective", "workers": "resolveWorkers.pool"}
        or validate_node.get("inputs") != {"plan": "plan.value"}
        or execute_node.get("inputs")
        != {"plan": "validate.plan", "snapshot": "snapshot.value"}
        or patch_node.get("inputs")
        != {"plan": "execute.plan", "gaps": "detectGaps.gaps"}
        or verify_node.get("inputs")
        != {"subject": "execute.result", "evidence": "execute.evidence"}
    ):
        raise RuntimeError("bounded research orchestration dataflow does not match")
    if not isinstance(plan_config, Mapping) or not isinstance(
        plan_config.get("limits"), Mapping
    ):
        raise RuntimeError("bounded research task-plan limits are missing")
    raw_limits = plan_config["limits"]
    if raw_limits != {"maxTasks": 48, "maxDepth": 4, "maxParallelTasks": 8}:
        raise RuntimeError("bounded research task-plan limits do not match")
    phase_budgets = plan_config.get("phaseBudgets")
    if not isinstance(phase_budgets, Mapping) or sum(
        Decimal(str(value)) for value in phase_budgets.values()
    ) != Decimal("1.00"):
        raise RuntimeError(
            "bounded research phase budgets must partition the full budget"
        )
    if validate_config != {
        "requireAcyclicDependencies": True,
        "requireBoundedRecursion": True,
        "requireExplicitContextAccess": True,
    }:
        raise RuntimeError("bounded research validation contract does not match")
    if not isinstance(execute_config, Mapping):
        raise RuntimeError("bounded research execution contract is missing")
    pressure = execute_config.get("onBudgetPressure")
    if (
        execute_config.get("checkpoint") != "each_task"
        or execute_config.get("reservation") != "per_task"
        or pressure
        != {
            "cancelPriorities": ["optional", "normal"],
            "preserve": ["required", "verification", "finalization"],
        }
    ):
        raise RuntimeError(
            "bounded research task checkpoint, reservation, and budget pressure contract does not match"
        )
    if (
        not isinstance(patch_config, Mapping)
        or patch_config.get("concurrency") != "compare_and_swap"
        or patch_node.get("when") != "detectGaps.requiresMoreWork"
    ):
        raise RuntimeError("bounded research replan patch must use compare-and-swap")
    if not isinstance(verify_config, Mapping) or verify_config != {
        "checks": ["claim_support", "source_resolution", "contradiction_scan"],
        "independence": "exclude_originating_workers",
    }:
        raise RuntimeError("bounded research verification contract does not match")

    limits = TaskPlanLimits(
        max_steps=int(raw_limits["maxTasks"]),
        max_depth=int(raw_limits["maxDepth"]),
        max_parallel_tasks=int(raw_limits["maxParallelTasks"]),
    )
    plan = TaskPlan(
        plan_id="research-plan-1",
        objective="Research the declared objective with bounded independent verification",
        steps=(
            TaskStep(
                "collect-optional",
                "Collect optional source",
                metadata={"priority": "optional"},
            ),
            TaskStep(
                "collect-required",
                "Collect required source",
                metadata={"priority": "required"},
            ),
            TaskStep(
                "synthesize",
                "Synthesize supported claims",
                depends_on=("collect-optional", "collect-required"),
                metadata={"priority": "normal"},
            ),
            TaskStep(
                "verify",
                "Verify claims independently",
                depends_on=("synthesize",),
                metadata={"priority": "verification"},
            ),
            TaskStep(
                "finalize",
                "Finalize the result bundle",
                depends_on=("verify",),
                metadata={"priority": "finalization"},
            ),
        ),
        limits=limits,
        context_resources=("source-snapshot", "draft-result"),
        context_access=(
            TaskContextAccess("collect-optional", "source-snapshot", "read"),
            TaskContextAccess("collect-required", "source-snapshot", "read"),
            TaskContextAccess("synthesize", "source-snapshot", "read"),
            TaskContextAccess("synthesize", "draft-result", "write"),
            TaskContextAccess("verify", "draft-result", "read"),
            TaskContextAccess("finalize", "draft-result", "read"),
        ),
    )
    contract = TaskExecutionContract(
        checkpoint=str(execute_config["checkpoint"]),  # type: ignore[arg-type]
        reservation=str(execute_config["reservation"]),  # type: ignore[arg-type]
        cancel_priorities=tuple(pressure["cancelPriorities"]),  # type: ignore[arg-type]
        preserve_priorities=tuple(pressure["preserve"]),  # type: ignore[arg-type]
    )
    ledger = InMemoryBudgetLedger()
    parent_amount = UsageAmount("model_total_tokens", Decimal("100"), "tokens")
    child_amount = UsageAmount("model_total_tokens", Decimal("40"), "tokens")
    ledger.allocate(
        "budget-research",
        PolicyResourceRef("run:research-1"),
        [parent_amount],
        policy_ref="policy:research",
    )
    reservation = ledger.reserve(
        "budget-research",
        PolicyResourceRef("task:coordinator"),
        [parent_amount],
        purpose="provider_call",
        expires_at="2026-07-10T01:00:00Z",
    )
    parent_permit = ledger.issue_permit(
        "permit-research-parent",
        reservation_ids=[reservation.reservation_id],
        owner=PolicyResourceRef("task:coordinator"),
        atomic_unit=PolicyResourceRef("plan:research-plan-1"),
        admission_epoch=1,
        continuation_profile="finish_current_task",
        policy_snapshot_digest="sha256:research-policy",
        expires_at="2026-07-10T01:00:00Z",
    )
    child_permit = ChildBudgetDelegation(
        delegation_id="delegation-collect-optional",
        parent_permit=parent_permit,
        child_owner=PolicyResourceRef("task:collect-optional"),
        amounts=[child_amount],
        expires_at="2026-07-10T00:45:00Z",
    ).create_child_permit("permit-collect-optional")
    checkpoint = contract.checkpoint_completion(
        plan,
        "collect-optional",
        child_permit,
        result_digest="sha256:collect-optional-result",
        completed_at="2026-07-10T00:30:00Z",
    )
    cancellations = contract.budget_pressure_cancellations(
        plan,
        active_step_ids=("collect-optional", "synthesize", "verify", "finalize"),
    )
    patch = TaskPlanPatch(
        patch_id="research-gap-patch-1",
        base_plan_id=plan.plan_id,
        base_revision=plan.revision,
        upsert_steps=(
            TaskStep(
                "collect-gap", "Collect missing source", metadata={"priority": "normal"}
            ),
            TaskStep(
                "synthesize",
                "Synthesize supported claims including gap evidence",
                depends_on=("collect-optional", "collect-required", "collect-gap"),
                metadata={"priority": "normal"},
            ),
        ),
    )
    patched = plan.apply_patch(patch)
    stale_rejected = False
    try:
        patched.apply_patch(patch)
    except TaskPlanPatchMismatchError:
        stale_rejected = True
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "plan": {
            "revision": plan.revision,
            "layers": [list(layer) for layer in plan.execution_layers()],
            "maxDepth": plan.limits.max_depth,
            "maxParallelTasks": plan.limits.max_parallel_tasks,
            "contextAccessCount": len(plan.context_access),
            "digest": plan.content_digest(),
        },
        "budget": {
            "parentPermitId": parent_permit.permit_id,
            "childPermitId": child_permit.permit_id,
            "childOwner": child_permit.owner.resource_id,
            "childAmounts": [
                str(amount.amount) for amount in child_permit.authorized_amounts
            ],
            "childExpiresAt": child_permit.expires_at,
            "checkpointStepId": checkpoint.step_id,
            "checkpointPermitId": checkpoint.permit_id,
            "cancellations": list(cancellations),
        },
        "patch": {
            "baseRevision": patch.base_revision,
            "updatedRevision": patched.revision,
            "stepIds": [step.step_id for step in patched.steps],
            "staleRejected": stale_rejected,
        },
    }


def _bounded_task_plan_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_bounded_research_orchestrator(application, scenario_path)
    plan = evidence["plan"]
    if (
        not isinstance(plan, Mapping)
        or plan.get("layers")
        != [
            ["collect-optional", "collect-required"],
            ["synthesize"],
            ["verify"],
            ["finalize"],
        ]
        or plan.get("maxDepth") != 4
        or plan.get("maxParallelTasks") != 8
        or plan.get("contextAccessCount") != 6
    ):
        raise RuntimeError("bounded task plan evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "bounded task plan check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "plan": plan,
        }
    )


def _task_budget_delegation_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_bounded_research_orchestrator(application, scenario_path)
    budget = evidence["budget"]
    if not isinstance(budget, Mapping) or budget != {
        "parentPermitId": "permit-research-parent",
        "childPermitId": "permit-collect-optional",
        "childOwner": "task:collect-optional",
        "childAmounts": ["40"],
        "childExpiresAt": "2026-07-10T00:45:00Z",
        "checkpointStepId": "collect-optional",
        "checkpointPermitId": "permit-collect-optional",
        "cancellations": ["collect-optional", "synthesize"],
    }:
        raise RuntimeError("task budget delegation evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "task budget delegation check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "budget": budget,
        }
    )


def _replan_patch_cas_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_bounded_research_orchestrator(application, scenario_path)
    patch = evidence["patch"]
    if (
        not isinstance(patch, Mapping)
        or patch.get("baseRevision") != 1
        or patch.get("updatedRevision") != 2
        or patch.get("staleRejected") is not True
        or "collect-gap" not in patch.get("stepIds", [])
    ):
        raise RuntimeError("replan patch compare-and-swap evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "replan patch CAS check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "patch": patch,
        }
    )


def _exercise_verified_rtl_workspace_trial(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "verified-rtl-workspace-trial":
        raise RuntimeError(
            "verified trial gate requires the verified-rtl-workspace-trial application"
        )
    documents = load_documents(scenario_path)
    if len(documents) != 2:
        raise RuntimeError(
            "verified RTL scenario must contain workspace and candidate trial graphs"
        )
    graph, trial_graph = documents
    graph_spec = graph.get("spec")
    trial_spec = trial_graph.get("spec")
    if (
        not isinstance(graph_spec, Mapping)
        or not isinstance(graph_spec.get("nodes"), Mapping)
        or not isinstance(trial_spec, Mapping)
        or not isinstance(trial_spec.get("nodes"), Mapping)
    ):
        raise RuntimeError("verified RTL graph nodes must be mappings")
    nodes = graph_spec["nodes"]
    trial_nodes = trial_spec["nodes"]
    review_node = nodes.get("review")
    verify_node = nodes.get("verifyTrial")
    commit_node = nodes.get("commit")
    reserve_node = trial_nodes.get("reserve")
    fork_node = trial_nodes.get("fork")
    apply_node = trial_nodes.get("apply")
    formal_node = trial_nodes.get("formal")
    aggregate_checks_node = trial_nodes.get("aggregateChecks")
    gate_node = trial_nodes.get("gate")
    seal_node = trial_nodes.get("seal")
    if any(
        not isinstance(node, Mapping)
        for node in (
            review_node,
            verify_node,
            commit_node,
            reserve_node,
            fork_node,
            apply_node,
            formal_node,
            aggregate_checks_node,
            gate_node,
            seal_node,
        )
    ):
        raise RuntimeError("verified RTL governance nodes are missing")
    assert isinstance(review_node, Mapping)
    assert isinstance(verify_node, Mapping)
    assert isinstance(commit_node, Mapping)
    assert isinstance(reserve_node, Mapping)
    assert isinstance(fork_node, Mapping)
    assert isinstance(apply_node, Mapping)
    assert isinstance(formal_node, Mapping)
    assert isinstance(aggregate_checks_node, Mapping)
    assert isinstance(gate_node, Mapping)
    assert isinstance(seal_node, Mapping)
    review_config = review_node.get("config")
    verify_config = verify_node.get("config")
    reserve_config = reserve_node.get("config")
    if (
        review_node.get("block") != "review.request@1"
        or review_node.get("inputs") != {"subject": "select.changeSet"}
        or review_config
        != {"scope": "design_intent", "invalidateOnSubjectChange": True}
    ):
        raise RuntimeError("verified RTL review invalidation contract does not match")
    if (
        verify_node.get("block") != "trial.plan_commit@1"
        or verify_node.get("inputs")
        != {
            "changeSet": "select.changeSet",
            "checks": "select.checks",
            "gate": "select.gate",
            "leases": "select.leases",
            "review": "review.record",
        }
        or verify_config
        != {
            "requiredChecks": ["lint", "compile", "regression", "formal"],
            "requiredLeaseKinds": ["eda.formal"],
            "requiredReviewScopes": ["design_intent"],
        }
    ):
        raise RuntimeError("verified RTL trial commit requirements do not match")
    if (
        commit_node.get("block") != "workspace.commit_changeset@1"
        or commit_node.get("when") != "verifyTrial.ready"
        or commit_node.get("inputs")
        != {
            "workspace": "$input.workspace",
            "base": "snapshot.value",
            "request": "verifyTrial.commitRequest",
        }
        or commit_node.get("config") != {"concurrency": "compare_and_swap"}
    ):
        raise RuntimeError("verified RTL commit dataflow does not match")
    reserve_limits = (
        reserve_config.get("limits") if isinstance(reserve_config, Mapping) else None
    )
    if (
        reserve_node.get("block") != "budget.reserve@1"
        or reserve_limits
        != [
            {"kind": "cpu_seconds", "quantity": 3600, "unit": "second"},
            {"kind": "licensed_resource_seconds", "quantity": 900, "unit": "second"},
        ]
        or formal_node.get("block") != "check.run_suite@1"
        or formal_node.get("flow") != {"leasePool": "formal-license"}
    ):
        raise RuntimeError(
            "verified RTL budget reservation and lease-pool contract does not match"
        )
    if (
        fork_node.get("block") != "workspace.fork@1"
        or fork_node.get("execution") != {"requires": {"isolation": "sandbox"}}
        or apply_node.get("block") != "workspace.apply_changeset@1"
        or apply_node.get("inputs") != {"workspace": "fork.workspace"}
    ):
        raise RuntimeError("verified RTL trial must apply changes in the isolated fork")
    seal_inputs = seal_node.get("inputs")
    if (
        aggregate_checks_node.get("block") != "check.aggregate@1"
        or aggregate_checks_node.get("inputs")
        != {
            "fast": "fast.results",
            "formal": "formal.results",
            "synthesis": "synthesis.results",
        }
        or gate_node.get("block") != "gate.evaluate@1"
        or gate_node.get("inputs") != {"checks": "aggregateChecks.results"}
        or seal_node.get("block") != "trial.seal_result@1"
        or not isinstance(seal_inputs, Mapping)
        or seal_inputs.get("checks") != "aggregateChecks.results"
    ):
        raise RuntimeError("verified RTL trial checks must use explicit aggregation")

    base = WorkspaceSnapshot(
        workspace_id="workspace-rtl",
        snapshot_id="snapshot-base",
        revision=7,
        resources=(
            ResourceSnapshotRef("design.v", "sha256:base-design", resource_kind="file"),
        ),
        created_at="2026-07-10T00:00:00Z",
    )
    candidate_resources = (
        ResourceSnapshotRef(
            "design.v", "sha256:candidate-design", resource_kind="file"
        ),
    )
    candidate = WorkspaceSnapshot(
        workspace_id="workspace-rtl",
        snapshot_id="snapshot-candidate",
        revision=8,
        resources=candidate_resources,
        created_at="2026-07-10T00:30:00Z",
        base_snapshot_id=base.snapshot_id,
        base_snapshot_digest=base.content_digest(),
    )
    change_set = ChangeSet(
        "changeset-rtl-1",
        base=ResourceSnapshotRef(
            "workspace-rtl", base.content_digest(), resource_kind="workspace"
        ),
        candidate=ResourceSnapshotRef(
            "workspace-rtl",
            candidate.content_digest(),
            resource_kind="workspace",
        ),
        operations=(
            {"op": "file.replace", "resource_id": "design.v", "resource_kind": "file"},
        ),
    )
    required_checks = tuple(verify_config["requiredChecks"])  # type: ignore[index]
    checks = tuple(
        CheckResult(check_id, change_set.candidate, "passed")
        for check_id in required_checks
    )
    gate = evaluate_gate(
        "rtl-quality",
        change_set.candidate,
        checks=list(checks),
        required_check_ids=list(required_checks),
    )
    mutation = WorkspaceMutationPolicy(
        policy_id="rtl-trial-mutation",
        allowed_resource_kinds=("file",),
        required_review_scopes=("design_intent",),
    ).evaluate(
        change_set,
        PrincipalRef("optimizer-1"),
        review_scopes=("design_intent",),
        base_resources=base.resources,
        candidate_resources=candidate.resources,
    )
    reviewer = PrincipalRef("reviewer-1")
    review_workflow = ReviewWorkflow(
        request=ReviewRequest(
            request_id="review-request-rtl-1",
            subject=change_set.candidate,
            requested_by=PrincipalRef("optimizer-1"),
            required_scopes=("design_intent",),
            created_at="2026-07-10T00:15:00Z",
        ),
        credential_provider=InMemoryReviewerCredentialProvider(
            (
                ReviewerCredential(
                    "credential-design-intent",
                    reviewer,
                    scopes=("design_intent",),
                    issued_at="2026-07-10T00:00:00Z",
                ),
            )
        ),
    )
    review = review_workflow.record_review(
        review_id="review-rtl-1",
        reviewer=reviewer,
        scope="design_intent",
        decision="accept",
        created_at="2026-07-10T00:20:00Z",
    )
    completed_before_invalidation = review_workflow.completed_scopes()
    invalidated_workflow = review_workflow.with_review(
        review.invalidate("2026-07-10T00:21:00Z")
    )
    completed_after_invalidation = invalidated_workflow.completed_scopes()

    ledger = InMemoryBudgetLedger()
    licensed_amount = UsageAmount("licensed_resource_seconds", Decimal("900"), "second")
    trial_owner = PolicyResourceRef("trial:rtl-1")
    ledger.allocate(
        "budget-rtl",
        PolicyResourceRef("run:rtl-1"),
        [licensed_amount],
        policy_ref="policy:rtl",
    )
    reservation = ledger.reserve(
        "budget-rtl",
        trial_owner,
        [licensed_amount],
        purpose="provider_call",
        expires_at="2026-07-10T00:40:00Z",
    )
    permit = ledger.issue_permit(
        "permit-rtl-formal",
        reservation_ids=[reservation.reservation_id],
        owner=trial_owner,
        atomic_unit=trial_owner,
        admission_epoch=1,
        continuation_profile="finish_current_check",
        policy_snapshot_digest="sha256:rtl-policy",
        expires_at="2026-07-10T00:40:00Z",
    )
    leased_pool, lease = LeasePool(
        "formal-license", "eda.formal", 1
    ).acquire_with_budget_permit(
        LeaseRequest("formal-check", trial_owner, "eda.formal"),
        permit,
        [UsageAmount("licensed_resource_seconds", Decimal("300"), "second")],
        lease_id="lease-rtl-formal",
        acquired_at="2026-07-10T00:10:00Z",
        expires_at="2026-07-10T00:35:00Z",
    )
    trial_plan = WorkspaceTrialPlan(
        trial_id="rtl-1",
        change_set=change_set,
        expected_base_revision=base.revision,
        required_check_ids=required_checks,
        required_lease_kinds=tuple(verify_config["requiredLeaseKinds"]),  # type: ignore[index]
        required_review_scopes=tuple(verify_config["requiredReviewScopes"]),  # type: ignore[index]
        checks=checks,
        gate=gate,
        mutation_decision=mutation,
        leases=(lease,),
        reviews=(review,),
    )
    commit_request = trial_plan.to_commit_request(
        "commit-rtl-1",
        now="2026-07-10T00:25:00Z",
    )
    commit = (
        InMemoryWorkspaceStore()
        .put_snapshot(base)
        .compare_and_swap_commit_request(
            workspace_id="workspace-rtl",
            request=commit_request,
            new_snapshot_id="snapshot-candidate",
            resources=candidate_resources,
            committed_by=PrincipalRef("optimizer-1"),
            committed_at="2026-07-10T00:30:00Z",
        )
    )
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "lease": {
            "leaseId": lease.lease_id,
            "permitId": lease.metadata.get("budget_permit_id"),
            "reservationRefs": lease.metadata.get("budget_reservation_refs"),
            "resourceKind": lease.resource_kind,
            "holder": lease.holder.resource_id,
            "fencingEpoch": lease.fencing_epoch,
            "activeAtGate": lease.is_active_at("2026-07-10T00:25:00Z"),
            "availableUnits": leased_pool.available_units,
        },
        "review": {
            "subjectDigest": review.subject_digest,
            "completedBeforeInvalidation": list(completed_before_invalidation),
            "completedAfterInvalidation": list(completed_after_invalidation),
            "invalidatedReviewValid": review.invalidate(
                "2026-07-10T00:21:00Z"
            ).is_valid_for(change_set.candidate),
        },
        "commit": {
            "ready": True,
            "changeSetDigest": commit_request.metadata.get("change_set_digest"),
            "leaseIds": commit_request.metadata.get("lease_ids"),
            "trialId": commit_request.metadata.get("trial_id"),
            "gateDecision": commit_request.gate.decision,
            "reviewIds": [item.review_id for item in commit_request.reviews],
            "snapshotDigest": commit.snapshot.content_digest(),
            "candidateDigest": change_set.candidate.digest,
            "revision": commit.snapshot.revision,
        },
    }


def _budget_lease_reservation_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_verified_rtl_workspace_trial(application, scenario_path)
    lease = evidence["lease"]
    if not isinstance(lease, Mapping) or lease != {
        "leaseId": "lease-rtl-formal",
        "permitId": "permit-rtl-formal",
        "reservationRefs": ["reservation-000001"],
        "resourceKind": "eda.formal",
        "holder": "trial:rtl-1",
        "fencingEpoch": 1,
        "activeAtGate": True,
        "availableUnits": 0,
    }:
        raise RuntimeError("budget-bound scarce-resource lease evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "budget lease reservation check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "lease": lease,
        }
    )


def _review_invalidation_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_verified_rtl_workspace_trial(application, scenario_path)
    review = evidence["review"]
    if not isinstance(review, Mapping) or review != {
        "subjectDigest": evidence["commit"]["candidateDigest"],  # type: ignore[index]
        "completedBeforeInvalidation": ["design_intent"],
        "completedAfterInvalidation": [],
        "invalidatedReviewValid": False,
    }:
        raise RuntimeError("review subject invalidation evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "review invalidation check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "review": review,
        }
    )


def _governed_trial_commit_gate(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_verified_rtl_workspace_trial(application, scenario_path)
    commit = evidence["commit"]
    if (
        not isinstance(commit, Mapping)
        or commit.get("ready") is not True
        or commit.get("gateDecision") != "pass"
        or commit.get("leaseIds") != ["lease-rtl-formal"]
        or commit.get("reviewIds") != ["review-rtl-1"]
        or commit.get("snapshotDigest") != commit.get("candidateDigest")
        or commit.get("revision") != 8
    ):
        raise RuntimeError("governed trial commit evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "governed trial commit gate",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "commit": commit,
        }
    )


def _exercise_kubernetes_canary(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "kubernetes-canary":
        raise RuntimeError(
            "deployment semantic gate requires the kubernetes-canary application"
        )
    documents = load_documents(scenario_path)
    if len(documents) != 2:
        raise RuntimeError(
            "kubernetes canary scenario must contain release and deployment documents"
        )
    release_document, deployment_document = documents
    if (
        release_document.get("kind") != "GraphRelease"
        or deployment_document.get("kind") != "GraphDeployment"
    ):
        raise RuntimeError(
            "kubernetes canary scenario requires GraphRelease and GraphDeployment documents"
        )
    release_spec = release_document.get("spec")
    deployment_spec = deployment_document.get("spec")
    if not isinstance(release_spec, Mapping) or not isinstance(
        deployment_spec, Mapping
    ):
        raise RuntimeError(
            "kubernetes canary release and deployment specs must be mappings"
        )
    release_metadata = release_document.get("metadata")
    release_ref = deployment_spec.get("releaseRef")
    if not isinstance(release_metadata, Mapping) or not isinstance(
        release_metadata.get("name"), str
    ):
        raise RuntimeError("kubernetes canary release name is missing")
    release_name = release_metadata["name"]
    if release_ref != {"name": release_name}:
        raise RuntimeError(
            "kubernetes canary deployment must reference the verified release"
        )
    bundle_spec = release_spec.get("bundle")
    identity = release_spec.get("identity")
    rollout = deployment_spec.get("rollout")
    upgrades = deployment_spec.get("upgrades")
    targets = deployment_spec.get("targets")
    if (
        not isinstance(bundle_spec, Mapping)
        or not isinstance(identity, Mapping)
        or not isinstance(rollout, Mapping)
        or not isinstance(upgrades, Mapping)
        or not isinstance(targets, Mapping)
    ):
        raise RuntimeError(
            "kubernetes canary release identity, rollout, upgrades, and targets are required"
        )
    bundle_ref = bundle_spec.get("ref")
    bundle_digest_ref = (
        bundle_ref.rsplit("@sha256:", 1)[-1] if isinstance(bundle_ref, str) else ""
    )
    if (
        not isinstance(bundle_ref, str)
        or "@sha256:" not in bundle_ref
        or not bundle_digest_ref
        or bundle_digest_ref != bundle_digest_ref.strip()
        or bundle_spec.get("mediaType") != "application/vnd.graphblocks.bundle.v1"
        or bundle_spec.get("signaturePolicy") != "production-publishers"
    ):
        raise RuntimeError(
            "kubernetes canary release must require a digest-pinned, production-signed bundle"
        )
    expected_identity_fields = {
        "graphHash",
        "physicalPlanHash",
        "bindingLockHash",
        "packageLockHash",
        "promptLockHash",
    }
    if set(identity) != expected_identity_fields or any(
        not isinstance(identity[field_name], str)
        or not identity[field_name].startswith("sha256:")
        or not identity[field_name].removeprefix("sha256:")
        or identity[field_name] != identity[field_name].strip()
        for field_name in expected_identity_fields
    ):
        raise RuntimeError("kubernetes canary release identity locks do not match")
    rollout_steps = rollout.get("steps")
    rollout_gates = rollout.get("gates")
    if (
        rollout.get("strategy") != "canary"
        or rollout.get("affinity") != "conversation_id"
        or rollout_steps
        != [
            {"traffic": 1, "minimumSamples": 200},
            {"traffic": 10, "minimumDuration": "30m"},
            {"traffic": 50, "minimumDuration": "1h"},
        ]
        or rollout_gates
        != [
            {"metric": "turn_success_rate", "min": 0.995},
            {"metric": "citation_validation_rate", "min": 0.98},
            {"metric": "average_cost_per_turn", "maxRegression": 0.10},
        ]
    ):
        raise RuntimeError(
            "kubernetes canary rollout thresholds and steps do not match"
        )
    if upgrades != {
        "existingRequests": "finish_on_old",
        "conversations": "keep_affinity",
        "durableJobs": "checkpoint_and_migrate",
    }:
        raise RuntimeError(
            "kubernetes canary workload-aware upgrade contract does not match"
        )
    control_target = targets.get("control")
    if not isinstance(control_target, Mapping) or control_target.get("lifecycle") != {
        "startup": "120s",
        "drain": "60s",
    }:
        raise RuntimeError(
            "kubernetes canary control target must declare its drain lifecycle"
        )

    release = (
        GraphRelease(release_name, "2026.06.22.1")
        .with_bundle(canonical_hash(release_document), str(bundle_spec["mediaType"]))
        .with_application_hash(
            canonical_hash({"application": release_spec.get("application")})
        )
        .with_graph(
            "enterprise-rag",
            GraphReleaseGraph(
                canonical_hash({"graphHash": identity["graphHash"]}),
                canonical_hash({"physicalPlanHash": identity["physicalPlanHash"]}),
            ),
        )
        .with_lock(
            "bindings",
            ReleaseLockRef(
                "locks/bindings.lock",
                canonical_hash({"binding": identity["bindingLockHash"]}),
            ),
        )
        .with_lock(
            "packages",
            ReleaseLockRef(
                "locks/packages.lock",
                canonical_hash({"package": identity["packageLockHash"]}),
            ),
        )
        .with_lock(
            "prompt",
            ReleaseLockRef(
                "locks/prompts.lock",
                canonical_hash({"prompt": identity["promptLockHash"]}),
            ),
        )
        .with_prompt_lock("rag", PromptLock.versioned("enterprise-rag", "2026.06.22.1"))
        .with_supply_chain(
            SupplyChainLock(
                sbom_ref="oci://registry.example.com/sbom@sha256:sbom",
                provenance_ref="oci://registry.example.com/provenance@sha256:provenance",
                signature_policy=str(bundle_spec["signaturePolicy"]),
            )
        )
    )
    release.validate_production_pins()
    release_bundle = ReleaseBundle(
        bundle_id="enterprise-rag-production-bundle",
        release=release,
        artifacts={
            "provenance": canonical_hash(
                {"release": release.content_digest(), "kind": "provenance"}
            ),
            "sbom": canonical_hash(
                {"release": release.content_digest(), "kind": "sbom"}
            ),
        },
    )
    signing_key = b"graphblocks-local-production-publisher"
    attestation = ReleaseAttestation.sign(
        release_bundle,
        signer_id="production-publisher-1",
        signing_key=signing_key,
    )
    verified = verify_release_attestation(
        release_bundle,
        attestation,
        trusted_signing_keys={"production-publisher-1": signing_key},
    )
    tampered = verify_release_attestation(
        replace(
            release_bundle,
            artifacts={
                **release_bundle.artifacts,
                "sbom": canonical_hash({"tampered": True}),
            },
        ),
        attestation,
        trusted_signing_keys={"production-publisher-1": signing_key},
    )
    untrusted = verify_release_attestation(
        release_bundle,
        attestation,
        trusted_signing_keys={"staging-publisher": signing_key},
    )

    thresholds = tuple(
        CanaryMetricThreshold(
            metric=str(gate["metric"]),
            minimum=float(gate["min"]) if "min" in gate else None,
            max_regression=float(gate["maxRegression"])
            if "maxRegression" in gate
            else None,
        )
        for gate in rollout_gates
        if isinstance(gate, Mapping)
    )
    passing_metrics = {
        "average_cost_per_turn": 0.102,
        "citation_validation_rate": 0.985,
        "turn_success_rate": 0.997,
    }
    quality = evaluate_canary_metrics(
        thresholds,
        candidate_metrics=passing_metrics,
        baseline_metrics={"average_cost_per_turn": 0.1},
    )
    failing_quality = evaluate_canary_metrics(
        thresholds,
        candidate_metrics={**passing_metrics, "average_cost_per_turn": 0.12},
        baseline_metrics={"average_cost_per_turn": 0.1},
    )
    rollout_plan = RolloutPlan.canary(
        "rollout-enterprise-rag-1",
        "revision-stable",
        "revision-canary",
        affinity=str(rollout["affinity"]),
        canary_steps=(
            RolloutStep.canary("canary-1", traffic_percent=1, minimum_samples=200),
            RolloutStep.canary(
                "canary-10", traffic_percent=10, minimum_duration_seconds=1800
            ),
            RolloutStep.canary(
                "canary-50", traffic_percent=50, minimum_duration_seconds=3600
            ),
        ),
    )
    aborted = (
        rollout_plan.initial_state()
        .advance_for_test(2)
        .evaluate_gate(
            RolloutAnalysisResult(
                step_id="canary-1",
                passed=False,
                sample_count=200,
                metrics=failing_quality.evidence_contract(),
                reason="canary_quality_gate_failed",
            )
        )
    )
    rollback = evaluate_rollback_and_drain(
        aborted,
        {
            "conversation": ("revision-canary", False),
            "durable_job": ("revision-canary", True),
            "existing_request": ("revision-canary", False),
            "new_request": (None, False),
            "realtime_session": ("revision-canary", False),
        },
    )
    rollback_suppressed = evaluate_rollback_and_drain(
        replace(aborted, automatic_rollback_allowed=False),
        {"new_request": (None, False)},
    )
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "release": {
            "bundleId": release_bundle.bundle_id,
            "bundleDigest": release_bundle.content_digest(),
            "attestationDigest": release_bundle.attestation_digest(),
            "signerId": verified.signer_id,
            "verified": verified.verified,
            "reason": verified.reason,
            "tamperedVerified": tampered.verified,
            "tamperedReason": tampered.reason,
            "untrustedVerified": untrusted.verified,
            "untrustedReason": untrusted.reason,
        },
        "canary": {
            "passing": quality.evidence_contract(),
            "failing": failing_quality.evidence_contract(),
            "passingDigest": quality.content_digest(),
            "failingDigest": failing_quality.content_digest(),
        },
        "rollback": {
            **rollback.evidence_contract(),
            "suppressedAllowed": rollback_suppressed.rollback_allowed,
            "controlDrain": control_target["lifecycle"]["drain"],  # type: ignore[index]
        },
    }


def _release_bundle_verification(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_kubernetes_canary(application, scenario_path)
    release = evidence["release"]
    if (
        not isinstance(release, Mapping)
        or release.get("verified") is not True
        or release.get("reason") != "trusted_signature"
        or release.get("tamperedVerified") is not False
        or release.get("tamperedReason") != "subject_digest_mismatch"
        or release.get("untrustedVerified") is not False
        or release.get("untrustedReason") != "untrusted_signer"
    ):
        raise RuntimeError("signed release bundle verification evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "release bundle verification",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "release": release,
        }
    )


def _canary_quality_gate(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_kubernetes_canary(application, scenario_path)
    canary = evidence["canary"]
    if (
        not isinstance(canary, Mapping)
        or canary.get("passing", {}).get("passed") is not True  # type: ignore[union-attr]
        or canary.get("passing", {}).get("violations") != []  # type: ignore[union-attr]
        or canary.get("failing", {}).get("passed") is not False  # type: ignore[union-attr]
        or canary.get("failing", {}).get("violations")
        != ["average_cost_per_turn:max_regression_exceeded"]  # type: ignore[union-attr]
    ):
        raise RuntimeError("canary threshold evaluation evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "canary quality gate",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "canary": canary,
        }
    )


def _rollback_and_drain_gate(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_kubernetes_canary(application, scenario_path)
    rollback = evidence["rollback"]
    decisions = (
        rollback.get("workloadDecisions") if isinstance(rollback, Mapping) else None
    )
    if (
        not isinstance(rollback, Mapping)
        or rollback.get("rollbackAllowed") is not True
        or rollback.get("restoredRevisionId") != "revision-stable"
        or rollback.get("abortedRevisionId") != "revision-canary"
        or rollback.get("suppressedAllowed") is not False
        or rollback.get("controlDrain") != "60s"
        or not isinstance(decisions, list)
        or {decision.get("workload"): decision.get("kind") for decision in decisions}
        != {
            "conversation": "keep_affinity",
            "durable_job": "checkpoint_and_migrate",
            "existing_request": "finish_on_old",
            "new_request": "admit_on_new",
            "realtime_session": "drain_on_old",
        }
    ):
        raise RuntimeError("rollback and workload drain evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "rollback and drain gate",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "rollback": rollback,
        }
    )


def _exercise_realtime_voice_agent(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "realtime-voice-agent":
        raise RuntimeError(
            "voice semantic gate requires the realtime-voice-agent application"
        )
    try:
        voice = importlib.import_module("graphblocks.voice")
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "voice semantic gates require bundled GraphBlocks voice support"
        ) from error
    documents = load_documents(scenario_path)
    if len(documents) != 1:
        raise RuntimeError("realtime voice scenario must contain exactly one graph")
    graph = documents[0]
    graph_spec = graph.get("spec")
    if not isinstance(graph_spec, Mapping):
        raise RuntimeError("realtime voice graph spec must be a mapping")
    execution = graph_spec.get("execution")
    voice_spec = graph_spec.get("voice")
    nodes = graph_spec.get("nodes")
    if (
        not isinstance(execution, Mapping)
        or not isinstance(voice_spec, Mapping)
        or not isinstance(nodes, Mapping)
    ):
        raise RuntimeError(
            "realtime voice execution, voice, and node contracts are required"
        )
    if graph_spec.get("extensions") != ["graphblocks.voice/v1alpha1"] or execution != {
        "lifetime": "session",
        "interaction": "duplex",
        "durability": "checkpointed",
    }:
        raise RuntimeError(
            "realtime voice graph must declare checkpointed duplex session execution"
        )
    provider = voice_spec.get("provider")
    turn_detection = voice_spec.get("turnDetection")
    local_vad = voice_spec.get("localVad")
    interruption = voice_spec.get("interruption")
    playback_config = voice_spec.get("playback")
    session_node = nodes.get("session")
    tools_node = nodes.get("tools")
    telemetry_node = nodes.get("telemetry")
    if any(
        not isinstance(value, Mapping)
        for value in (
            provider,
            turn_detection,
            local_vad,
            interruption,
            playback_config,
            session_node,
            tools_node,
            telemetry_node,
        )
    ):
        raise RuntimeError(
            "realtime voice provider, authority, playback, and node contracts are required"
        )
    assert isinstance(provider, Mapping)
    assert isinstance(turn_detection, Mapping)
    assert isinstance(local_vad, Mapping)
    assert isinstance(interruption, Mapping)
    assert isinstance(playback_config, Mapping)
    assert isinstance(session_node, Mapping)
    assert isinstance(tools_node, Mapping)
    assert isinstance(telemetry_node, Mapping)
    if (
        turn_detection != {"authority": "provider", "mode": "semantic"}
        or local_vad != {"enabled": True, "role": "metrics_and_early_duck_only"}
        or interruption
        != {
            "authority": "provider",
            "policy": "adaptive",
            "onPossible": "duck",
            "onConfirmed": [
                "clear_playout",
                "cancel_response",
                "truncate_conversation",
            ],
        }
    ):
        raise RuntimeError(
            "realtime voice provider interruption authority contract does not match"
        )
    if playback_config != {"acknowledgements": "required", "reportInterval": "100ms"}:
        raise RuntimeError(
            "realtime voice playback acknowledgement contract does not match"
        )
    transport_config = session_node.get("config", {}).get("transport")
    if (
        session_node.get("block") != "realtime.session@1"
        or session_node.get("bindings") != {"provider": "realtime-provider"}
        or transport_config
        != {
            "kind": "provider_realtime",
            "uri": "wss://realtime.example.com/v1/sessions",
            "codec": "pcm16",
            "sampleRateHz": 24000,
            "channels": 1,
        }
        or tools_node.get("block") != "tools.dispatch@1"
        or tools_node.get("inputs") != {"calls": "session.toolCalls"}
        or tools_node.get("outputs") != {"results": "session.toolResults"}
        or telemetry_node.get("block") != "telemetry.voice_session@1"
    ):
        raise RuntimeError(
            "realtime voice session, tool, and telemetry dataflow does not match"
        )
    if (
        provider.get("adapterId") != "openai-realtime"
        or provider.get("authSecretRef") != "secret://providers/openai-realtime"
        or provider.get("defaultModel") != "gpt-realtime"
        or provider.get("defaultInstructions") != "Use concise voice-safe answers."
    ):
        raise RuntimeError("realtime voice provider binding contract does not match")

    transport = voice.VoiceTransport(
        kind=str(transport_config["kind"]),
        uri=str(transport_config["uri"]),
        codec=str(transport_config["codec"]),
        sample_rate_hz=int(transport_config["sampleRateHz"]),
        channels=int(transport_config["channels"]),
    )
    session = voice.DuplexSession(
        "voice-session-1",
        transport,
        started_at_ms=0,
        metadata={"provider": str(provider["adapterId"])},
    ).begin_turn("voice-turn-1")
    request = voice.RealtimeSessionRequest(
        session=session,
        model=str(provider["defaultModel"]),
        instructions=str(provider["defaultInstructions"]),
        modalities=("audio", "text"),
        tools=("knowledge.search", "ticket.create"),
    )
    local_authority = voice.VadAuthority("local-vad", speech_threshold=0.6)
    speech = local_authority.evaluate(
        voice.AudioFrame(
            "microphone", sequence=1, start_ms=0, duration_ms=20, speech_probability=0.9
        )
    )
    silence = local_authority.evaluate(
        voice.AudioFrame(
            "microphone",
            sequence=2,
            start_ms=20,
            duration_ms=20,
            speech_probability=0.1,
        )
    )
    playback = voice.PlaybackLedger().append(
        voice.PlaybackEntry(
            "playback-1",
            sequence=1,
            status="queued",
            audio_ref="artifact://voice/playback-1",
        )
    )
    started = playback.start("playback-1", occurred_at_ms=5)
    classifier = voice.InterruptionClassifier(
        "adaptive-barge-in",
        provider_authority_id=str(turn_detection["authority"]),
    )
    advisory = classifier.classify(
        session_id=session.session_id,
        vad_decision=speech,
        playback=started,
        occurred_at_ms=20,
    )
    confirmed = classifier.classify(
        session_id=session.session_id,
        vad_decision=silence,
        playback=started,
        occurred_at_ms=25,
        provider_decision=voice.ProviderInterruptionDecision(
            authority_id="provider",
            session_id=session.session_id,
            kind="interrupt",
            occurred_at_ms=25,
            reason="provider_confirmed_barge_in",
        ),
    )
    interrupted = started.interrupt_active(
        occurred_at_ms=confirmed.occurred_at_ms,
        reason=str(confirmed.reason),
    )
    acknowledged = interrupted.acknowledge("playback-1", occurred_at_ms=30)
    repeated_acknowledgement_is_idempotent = (
        acknowledged.acknowledge("playback-1", occurred_at_ms=30) == acknowledged
    )
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "session": {
            "state": session.state,
            "currentTurnId": session.current_turn_id,
            "providerContract": request.provider_contract(),
        },
        "interruption": {
            "localVadKind": speech.kind,
            "advisoryKind": advisory.kind,
            "advisoryReason": advisory.reason,
            "advisoryInterruptedIds": list(advisory.interrupted_playback_ids),
            "confirmedLocalVadKind": silence.kind,
            "confirmedKind": confirmed.kind,
            "confirmedReason": confirmed.reason,
            "confirmedInterruptedIds": list(confirmed.interrupted_playback_ids),
        },
        "playback": {
            "status": acknowledged.entries[0].status,
            "startedAtMs": acknowledged.entries[0].started_at_ms,
            "completedAtMs": acknowledged.entries[0].completed_at_ms,
            "acknowledgedAtMs": acknowledged.entries[0].acknowledged_at_ms,
            "reason": acknowledged.entries[0].reason,
            "idempotentAcknowledgement": repeated_acknowledgement_is_idempotent,
            "digest": acknowledged.content_digest(),
        },
    }


def _duplex_session_contract_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_realtime_voice_agent(application, scenario_path)
    session = evidence["session"]
    provider_contract = (
        session.get("providerContract") if isinstance(session, Mapping) else None
    )
    if (
        not isinstance(session, Mapping)
        or session.get("state") != "open"
        or session.get("currentTurnId") != "voice-turn-1"
        or not isinstance(provider_contract, Mapping)
        or provider_contract.get("sessionId") != "voice-session-1"
        or provider_contract.get("transport", {}).get("kind") != "provider_realtime"  # type: ignore[union-attr]
        or provider_contract.get("modalities") != ["audio", "text"]
        or provider_contract.get("tools") != ["knowledge.search", "ticket.create"]
    ):
        raise RuntimeError("duplex realtime session evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "duplex session contract check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "session": session,
        }
    )


def _interruption_authority_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_realtime_voice_agent(application, scenario_path)
    interruption = evidence["interruption"]
    if not isinstance(interruption, Mapping) or interruption != {
        "localVadKind": "speech_start",
        "advisoryKind": "continue",
        "advisoryReason": "awaiting_provider_confirmation",
        "advisoryInterruptedIds": [],
        "confirmedLocalVadKind": "silence",
        "confirmedKind": "interrupt",
        "confirmedReason": "provider_confirmed_barge_in",
        "confirmedInterruptedIds": ["playback-1"],
    }:
        raise RuntimeError(
            "provider-confirmed interruption authority evidence is incomplete"
        )
    return 0, canonical_dumps(
        {
            "gate": "interruption authority check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "interruption": interruption,
        }
    )


def _playback_ledger_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_realtime_voice_agent(application, scenario_path)
    playback = evidence["playback"]
    if (
        not isinstance(playback, Mapping)
        or playback.get("status") != "interrupted"
        or playback.get("startedAtMs") != 5
        or playback.get("completedAtMs") != 25
        or playback.get("acknowledgedAtMs") != 30
        or playback.get("reason") != "provider_confirmed_barge_in"
        or playback.get("idempotentAcknowledgement") is not True
    ):
        raise RuntimeError(
            "ordered acknowledged playback ledger evidence is incomplete"
        )
    return 0, canonical_dumps(
        {
            "gate": "playback ledger check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "playback": playback,
        }
    )


def _exercise_telemetry_outage_correctness(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "telemetry-outage-correctness":
        raise RuntimeError(
            "telemetry semantic gate requires the telemetry-outage-correctness application"
        )
    try:
        audit_module = importlib.import_module("graphblocks.audit")
        langfuse_module = importlib.import_module("graphblocks.integrations.langfuse")
        otel_module = importlib.import_module("graphblocks.integrations.otel")
        telemetry_module = importlib.import_module("graphblocks.telemetry")
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "telemetry semantic gates require bundled GraphBlocks observability support"
        ) from error
    documents = load_documents(scenario_path)
    if len(documents) != 1 or documents[0].get("kind") != "ObservabilityProfile":
        raise RuntimeError(
            "telemetry outage scenario must contain exactly one ObservabilityProfile"
        )
    profile = documents[0]
    profile_spec = profile.get("spec")
    if not isinstance(profile_spec, Mapping):
        raise RuntimeError("telemetry outage profile spec must be a mapping")
    capture = profile_spec.get("capture")
    metrics = profile_spec.get("metrics")
    exporters = profile_spec.get("exporters")
    durable_records = profile_spec.get("durableRecords")
    if any(
        not isinstance(value, Mapping)
        for value in (capture, metrics, exporters, durable_records)
    ):
        raise RuntimeError(
            "telemetry capture, metric, exporter, and durable-record contracts are required"
        )
    assert isinstance(capture, Mapping)
    assert isinstance(metrics, Mapping)
    assert isinstance(exporters, Mapping)
    assert isinstance(durable_records, Mapping)
    if capture != {
        "messages": "redacted",
        "documentContent": "reference_only",
        "toolArguments": "schema_only",
        "toolResults": "metadata",
        "embeddings": "none",
        "rawFiles": "none",
    }:
        raise RuntimeError(
            "telemetry capture policy does not match the privacy contract"
        )
    allowed_dimensions = [
        "environment",
        "release_channel",
        "graph_id",
        "block_type",
        "target_id",
        "provider",
        "outcome",
    ]
    forbidden_dimensions = [
        "run_id",
        "user_id",
        "conversation_id",
        "document_id",
        "chunk_id",
    ]
    if (
        metrics.get("dimensions") != allowed_dimensions
        or metrics.get("forbiddenDimensions") != forbidden_dimensions
        or set(allowed_dimensions) & set(forbidden_dimensions)
    ):
        raise RuntimeError(
            "telemetry metrics must forbid high-cardinality identity dimensions"
        )
    otlp_config = exporters.get("otlp")
    langfuse_config = exporters.get("langfuse")
    if not isinstance(otlp_config, Mapping) or otlp_config != {
        "endpoint": "otel-gateway:4317"
    }:
        raise RuntimeError("telemetry OTel exporter contract does not match")
    if not isinstance(langfuse_config, Mapping) or langfuse_config != {
        "mode": "collector",
        "project": "company-ai",
    }:
        raise RuntimeError("telemetry Langfuse collector contract does not match")
    if durable_records != {
        "executionJournal": "run-postgres",
        "auditLog": {
            "sink": "audit-postgres",
            "delivery": "transactional_outbox",
            "retention": "7y",
        },
        "usageLedger": {
            "sink": "usage-postgres",
            "deduplicateBy": ["provider_response_id", "node_attempt_id"],
        },
        "budgetLedger": "budget-postgres",
    }:
        raise RuntimeError("telemetry outage durable correctness stores do not match")

    record = telemetry_module.GenerationTelemetryRecord(
        record_id="generation-telemetry-1",
        run_id="run-telemetry-1",
        span_id="span-telemetry-1",
        node_id="answer",
        provider="openai-compatible",
        model="gpt-test",
        release_id="release-telemetry-1",
        input_digest="sha256:input",
        output_digest="sha256:output",
        usage={"input_tokens": 20, "output_tokens": 8},
        attributes={
            "environment": "production",
            "api_key": "must-not-appear",
            "prompt": "private prompt must not appear",
        },
    )
    cardinality_linter = telemetry_module.MetricCardinalityLinter(
        blocked_labels=tuple(forbidden_dimensions),
    )
    allowed_cardinality = cardinality_linter.lint_samples(
        (
            {
                "name": "graphblocks_generation_total",
                "labels": {
                    dimension: f"value-{dimension}" for dimension in allowed_dimensions
                },
                "value": 1,
            },
        )
    )
    blocked_cardinality = cardinality_linter.lint_samples(
        (
            {
                "name": "graphblocks_generation_total",
                "labels": {"run_id": "run-telemetry-1"},
                "value": 1,
            },
        )
    )
    if not allowed_cardinality.passed or blocked_cardinality.issue_contracts() != [
        {
            "metric_name": "graphblocks_generation_total",
            "label": "run_id",
            "distinct_values": 1,
            "limit": 0,
            "reason": "blocked_label",
        }
    ]:
        raise RuntimeError(
            "telemetry metric cardinality policy did not enforce forbidden dimensions"
        )
    otel_projection = otel_module.otlp_span_from_generation(
        record,
        schema_url="https://opentelemetry.io/schemas/1.27.0",
    ).span_contract()
    langfuse_projection = langfuse_module.langfuse_generation_from_observation(
        record,
        trace_id="trace-telemetry-1",
    ).generation_contract()
    if "must-not-appear" in repr(
        otel_projection
    ) or "private prompt must not appear" in repr(otel_projection):
        raise RuntimeError("OTel projection exposed protected telemetry content")
    if "must-not-appear" in repr(
        langfuse_projection
    ) or "private prompt must not appear" in repr(langfuse_projection):
        raise RuntimeError("Langfuse projection exposed protected telemetry content")

    with TemporaryDirectory(prefix="graphblocks-telemetry-acceptance-") as directory:
        journal = SQLiteExecutionJournal(
            Path(directory) / "journal.sqlite3", "run-telemetry-1"
        )
        audit = audit_module.SQLiteAuditOutbox(Path(directory) / "audit.sqlite3")
        usage = SQLiteUsageLedger(Path(directory) / "usage.sqlite3")
        budget = SQLiteBudgetLedger(Path(directory) / "budget.sqlite3")
        try:
            journal.append("run_started", {"graphHash": "sha256:graph"})
            journal.append(
                "node_succeeded", {"node": "answer", "outputDigest": "sha256:output"}
            )
            journal.append_terminal("run_succeeded", {"outputDigest": "sha256:output"})
            audit.append(
                "application_event",
                {
                    "event_id": "event-1",
                    "kind": "RunSucceeded",
                    "run_id": "run-telemetry-1",
                },
                occurred_at="2026-07-10T00:00:00Z",
                record_id="audit-telemetry-1",
            )
            usage.append(
                UsageRecord(
                    record_id="usage-telemetry-1",
                    source="provider_reported",
                    confidence="provider_exact",
                    amounts=(
                        UsageAmount("model_total_tokens", Decimal("28"), "tokens"),
                    ),
                    occurred_at="2026-07-10T00:00:00Z",
                    run_id="run-telemetry-1",
                    attempt_id="attempt-1",
                    provider_response_id="response-1",
                )
            )
            budget.allocate(
                "budget-telemetry-1",
                PolicyResourceRef("tenant:acme", resource_kind="tenant"),
                [UsageAmount("model_total_tokens", Decimal("100"), "tokens")],
                policy_ref="policy-1",
            )
            reservation = budget.reserve(
                "budget-telemetry-1",
                PolicyResourceRef("run-telemetry-1", resource_kind="run"),
                [UsageAmount("model_total_tokens", Decimal("40"), "tokens")],
                purpose="provider_call",
                expires_at="2026-07-10T01:00:00Z",
                reservation_id="reservation-telemetry-1",
            )
            budget.commit(
                reservation.reservation_id,
                [UsageAmount("model_total_tokens", Decimal("28"), "tokens")],
            )
            balance = budget.balance("budget-telemetry-1")
            baseline = telemetry_module.TelemetryCorrectnessSnapshot.capture(
                execution_journal=[entry.to_dict() for entry in journal.records],
                audit_log=[
                    {
                        "record_id": entry.record_id,
                        "payload_digest": entry.payload_digest,
                        "status": entry.status,
                    }
                    for entry in audit.pending()
                ],
                usage_ledger=[
                    {
                        "record_id": entry.record_id,
                        "amounts": [
                            {
                                "kind": amount.kind,
                                "amount": str(amount.amount),
                                "unit": amount.unit,
                            }
                            for amount in entry.amounts
                        ],
                    }
                    for entry in usage.records_for_run("run-telemetry-1")
                ],
                budget_ledger={
                    "budget_id": balance.budget_id,
                    "revision": balance.revision,
                    "committed": [
                        {
                            "kind": amount.kind,
                            "amount": str(amount.amount),
                            "unit": amount.unit,
                        }
                        for amount in balance.committed
                    ],
                },
            )
            outbox = telemetry_module.TelemetryExportOutbox()
            outbox.accept((record,))
            outbox.accept((record,))
            failed = outbox.attempt_export(
                "otlp",
                lambda records: (_ for _ in ()).throw(
                    TimeoutError("collector unavailable")
                ),
                correctness_probe=lambda: baseline,
                retryable=True,
            )
            balance_after_failure = budget.balance("budget-telemetry-1")
            after_failure = telemetry_module.TelemetryCorrectnessSnapshot.capture(
                execution_journal=[entry.to_dict() for entry in journal.records],
                audit_log=[
                    {
                        "record_id": entry.record_id,
                        "payload_digest": entry.payload_digest,
                        "status": entry.status,
                    }
                    for entry in audit.pending()
                ],
                usage_ledger=[
                    {
                        "record_id": entry.record_id,
                        "amounts": [
                            {
                                "kind": amount.kind,
                                "amount": str(amount.amount),
                                "unit": amount.unit,
                            }
                            for amount in entry.amounts
                        ],
                    }
                    for entry in usage.records_for_run("run-telemetry-1")
                ],
                budget_ledger={
                    "budget_id": balance_after_failure.budget_id,
                    "revision": balance_after_failure.revision,
                    "committed": [
                        {
                            "kind": amount.kind,
                            "amount": str(amount.amount),
                            "unit": amount.unit,
                        }
                        for amount in balance_after_failure.committed
                    ],
                },
            )
            delivered_ids: list[str] = []
            recovered = outbox.attempt_export(
                "otlp",
                lambda records: delivered_ids.extend(
                    item.record_id for item in records
                ),
                correctness_probe=lambda: after_failure,
                retryable=True,
            )
            redundant = outbox.attempt_export(
                "otlp",
                lambda records: delivered_ids.extend(
                    item.record_id for item in records
                ),
                correctness_probe=lambda: after_failure,
                retryable=True,
            )
            langfuse_delivery = outbox.attempt_export(
                "langfuse",
                lambda records: None,
                correctness_probe=lambda: after_failure,
            )
            final_balance = budget.balance("budget-telemetry-1")
            final_snapshot = telemetry_module.TelemetryCorrectnessSnapshot.capture(
                execution_journal=[entry.to_dict() for entry in journal.records],
                audit_log=[
                    {
                        "record_id": entry.record_id,
                        "payload_digest": entry.payload_digest,
                        "status": entry.status,
                    }
                    for entry in audit.pending()
                ],
                usage_ledger=[
                    {
                        "record_id": entry.record_id,
                        "amounts": [
                            {
                                "kind": amount.kind,
                                "amount": str(amount.amount),
                                "unit": amount.unit,
                            }
                            for amount in entry.amounts
                        ],
                    }
                    for entry in usage.records_for_run("run-telemetry-1")
                ],
                budget_ledger={
                    "budget_id": final_balance.budget_id,
                    "revision": final_balance.revision,
                    "committed": [
                        {
                            "kind": amount.kind,
                            "amount": str(amount.amount),
                            "unit": amount.unit,
                        }
                        for amount in final_balance.committed
                    ],
                },
            )
            return {
                "applicationDigest": canonical_hash(application.application_contract()),
                "scenarioDigest": canonical_hash(documents),
                "otel": otel_projection,
                "langfuse": langfuse_projection,
                "cardinality": {
                    "allowedPassed": allowed_cardinality.passed,
                    "blockedIssues": blocked_cardinality.issue_contracts(),
                },
                "outage": {
                    "failed": failed.evaluation_contract(),
                    "recovered": recovered.evaluation_contract(),
                    "redundant": redundant.evaluation_contract(),
                    "langfuseDelivery": langfuse_delivery.evaluation_contract(),
                    "baselineDigest": baseline.digest,
                    "afterFailureDigest": after_failure.digest,
                    "finalDigest": final_snapshot.digest,
                    "deliveredIds": delivered_ids,
                    "journalRecordCount": len(journal.records),
                    "auditRecordIds": [entry.record_id for entry in audit.pending()],
                    "usageRecordIds": [
                        entry.record_id
                        for entry in usage.records_for_run("run-telemetry-1")
                    ],
                    "budgetCommitted": [
                        str(amount.amount) for amount in final_balance.committed
                    ],
                },
            }
        finally:
            journal.close()
            audit.close()
            usage.close()
            budget.close()


def _otel_projection_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_telemetry_outage_correctness(application, scenario_path)
    otel = evidence["otel"]
    cardinality = evidence["cardinality"]
    attributes = otel.get("attributes") if isinstance(otel, Mapping) else None
    if (
        not isinstance(otel, Mapping)
        or otel.get("schema_url") != "https://opentelemetry.io/schemas/1.27.0"
        or otel.get("span_id") != "span-telemetry-1"
        or not isinstance(attributes, Mapping)
        or attributes.get("graphblocks.attribute.environment") != "production"
        or attributes.get("graphblocks.attribute.api_key") != "[redacted]"
        or "graphblocks.attribute.prompt" in attributes
        or otel.get("metrics") != {"usage.input_tokens": 20, "usage.output_tokens": 8}
        or not isinstance(cardinality, Mapping)
        or cardinality.get("allowedPassed") is not True
        or cardinality.get("blockedIssues")
        != [
            {
                "metric_name": "graphblocks_generation_total",
                "label": "run_id",
                "distinct_values": 1,
                "limit": 0,
                "reason": "blocked_label",
            }
        ]
    ):
        raise RuntimeError("OTel generation projection evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "OTel projection check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "otel": otel,
            "cardinality": cardinality,
        }
    )


def _langfuse_projection_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_telemetry_outage_correctness(application, scenario_path)
    langfuse = evidence["langfuse"]
    metadata = langfuse.get("metadata") if isinstance(langfuse, Mapping) else None
    attributes = metadata.get("attributes") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(langfuse, Mapping)
        or langfuse.get("trace_id") != "trace-telemetry-1"
        or langfuse.get("generation_id") != "span-telemetry-1"
        or not isinstance(attributes, Mapping)
        or attributes.get("environment") != "production"
        or attributes.get("api_key") != "[redacted]"
        or "prompt" in attributes
        or langfuse.get("usage") != {"input_tokens": 20, "output_tokens": 8}
    ):
        raise RuntimeError("Langfuse generation projection evidence is incomplete")
    return 0, canonical_dumps(
        {
            "gate": "Langfuse projection check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "langfuse": langfuse,
        }
    )


def _telemetry_outage_correctness_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_telemetry_outage_correctness(application, scenario_path)
    outage = evidence["outage"]
    failed = outage.get("failed") if isinstance(outage, Mapping) else None
    recovered = outage.get("recovered") if isinstance(outage, Mapping) else None
    redundant = outage.get("redundant") if isinstance(outage, Mapping) else None
    if (
        not isinstance(outage, Mapping)
        or not isinstance(failed, Mapping)
        or not isinstance(recovered, Mapping)
        or not isinstance(redundant, Mapping)
        or failed.get("result", {}).get("status") != "failed"  # type: ignore[union-attr]
        or failed.get("result", {}).get("run_impact") != "none"  # type: ignore[union-attr]
        or failed.get("pending_record_ids") != ["generation-telemetry-1"]
        or recovered.get("result", {}).get("status") != "completed"  # type: ignore[union-attr]
        or recovered.get("pending_record_ids") != []
        or redundant.get("result", {}).get("record_ids") != []  # type: ignore[union-attr]
        or outage.get("baselineDigest") != outage.get("afterFailureDigest")
        or outage.get("baselineDigest") != outage.get("finalDigest")
        or outage.get("deliveredIds") != ["generation-telemetry-1"]
        or outage.get("journalRecordCount") != 3
        or outage.get("auditRecordIds") != ["audit-telemetry-1"]
        or outage.get("usageRecordIds") != ["usage-telemetry-1"]
        or outage.get("budgetCommitted") != ["28"]
    ):
        raise RuntimeError(
            "telemetry outage durable correctness evidence is incomplete"
        )
    return 0, canonical_dumps(
        {
            "gate": "telemetry outage correctness check",
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "outage": outage,
        }
    )


def _exercise_direct_file_analysis(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "direct-file-analysis":
        raise RuntimeError(
            "direct-file semantic gate requires the direct-file acceptance application"
        )
    documents = load_documents(scenario_path)
    if len(documents) != 1 or documents[0].get("kind") != "Graph":
        raise RuntimeError("direct-file acceptance scenario must contain one graph")
    graph = documents[0]
    spec = graph.get("spec")
    if not isinstance(spec, Mapping):
        raise RuntimeError("direct-file acceptance graph spec must be a mapping")
    nodes = spec.get("nodes")
    if not isinstance(nodes, Mapping):
        raise RuntimeError("direct-file acceptance graph nodes must be a mapping")
    analyze = nodes.get("analyze")
    generated = nodes.get("generateArtifact")
    bundle_node = nodes.get("bundle")
    if (
        not isinstance(analyze, Mapping)
        or not isinstance(generated, Mapping)
        or not isinstance(bundle_node, Mapping)
    ):
        raise RuntimeError(
            "direct-file scenario must declare analysis, artifact, and result bundle nodes"
        )
    analyze_config = analyze.get("config")
    generated_config = generated.get("config")
    bundle_config = bundle_node.get("config")
    analyze_inputs = analyze.get("inputs")
    generated_inputs = generated.get("inputs")
    bundle_inputs = bundle_node.get("inputs")
    if (
        analyze.get("block") != "document.analyze_direct@1"
        or generated.get("block") != "artifact.generate@1"
        or bundle_node.get("block") != "result.bundle@1"
        or analyze_inputs
        != {
            "files": "$input.files",
            "question": "$input.question",
            "snapshot": "snapshot.value",
        }
        or generated_inputs != {"analysis": "analyze.result"}
        or bundle_inputs
        != {
            "outputs": "analyze.results",
            "evidence": "analyze.sourceRefs",
            "artifacts": "generateArtifact.artifacts",
        }
        or bundle_node.get("outputs") != {"result": "$output.result"}
    ):
        raise RuntimeError(
            "direct-file analysis and generated artifact dataflow does not match"
        )
    if (
        not isinstance(analyze_config, Mapping)
        or analyze_config.get("requireSourceRef") is not True
        or analyze_config.get("preserveDocumentSpan") is not True
    ):
        raise RuntimeError(
            "direct-file analysis must require source refs with document spans"
        )
    if (
        not isinstance(generated_config, Mapping)
        or generated_config.get("mediaType") != "text/markdown"
        or generated_config.get("filename") != "analysis.md"
        or generated_config.get("requireChecksum") is not True
        or not isinstance(bundle_config, Mapping)
        or bundle_config.get("requireGeneratedArtifact") is not True
    ):
        raise RuntimeError(
            "direct-file scenario must require a checksummed generated artifact"
        )

    source_text = "Alpha policy requires audit logs.\n"
    asset, revision = create_local_text_revision(
        "file:///acceptance/source.txt",
        source_text,
        observed_at="2026-07-10T00:00:00Z",
        filename="source.txt",
    )
    document = parse_plain_text_document(asset, revision, source_text)
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    if len(chunks) != 1 or len(chunks[0].source_refs) != 1:
        raise RuntimeError(
            "direct-file source lineage did not produce one source reference"
        )
    source_ref = chunks[0].source_refs[0]
    if source_ref.locator is None:
        raise RuntimeError(
            "direct-file source reference did not retain a document span"
        )
    artifact_body = b"# Analysis\n\nAlpha policy requires audit logs.\n"
    with TemporaryDirectory(prefix="graphblocks-direct-file-") as directory:
        store = LocalBlobStore(directory)
        artifact = store.put(
            BlobKey("outputs/analysis.md"),
            artifact_body,
            PutOptions(media_type="text/markdown", filename="analysis.md"),
        )
        persisted_body_matches = (
            store.get(BlobKey("outputs/analysis.md")) == artifact_body
        )
        persisted_metadata = store.head(BlobKey("outputs/analysis.md"))
    bundle = ResultBundle(
        bundle_id="bundle-direct-file-1",
        run_id="run-direct-file-1",
        release_id="release-direct-file-1",
        inputs=[],
        outputs=[],
        artifacts=[artifact],
    )
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "source": {
            "sourceId": source_ref.source_id,
            "digest": source_ref.digest,
            "assetId": source_ref.locator.asset_id,
            "revisionId": source_ref.locator.revision_id,
            "documentId": source_ref.locator.document_id,
            "chunkId": source_ref.locator.chunk_id,
            "elementId": source_ref.locator.element_id,
        },
        "artifact": {
            "artifactId": artifact.artifact_id,
            "checksum": artifact.checksum,
            "mediaType": artifact.media_type,
            "filename": artifact.filename,
            "sizeBytes": artifact.size_bytes,
            "persistedBodyMatches": persisted_body_matches,
            "metadataChecksumMatches": persisted_metadata.artifact.checksum
            == artifact.checksum,
            "bundleArtifactIds": [item.artifact_id for item in bundle.artifacts],
        },
    }


def _exercise_document_ingestion(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "document-ingestion":
        raise RuntimeError(
            "document semantic gate requires the document-ingestion application"
        )
    documents = load_documents(scenario_path)
    if len(documents) < 2:
        raise RuntimeError(
            "document-ingestion scenario must contain job and item graphs"
        )
    item_graph = next(
        (
            document
            for document in documents
            if isinstance(document.get("metadata"), Mapping)
            and document["metadata"].get("name") == "process-single-asset"
        ),
        None,
    )
    if not isinstance(item_graph, Mapping):
        raise RuntimeError("document-ingestion item graph is missing")
    item_spec = item_graph.get("spec")
    if not isinstance(item_spec, Mapping) or not isinstance(
        item_spec.get("nodes"), Mapping
    ):
        raise RuntimeError("document-ingestion item graph nodes must be a mapping")
    item_nodes = item_spec["nodes"]
    convert = item_nodes.get("convert")
    persist = item_nodes.get("persist")
    commit = item_nodes.get("commit")
    if (
        not isinstance(convert, Mapping)
        or not isinstance(persist, Mapping)
        or not isinstance(commit, Mapping)
    ):
        raise RuntimeError(
            "document-ingestion conversion, persistence, and commit nodes are missing"
        )
    convert_config = convert.get("config")
    persist_bindings = persist.get("bindings")
    persist_config = persist.get("config")
    if (
        convert.get("block") != "document.convert@1"
        or persist.get("block") != "ingestion.persist_staging@1"
        or commit.get("block") != "ingestion.commit_revision@1"
        or convert.get("inputs") != {"asset": "load.value"}
        or persist.get("inputs")
        != {
            "transaction": "begin.transaction",
            "document": "redact.document",
            "chunks": "split.chunks",
        }
        or commit.get("inputs")
        != {
            "transaction": "begin.transaction",
            "staged": "persist.result",
        }
    ):
        raise RuntimeError(
            "document-ingestion conversion and commit dataflow does not match"
        )
    if (
        not isinstance(convert_config, Mapping)
        or convert_config.get("strategy") != "locked_auto"
    ):
        raise RuntimeError("document-ingestion parser strategy must remain locked_auto")
    candidates_by_media_type = convert_config.get("candidates")
    if not isinstance(candidates_by_media_type, Mapping):
        raise RuntimeError("document-ingestion parser candidates must be a mapping")
    pdf_candidates = candidates_by_media_type.get("application/pdf")
    if not isinstance(pdf_candidates, list) or len(pdf_candidates) < 2:
        raise RuntimeError(
            "document-ingestion PDF parser chain requires primary and fallback candidates"
        )
    candidate_pairs: list[tuple[str, str]] = []
    for candidate in pdf_candidates:
        if not isinstance(candidate, Mapping):
            raise RuntimeError("document-ingestion parser candidate must be a mapping")
        implementation = candidate.get("implementation")
        version = candidate.get("version")
        if not isinstance(implementation, str) or not isinstance(version, str):
            raise RuntimeError(
                "document-ingestion parser candidate identity is incomplete"
            )
        candidate_pairs.append((implementation, version))
    if (
        not isinstance(persist_bindings, Mapping)
        or persist_bindings.get("index") != "knowledge-index-staging"
    ):
        raise RuntimeError(
            "document-ingestion persistence must use the staging knowledge index"
        )
    if not isinstance(persist_config, Mapping) or persist_config != {
        "requireAclRevision": True,
        "propagateAclTo": ["document", "chunks", "index"],
    }:
        raise RuntimeError("document-ingestion ACL propagation contract does not match")

    parser_attempts: list[str] = []

    def primary(source: io.BytesIO) -> object:
        parser_attempts.append(candidate_pairs[0][0])
        raise PdfParserError("Marker parser quality gate failed")

    def fallback(body: bytes) -> list[PdfPageText]:
        parser_attempts.append(candidate_pairs[1][0])
        return [PdfPageText(page_number=1, text=body.decode("utf-8").strip())]

    registry = DocumentParserRegistry()
    registry.register(
        marker_pdf_parser_descriptor(
            converter=primary,
            html_text_extractor=lambda value: value,
            processor_id=candidate_pairs[0][0],
            version=candidate_pairs[0][1],
        )
    )
    registry.register(
        pdf_parser_descriptor(
            extractor=fallback,
            processor_id=candidate_pairs[1][0],
            version=candidate_pairs[1][1],
        )
    )
    source_text = "Restricted policy requires approval.\n"
    asset, base_revision = create_local_text_revision(
        "file:///acceptance/restricted.pdf",
        source_text,
        observed_at="2026-07-10T00:00:00Z",
        filename="restricted.pdf",
    )
    revision = replace(
        base_revision,
        artifact=replace(base_revision.artifact, media_type="application/pdf"),
        acl={"tenant_id": "acme", "groups": ["compliance"]},
    )
    parsed = registry.parse_with_candidates(
        asset,
        revision,
        source_text.encode("utf-8"),
        tuple(candidate_pairs),
    )
    chunks = chunk_document_by_lines(parsed.document, revision, max_elements=1)
    index = InMemoryKnowledgeIndex("knowledge-index-staging")
    index.upsert_chunks(chunks)
    published = index.publish_revision(asset.asset_id, revision.revision_id)
    hits = index.retriever("knowledge-index-read").search("approval", top_k=1)
    authorized = authorize_search_hits(
        hits,
        AuthContext(
            tenant_id="acme",
            principal_id="user-1",
            groups={"compliance"},
            roles=set(),
        ),
    )
    unauthorized = authorize_search_hits(
        hits,
        AuthContext(
            tenant_id="acme",
            principal_id="user-2",
            groups=set(),
            roles=set(),
        ),
    )
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "parser": {
            "attempts": parser_attempts,
            "failed": [lock.processor_id for lock in parsed.failed_locks],
            "selected": parsed.selected_lock.processor_id,
            "reason": parsed.selected_lock.reason,
        },
        "acl": {
            "revision": revision.acl,
            "chunk": chunks[0].acl,
            "retrieval": hits[0].item.acl,
            "authorizedHitIds": [hit.hit_id for hit in authorized],
            "unauthorizedHitIds": [hit.hit_id for hit in unauthorized],
            "publishedChunkIds": list(published.published_chunk_ids),
        },
    }


def _exercise_enterprise_rag(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "enterprise-rag":
        raise RuntimeError("RAG semantic gate requires the enterprise-rag application")
    documents = load_documents(scenario_path)
    graph = next(
        (document for document in documents if document.get("kind") == "Graph"), None
    )
    if not isinstance(graph, Mapping) or not isinstance(graph.get("spec"), Mapping):
        raise RuntimeError("enterprise RAG graph is missing")
    nodes = graph["spec"].get("nodes")
    if not isinstance(nodes, Mapping):
        raise RuntimeError("enterprise RAG graph nodes must be a mapping")
    validate_node = nodes.get("validate")
    retrieve_node = nodes.get("retrieve")
    fuse_node = nodes.get("fuse")
    if (
        not isinstance(validate_node, Mapping)
        or not isinstance(retrieve_node, Mapping)
        or not isinstance(fuse_node, Mapping)
    ):
        raise RuntimeError(
            "enterprise RAG retrieval, fusion, and validation nodes are required"
        )
    validate_config = validate_node.get("config")
    fuse_config = fuse_node.get("config")
    if (
        retrieve_node.get("block") != "retrieve.execute_plan@1"
        or fuse_node.get("block") != "retrieve.fuse@1"
        or validate_node.get("block") != "answer.validate_grounding@1"
        or validate_node.get("inputs")
        != {
            "response": "generate.response",
            "context": "context.pack",
        }
    ):
        raise RuntimeError(
            "enterprise RAG retrieval and validation dataflow does not match"
        )
    if (
        not isinstance(validate_config, Mapping)
        or validate_config.get("requireCitation") is not True
        or validate_config.get("onInsufficientEvidence") != "abstain"
        or not isinstance(fuse_config, Mapping)
        or fuse_config.get("deduplicateBy") != "canonical_source"
    ):
        raise RuntimeError(
            "enterprise RAG citation and abstention contract does not match"
        )

    source_text = "Alpha policy requires audit logs.\n"
    asset, revision = create_local_text_revision(
        "file:///acceptance/rag-policy.txt",
        source_text,
        observed_at="2026-07-10T00:00:00Z",
    )
    document = parse_plain_text_document(asset, revision, source_text)
    chunks = chunk_document_by_lines(document, revision, max_elements=1)
    hits = InMemoryChunkRetriever(chunks, retriever_id="acceptance-local").search(
        "audit", top_k=1
    )
    context = ContextPack(context_id="context-enterprise-rag-1", hits=hits)
    citation = Citation(
        citation_id="citation-1",
        source=hits[0].item.source,
        cited_text="requires audit logs",
    )
    answer = Answer(
        answer_id="answer-enterprise-rag-1",
        text="Alpha policy requires audit logs.",
        claims=[
            Claim(
                claim_id="claim-1",
                text="Alpha policy requires audit logs.",
                citation_ids=["citation-1"],
            )
        ],
        citations=[citation],
    )
    citation_result = validate_answer_citations(answer, context)
    grounding_result = validate_answer_grounding(answer, context)
    trace = resolve_citation_source_trace(answer, context, "citation-1")
    empty_result = validate_answer_grounding(
        answer,
        ContextPack(context_id="context-empty", hits=[]),
    )
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "citation": {
            "valid": citation_result.ok,
            "grounded": grounding_result.ok,
            "issueCodes": [issue.code for issue in citation_result.issues],
            "sourceId": trace.source.source_id,
            "hitId": trace.hit_id,
            "chunkId": None if trace.locator is None else trace.locator.chunk_id,
            "documentId": None if trace.locator is None else trace.locator.document_id,
        },
        "abstention": {
            "valid": empty_result.ok,
            "issueCodes": [issue.code for issue in empty_result.issues],
            "reason": None
            if empty_result.abstention is None
            else empty_result.abstention.reason,
        },
    }


def _exercise_multi_turn_chat(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> dict[str, object]:
    if application.application_id != "multi-turn-chat":
        raise RuntimeError(
            "conversation semantic gate requires the multi-turn-chat application"
        )
    documents = load_documents(scenario_path)
    if len(documents) != 3:
        raise RuntimeError(
            "multi-turn chat scenario must contain two policy profiles and one graph"
        )
    graph = next(
        (document for document in documents if document.get("kind") == "Graph"), None
    )
    hard_stop = next(
        (
            document
            for document in documents
            if document.get("kind") == "PolicyProfile"
            and isinstance(document.get("metadata"), Mapping)
            and document["metadata"].get("name") == "interactive-hard-stop"
        ),
        None,
    )
    if not isinstance(graph, Mapping) or not isinstance(hard_stop, Mapping):
        raise RuntimeError("multi-turn chat graph and hard-stop profile are required")
    graph_spec = graph.get("spec")
    hard_stop_spec = hard_stop.get("spec")
    if not isinstance(graph_spec, Mapping) or not isinstance(hard_stop_spec, Mapping):
        raise RuntimeError("multi-turn chat graph and policy specs must be mappings")
    nodes = graph_spec.get("nodes")
    exhaustion = hard_stop_spec.get("exhaustion")
    if not isinstance(nodes, Mapping) or not isinstance(exhaustion, Mapping):
        raise RuntimeError("multi-turn chat nodes and exhaustion policy are required")
    commit_turn = nodes.get("commitTurn")
    commit_config = (
        commit_turn.get("config") if isinstance(commit_turn, Mapping) else None
    )
    if (
        not isinstance(commit_turn, Mapping)
        or commit_turn.get("block") != "conversation.commit_turn@1"
        or commit_turn.get("inputs")
        != {
            "turn": "beginTurn.turn",
            "response": "respond.message",
        }
    ):
        raise RuntimeError("multi-turn conversation commit dataflow does not match")
    output_policy = exhaustion.get("output")
    if (
        not isinstance(commit_config, Mapping)
        or commit_config.get("concurrency") != "compare_and_swap"
    ):
        raise RuntimeError("multi-turn chat commit must use compare-and-swap")
    if (
        exhaustion.get("preset") != "hard_stop"
        or exhaustion.get("inFlight") != "cancel_immediately"
        or not isinstance(output_policy, Mapping)
        or output_policy.get("durableResult") != "retract"
    ):
        raise RuntimeError("multi-turn chat hard-stop policy must retract drafts")

    commit_store = InMemoryConversationStore()
    commit_store.create(Conversation(conversation_id="conversation-commit-1"))
    commit_store.begin_turn(
        "conversation-commit-1", expected_revision=0, turn_id="turn-commit-1"
    )
    draft = commit_store.append_turn_message(
        "turn-commit-1",
        Message(
            message_id="message-commit-1",
            role="assistant",
            parts=(ContentPart(kind="text", text="committed answer"),),
        ),
    )
    invisible_before_commit = (
        commit_store.get("conversation-commit-1").conversation.messages == ()
    )
    committed = commit_store.commit_turn("turn-commit-1")
    stale_conflict = False
    try:
        commit_store.append_messages(
            "conversation-commit-1",
            expected_revision=0,
            messages=[Message(message_id="message-stale-1", role="user")],
        )
    except ConversationConflictError:
        stale_conflict = True

    abort_store = InMemoryConversationStore()
    abort_store.create(Conversation(conversation_id="conversation-abort-1"))
    abort_store.begin_turn(
        "conversation-abort-1", expected_revision=0, turn_id="turn-abort-1"
    )
    abort_store.append_turn_message(
        "turn-abort-1",
        Message(message_id="message-abort-1", role="assistant"),
    )
    aborted = abort_store.abort_turn("turn-abort-1")
    policy_store = InMemoryConversationStore()
    policy_store.create(Conversation(conversation_id="conversation-policy-1"))
    policy_store.begin_turn(
        "conversation-policy-1", expected_revision=0, turn_id="turn-policy-1"
    )
    policy_store.append_turn_message(
        "turn-policy-1",
        Message(message_id="message-policy-1", role="assistant"),
    )
    policy_stopped = policy_store.policy_stop_turn("turn-policy-1")
    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "cas": {
            "draftStatus": draft.messages[0].status,
            "invisibleBeforeCommit": invisible_before_commit,
            "commitStatus": committed.status,
            "committedRevision": committed.committed_revision,
            "storedRevision": commit_store.get("conversation-commit-1").revision,
            "staleConflict": stale_conflict,
        },
        "draftLifecycle": {
            "abortedStatus": aborted.status,
            "abortedMessageStatuses": [message.status for message in aborted.messages],
            "abortStoredMessageCount": len(
                abort_store.get("conversation-abort-1").conversation.messages
            ),
            "policyStatus": policy_stopped.status,
            "policyMessageStatuses": [
                message.status for message in policy_stopped.messages
            ],
            "policyStoredMessageCount": len(
                policy_store.get("conversation-policy-1").conversation.messages
            ),
        },
    }


def _source_reference_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_direct_file_analysis(application, scenario_path)
    source = evidence["source"]
    if not isinstance(source, Mapping) or any(
        source.get(key) is None
        for key in (
            "sourceId",
            "digest",
            "assetId",
            "revisionId",
            "documentId",
            "chunkId",
        )
    ):
        raise RuntimeError("direct-file source reference lineage is incomplete")
    return 0, canonical_dumps({"gate": "source reference check", **evidence})


def _generated_artifact_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_direct_file_analysis(application, scenario_path)
    artifact = evidence["artifact"]
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("artifactId") != "blob:outputs/analysis.md"
        or artifact.get("mediaType") != "text/markdown"
        or artifact.get("filename") != "analysis.md"
        or not isinstance(artifact.get("checksum"), str)
        or artifact.get("persistedBodyMatches") is not True
        or artifact.get("metadataChecksumMatches") is not True
        or artifact.get("bundleArtifactIds") != ["blob:outputs/analysis.md"]
    ):
        raise RuntimeError("direct-file generated artifact evidence is incomplete")
    return 0, canonical_dumps({"gate": "generated artifact check", **evidence})


def _parser_fallback_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_document_ingestion(application, scenario_path)
    parser = evidence["parser"]
    attempts = parser.get("attempts") if isinstance(parser, Mapping) else None
    if (
        not isinstance(attempts, list)
        or len(attempts) != 2
        or not all(isinstance(attempt, str) and attempt for attempt in attempts)
        or attempts[0] == attempts[1]
        or parser.get("failed") != [attempts[0]]
        or parser.get("selected") != attempts[1]
        or parser.get("reason") != "candidate_fallback"
    ):
        raise RuntimeError("document-ingestion parser fallback evidence is incomplete")
    return 0, canonical_dumps({"gate": "parser fallback check", **evidence})


def _acl_propagation_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_document_ingestion(application, scenario_path)
    acl = evidence["acl"]
    if (
        not isinstance(acl, Mapping)
        or acl.get("revision") != {"tenant_id": "acme", "groups": ["compliance"]}
        or acl.get("chunk") != acl.get("revision")
        or acl.get("retrieval") != acl.get("revision")
        or len(acl.get("authorizedHitIds", [])) != 1
        or acl.get("unauthorizedHitIds") != []
        or len(acl.get("publishedChunkIds", [])) != 1
    ):
        raise RuntimeError("document-ingestion ACL propagation evidence is incomplete")
    return 0, canonical_dumps({"gate": "ACL propagation check", **evidence})


def _rag_citation_validation(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_enterprise_rag(application, scenario_path)
    citation = evidence["citation"]
    if (
        not isinstance(citation, Mapping)
        or citation.get("valid") is not True
        or citation.get("grounded") is not True
        or citation.get("issueCodes") != []
        or any(
            citation.get(key) is None
            for key in ("sourceId", "hitId", "chunkId", "documentId")
        )
    ):
        raise RuntimeError("enterprise RAG citation evidence is incomplete")
    return 0, canonical_dumps({"gate": "rag citation validation", **evidence})


def _abstention_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_enterprise_rag(application, scenario_path)
    abstention = evidence["abstention"]
    if not isinstance(abstention, Mapping) or abstention != {
        "valid": False,
        "issueCodes": ["grounding.insufficient_context"],
        "reason": "insufficient_context",
    }:
        raise RuntimeError("enterprise RAG abstention evidence is incomplete")
    return 0, canonical_dumps({"gate": "abstention check", **evidence})


def _conversation_cas_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_multi_turn_chat(application, scenario_path)
    cas = evidence["cas"]
    if not isinstance(cas, Mapping) or cas != {
        "draftStatus": "draft",
        "invisibleBeforeCommit": True,
        "commitStatus": "completed",
        "committedRevision": 1,
        "storedRevision": 1,
        "staleConflict": True,
    }:
        raise RuntimeError("multi-turn conversation CAS evidence is incomplete")
    return 0, canonical_dumps({"gate": "conversation CAS check", **evidence})


def _draft_retract_commit_check(
    application: AcceptanceApplication, scenario_path: Path
) -> tuple[int, str]:
    evidence = _exercise_multi_turn_chat(application, scenario_path)
    lifecycle = evidence["draftLifecycle"]
    if not isinstance(lifecycle, Mapping) or lifecycle != {
        "abortedStatus": "cancelled",
        "abortedMessageStatuses": ["retracted"],
        "abortStoredMessageCount": 0,
        "policyStatus": "policy_stopped",
        "policyMessageStatuses": ["retracted"],
        "policyStoredMessageCount": 0,
    }:
        raise RuntimeError("multi-turn draft retract evidence is incomplete")
    return 0, canonical_dumps({"gate": "draft retract commit check", **evidence})


def _exercise_coding_agent_background_callback(
    application: AcceptanceApplication,
    scenario_path: Path,
    *,
    signed_delivery: bool,
) -> dict[str, object]:
    if application.application_id != "coding-agent-background-callbacks":
        raise RuntimeError(
            "coding-agent semantic gate requires the coding-agent acceptance application"
        )
    documents = load_documents(scenario_path)
    if len(documents) != 2:
        raise RuntimeError(
            "coding-agent acceptance scenario must contain one application and one graph"
        )
    application_document, graph_document = documents
    if (
        not isinstance(application_document, Mapping)
        or application_document.get("kind") != "Application"
    ):
        raise RuntimeError(
            "coding-agent acceptance scenario application contract is missing"
        )
    if not isinstance(graph_document, Mapping) or graph_document.get("kind") != "Graph":
        raise RuntimeError("coding-agent acceptance scenario graph contract is missing")
    application_spec = application_document.get("spec")
    graph_spec = graph_document.get("spec")
    if not isinstance(application_spec, Mapping) or not isinstance(graph_spec, Mapping):
        raise RuntimeError("coding-agent acceptance scenario specs must be mappings")
    capabilities = application_spec.get("capabilities")
    required_capabilities = {
        "background_runs",
        "cursor_replay",
        "callback_subscription",
        "reconnect_resume",
    }
    if not isinstance(capabilities, list) or not required_capabilities.issubset(
        capabilities
    ):
        raise RuntimeError(
            "coding-agent acceptance scenario is missing background callback capabilities"
        )
    callback_registration = application_spec.get("callbackRegistration")
    if not isinstance(callback_registration, Mapping):
        raise RuntimeError(
            "coding-agent acceptance scenario callback registration is missing"
        )
    delivery_config = callback_registration.get("delivery")
    event_filter = callback_registration.get("event_filter")
    if not isinstance(delivery_config, Mapping) or not isinstance(
        event_filter, Mapping
    ):
        raise RuntimeError(
            "coding-agent callback registration delivery and event filter must be mappings"
        )
    signing = delivery_config.get("signing")
    if (
        delivery_config.get("kind") != "webhook"
        or not isinstance(signing, Mapping)
        or signing.get("algorithm") != "hmac-sha256"
        or signing.get("secret_ref") != "secret://callbacks/ide-relay"
    ):
        raise RuntimeError(
            "coding-agent callback registration must use the registered HMAC webhook"
        )
    graph_nodes = graph_spec.get("nodes")
    if not isinstance(graph_nodes, Mapping):
        raise RuntimeError("coding-agent acceptance graph nodes must be a mapping")
    scenario_start = graph_nodes.get("startCI")
    scenario_wait = graph_nodes.get("waitCI")
    if not isinstance(scenario_start, Mapping) or not isinstance(
        scenario_wait, Mapping
    ):
        raise RuntimeError(
            "coding-agent acceptance graph must declare startCI and waitCI"
        )
    if scenario_start.get("block") != "async.start_operation@1":
        raise RuntimeError(
            "coding-agent startCI must declare the async operation block"
        )
    if scenario_wait.get("block") != "async.await_callback@1":
        raise RuntimeError(
            "coding-agent waitCI must declare the callback checkpoint block"
        )
    scenario_start_config = scenario_start.get("config")
    scenario_wait_config = scenario_wait.get("config")
    if not isinstance(scenario_start_config, Mapping) or not isinstance(
        scenario_wait_config, Mapping
    ):
        raise RuntimeError("coding-agent callback node configs must be mappings")
    expected_resume_config = {
        "requirePolicyReevaluation": True,
        "requireBudgetReservation": True,
        "requireReleaseCompatibility": True,
        "requireOwnershipFence": True,
    }
    if (
        scenario_start_config.get("resume") != expected_resume_config
        or scenario_wait_config.get("resume") != expected_resume_config
    ):
        raise RuntimeError(
            "coding-agent callback resume fences do not match the production contract"
        )
    scenario_start_callback = scenario_start_config.get("callback")
    scenario_wait_callback = scenario_wait_config.get("callback")
    if not isinstance(scenario_start_callback, Mapping) or not isinstance(
        scenario_wait_callback, Mapping
    ):
        raise RuntimeError("coding-agent callback schema contracts must be mappings")
    callback_schema = scenario_start_callback.get("schema")
    if (
        callback_schema != "schemas/CICallback@1"
        or scenario_wait_callback.get("schema") != callback_schema
        or scenario_start_callback.get("required") is not True
    ):
        raise RuntimeError(
            "coding-agent callback schema and required-delivery contract do not match"
        )
    pre_commit_race = scenario_start_callback.get("preCommitRace")
    idempotency_key = scenario_start_config.get("idempotencyKey")
    if not isinstance(pre_commit_race, Mapping) or pre_commit_race != {
        "onEarlyCallback": "quarantine",
        "quarantineTtl": "5m",
        "onQuarantineExpired": "reject_without_resume",
        "idempotencyKey": idempotency_key,
    }:
        raise RuntimeError(
            "coding-agent early callback quarantine contract does not match"
        )
    if (
        idempotency_key != "provider_delivery_id"
        or scenario_wait_config.get("idempotencyKey") != idempotency_key
        or scenario_start_config.get("attemptFencing") is not True
        or scenario_wait_config.get("attemptFencing") is not True
    ):
        raise RuntimeError(
            "coding-agent callback idempotency and attempt fences do not match"
        )
    if (
        scenario_start_config.get("timeout") != "30m"
        or scenario_wait_config.get("timeout") != "30m"
        or scenario_wait_config.get("checkpoint") is not True
        or scenario_wait_config.get("onTimeout") != "fail"
    ):
        raise RuntimeError(
            "coding-agent callback checkpoint timeout contract does not match"
        )
    execution = graph_spec.get("execution")
    event_stream = graph_spec.get("eventStream")
    if execution != {
        "lifetime": "job",
        "durability": "checkpointed",
        "interaction": "incremental",
    }:
        raise RuntimeError(
            "coding-agent graph execution contract must remain checkpointed and incremental"
        )
    if (
        not isinstance(event_stream, Mapping)
        or event_stream.get("replayable") is not True
    ):
        raise RuntimeError("coding-agent graph event stream must remain replayable")
    routes = application_spec.get("routes")
    if not isinstance(routes, list):
        raise RuntimeError("coding-agent application routes must be a list")
    routes_by_id = {
        route.get("id"): route
        for route in routes
        if isinstance(route, Mapping) and isinstance(route.get("id"), str)
    }
    create_task_route = routes_by_id.get("create-task")
    run_events_route = routes_by_id.get("run-events")
    callback_route = routes_by_id.get("external-callback")
    if (
        not isinstance(create_task_route, Mapping)
        or create_task_route.get("responseMode") != "accepted"
    ):
        raise RuntimeError(
            "coding-agent create-task route must return an accepted invocation handle"
        )
    if (
        not isinstance(run_events_route, Mapping)
        or run_events_route.get("cursorReplay") is not True
    ):
        raise RuntimeError("coding-agent run-events route must retain cursor replay")
    if (
        not isinstance(callback_route, Mapping)
        or callback_route.get("command") != "SubmitAsyncCallback"
    ):
        raise RuntimeError(
            "coding-agent external-callback route must submit authenticated callbacks"
        )

    callback_module: object | None = None
    if signed_delivery:
        try:
            callback_module = importlib.import_module("graphblocks.callbacks")
        except ModuleNotFoundError:
            raise RuntimeError(
                "signed webhook delivery check requires GraphBlocks callback support"
            ) from None

    downstream_executions = 0

    def fail_after_callback(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        nonlocal downstream_executions
        downstream_executions += 1
        if inputs.get("callback") != {"status": "completed", "conclusion": "success"}:
            raise RuntimeError(
                "coding-agent callback payload was not restored from the checkpoint"
            )
        raise RuntimeError("acceptance terminal failure notification")

    class TrustedResumeAdmission:
        def admit(
            self,
            submission: ServerAsyncCallbackSubmission,
            checkpoint: object,
        ) -> dict[str, object]:
            operation = getattr(checkpoint, "operation", None)
            if submission.verified_by != "callback-relay":
                raise RuntimeError(
                    "coding-agent callback was not authenticated by the relay principal"
                )
            if (
                not isinstance(operation, Mapping)
                or operation.get("expected_schema") != "schemas/CICallback@1"
            ):
                raise RuntimeError(
                    "coding-agent callback checkpoint schema fence is missing"
                )
            return {
                "schema_validated": True,
                "policy_reevaluated": True,
                "budget_reserved": True,
                "release_compatible": True,
                "ownership_fenced": True,
            }

    registry = stdlib_registry(allow_untyped=True)
    registry.register("acceptance.fail-after-callback@1", fail_after_callback)
    run_id = "run-coding-agent-acceptance-1"
    operation_id = "operation-coding-agent-acceptance-1"
    fixture_started = datetime.now(timezone.utc)
    fixture_started_at = fixture_started.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    submitted_at_unix_ms = int(fixture_started.timestamp() * 1000)
    timeout_ms = 30 * 60 * 1000
    runtime_graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "coding-agent-acceptance-runtime"},
        "spec": {
            "nodes": {
                "start": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": operation_id,
                        "runId": run_id,
                        "nodeId": "start",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-coding-agent-acceptance-1",
                        "resumeTokenHash": "sha256:" + ("a" * 64),
                        "idempotencyKey": idempotency_key,
                        "expectedSchema": callback_schema,
                        "createdAtUnixMs": submitted_at_unix_ms - 1000,
                        "submittedAtUnixMs": submitted_at_unix_ms,
                        "timeoutMs": timeout_ms,
                        "resume": dict(expected_resume_config),
                        "attemptFencing": scenario_start_config["attemptFencing"],
                    },
                },
                "wait": {
                    "block": "async.await_callback@1",
                    "inputs": {"operation": "start.operation"},
                    "config": {
                        "checkpoint": True,
                        "onTimeout": scenario_wait_config["onTimeout"],
                        "timeoutMs": timeout_ms,
                        "idempotencyKey": idempotency_key,
                        "callback": {"schema": callback_schema},
                        "resume": dict(expected_resume_config),
                        "attemptFencing": scenario_wait_config["attemptFencing"],
                    },
                },
                "finish": {
                    "block": "acceptance.fail-after-callback@1",
                    "inputs": {"callback": "wait.callback"},
                },
            }
        },
    }
    app = GraphBlocksServerApp(
        registry=registry,
        auth_hook=StaticBearerAuthHook(
            {
                "user-token": PrincipalRef(
                    "coding-agent-user",
                    tenant_id="tenant-1",
                    roles=("operator",),
                ),
                "relay-token": PrincipalRef(
                    "callback-relay",
                    tenant_id="tenant-1",
                    roles=("operator",),
                ),
            }
        ),
        require_async_callback_authentication=True,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        async_callback_resume_admission_hook=TrustedResumeAdmission(),
    )
    accepted = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={"Authorization": "Bearer user-token"},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": runtime_graph,
                    "runId": run_id,
                    "responseId": "response-coding-agent-acceptance-1",
                    "releaseId": "release-coding-agent-acceptance-1",
                    "policySnapshotId": "policy-coding-agent-acceptance-1",
                    "responseMode": "accepted",
                    "occurredAt": fixture_started_at,
                }
            ).encode("utf-8"),
        )
    )
    accepted_payload = json.loads(accepted.body.decode("utf-8"))
    if accepted.status_code != 202:
        raise RuntimeError("coding-agent accepted invocation did not return 202")
    waiting = app.advance_accepted_run(run_id)
    if waiting.get("status") != "waiting_callback":
        raise RuntimeError("coding-agent accepted run did not reach waiting_callback")
    waiting_events = app.handle(
        ServerRequest(
            method="GET",
            path=f"/runs/{run_id}/events",
            headers={"Authorization": "Bearer user-token"},
            query={},
            cookies={},
        )
    )
    waiting_events_payload = json.loads(waiting_events.body.decode("utf-8"))
    waiting_event_rows = waiting_events_payload.get("events")
    if (
        waiting_events.status_code != 200
        or not isinstance(waiting_event_rows, list)
        or len(waiting_event_rows) != 2
    ):
        raise RuntimeError("coding-agent waiting callback event stream is incomplete")
    waiting_metadata = waiting_event_rows[-1].get("metadata")
    if not isinstance(waiting_metadata, Mapping) or not isinstance(
        waiting_metadata.get("occurredAt"), str
    ):
        raise RuntimeError(
            "coding-agent waiting callback event lacks an occurrence timestamp"
        )
    waiting_at = waiting_metadata["occurredAt"]
    detached = app.handle(
        ServerRequest(
            method="POST",
            path=f"/runs/{run_id}/detach",
            headers={"Authorization": "Bearer user-token"},
            query={},
            cookies={},
            body=json.dumps(
                {"clientId": "coding-agent-client-1", "reason": "network_disconnect"}
            ).encode("utf-8"),
            requested_at=waiting_at,
        )
    )
    detached_payload = json.loads(detached.body.decode("utf-8"))
    if detached.status_code != 202:
        raise RuntimeError("coding-agent run detach did not return 202")
    callback_request = ServerRequest(
        method="POST",
        path=f"/callbacks/{operation_id}",
        headers={
            "Authorization": "Bearer relay-token",
            "GraphBlocks-Idempotency-Key": "delivery-idempotency-coding-agent-1",
        },
        query={},
        cookies={},
        body=json.dumps(
            {
                "callbackId": "callback-coding-agent-acceptance-1",
                "runId": run_id,
                "nodeId": "start",
                "attemptId": "attempt-1",
                "providerOperationId": "provider-coding-agent-acceptance-1",
                "policySnapshotId": "policy-coding-agent-acceptance-1",
                "payload": {"status": "completed", "conclusion": "success"},
            }
        ).encode("utf-8"),
        requested_at=waiting_at,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        app.accepted_run_executor = executor
        callback = app.handle(callback_request)
        completion = app.wait_for_accepted_run(run_id, timeout=5)
        duplicate_callback = app.handle(callback_request)
    callback_payload = json.loads(callback.body.decode("utf-8"))
    duplicate_callback_payload = json.loads(duplicate_callback.body.decode("utf-8"))
    if callback.status_code != 202:
        raise RuntimeError("coding-agent authenticated callback was not accepted")
    if completion.get("status") != "failed":
        raise RuntimeError(
            "coding-agent resumed run did not reach its terminal fixture state"
        )
    if (
        duplicate_callback.status_code != 200
        or duplicate_callback_payload.get("status") != "duplicate"
    ):
        raise RuntimeError("coding-agent duplicate callback was not deduplicated")
    attached = app.handle(
        ServerRequest(
            method="POST",
            path=f"/runs/{run_id}/attach",
            headers={"Authorization": "Bearer user-token"},
            query={},
            cookies={},
            body=json.dumps({"lastCursor": detached_payload.get("lastCursor")}).encode(
                "utf-8"
            ),
        )
    )
    attached_payload = json.loads(attached.body.decode("utf-8"))
    if attached.status_code != 200:
        raise RuntimeError(
            "coding-agent detached run could not replay from its retained cursor"
        )
    terminal_events = app.handle(
        ServerRequest(
            method="GET",
            path=f"/runs/{run_id}/events",
            headers={"Authorization": "Bearer user-token"},
            query={},
            cookies={},
        )
    )
    terminal_events_payload = json.loads(terminal_events.body.decode("utf-8"))
    terminal_event_rows = terminal_events_payload.get("events")
    if terminal_events.status_code != 200 or not isinstance(terminal_event_rows, list):
        raise RuntimeError("coding-agent terminal event stream is unavailable")
    event_kinds = [event.get("kind") for event in terminal_event_rows]
    replay_rows = attached_payload.get("events")
    if not isinstance(replay_rows, list):
        raise RuntimeError("coding-agent attach response events must be a list")
    replay_event_kinds = [event.get("kind") for event in replay_rows]

    signed_evidence: dict[str, object] | None = None
    if signed_delivery:
        assert callback_module is not None
        secret_ref = "secret://callbacks/ide-relay"
        secret = b"coding-agent-acceptance-registered-secret"

        class RegisteredSecretResolver:
            def __init__(self) -> None:
                self.lookups: list[str] = []

            def resolve(self, requested_secret_ref: str) -> bytes:
                self.lookups.append(requested_secret_ref)
                if requested_secret_ref != secret_ref:
                    raise KeyError(requested_secret_ref)
                return secret

        class VerifyingWebhookReceiver:
            def __init__(self) -> None:
                self.requests: list[dict[str, object]] = []

            def post(
                self,
                url: str,
                *,
                body: bytes,
                headers: dict[str, str],
                resolved_addresses: tuple[str, ...],
            ) -> object:
                envelope_payload = json.loads(body)
                envelope = callback_module.CallbackEnvelope(**envelope_payload)
                verified = callback_module.verify_webhook_headers_hmac_sha256(
                    envelope,
                    headers,
                    secret,
                    now=envelope.delivered_at,
                )
                self.requests.append(
                    {
                        "url": url,
                        "verified": verified,
                        "deliveryId": envelope.delivery_id,
                        "eventId": envelope.event_id,
                        "runId": envelope.run_id,
                        "cursor": envelope.cursor,
                        "idempotencyKey": envelope.idempotency_key,
                        "resolvedAddresses": list(resolved_addresses),
                    }
                )
                return callback_module.WebhookTransportResponse(
                    202 if verified else 401
                )

        resolver = RegisteredSecretResolver()
        receiver = VerifyingWebhookReceiver()
        app.callback_delivery_hook = callback_module.RegisteredSecretWebhookDispatcher(
            secret_resolver=resolver,
            transport=receiver,
            delivered_at_factory=lambda: (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            hostname_resolver=lambda host, port: ("93.184.216.34",),
        )
        registered = app.handle(
            ServerRequest(
                method="POST",
                path="/callbacks/register",
                headers={"Authorization": "Bearer user-token"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "subscriptionId": "callback-sub-coding-agent-acceptance-1",
                        "scope": callback_registration.get("scope"),
                        "scopeId": run_id,
                        "eventFilter": dict(event_filter),
                        "delivery": dict(delivery_config),
                        "replayFromCursor": accepted_payload.get("initialCursor"),
                        "failurePolicy": "retry_then_dead_letter",
                        "deadLetterPolicy": "webhook-standard",
                    }
                ).encode("utf-8"),
                requested_at=datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            )
        )
        registered_payload = json.loads(registered.body.decode("utf-8"))
        if registered.status_code != 201:
            raise RuntimeError(
                "coding-agent signed webhook callback registration failed"
            )
        registered_deliveries = registered_payload.get("deliveries", [])
        if not isinstance(registered_deliveries, list):
            raise RuntimeError(
                "coding-agent signed webhook delivery evidence must be a list"
            )
        stable_delivery_evidence = [
            {
                key: delivery[key]
                for key in (
                    "deliveryId",
                    "subscriptionId",
                    "eventId",
                    "runId",
                    "sequence",
                    "cursor",
                    "attempt",
                    "idempotencyKey",
                    "status",
                    "statusCode",
                )
                if key in delivery
            }
            for delivery in registered_deliveries
            if isinstance(delivery, Mapping)
        ]
        signed_evidence = {
            "registrationStatus": registered.status_code,
            "selectedEventKinds": [
                event.get("kind") for event in registered_payload.get("events", [])
            ],
            "deliveries": stable_delivery_evidence,
            "secretRefLookups": list(resolver.lookups),
            "receiverRequests": list(receiver.requests),
        }

    return {
        "applicationDigest": canonical_hash(application.application_contract()),
        "scenarioDigest": canonical_hash(documents),
        "scenarioApplicationDigest": canonical_hash(application_document),
        "scenarioGraphDigest": canonical_hash(graph_document),
        "accepted": {
            "statusCode": accepted.status_code,
            "runId": accepted_payload.get("runId"),
            "status": accepted_payload.get("status"),
            "eventStream": accepted_payload.get("eventStream"),
            "websocket": accepted_payload.get("websocket"),
            "cancel": accepted_payload.get("cancel"),
            "initialCursor": accepted_payload.get("initialCursor"),
        },
        "waitingStatus": waiting.get("status"),
        "detach": {
            "statusCode": detached.status_code,
            "lastCursor": detached_payload.get("lastCursor"),
        },
        "callback": {
            "statusCode": callback.status_code,
            "status": callback_payload.get("status"),
            "duplicateStatusCode": duplicate_callback.status_code,
            "duplicateStatus": duplicate_callback_payload.get("status"),
            "receiptCount": len(app.callback_submissions(operation_id)),
        },
        "completionStatus": completion.get("status"),
        "eventKinds": event_kinds,
        "replayEventKinds": replay_event_kinds,
        "replayLastCursor": attached_payload.get("lastCursor"),
        "downstreamExecutions": downstream_executions,
        "signedDelivery": signed_evidence,
    }


def _accepted_invocation_handle_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_coding_agent_background_callback(
        application,
        scenario_path,
        signed_delivery=False,
    )
    accepted = evidence["accepted"]
    if not isinstance(accepted, Mapping) or accepted != {
        "statusCode": 202,
        "runId": "run-coding-agent-acceptance-1",
        "status": "accepted",
        "eventStream": "/runs/run-coding-agent-acceptance-1/events",
        "websocket": "/runs/run-coding-agent-acceptance-1/ws",
        "cancel": "/runs/run-coding-agent-acceptance-1/cancel",
        "initialCursor": "run-coding-agent-acceptance-1:0",
    }:
        raise RuntimeError("accepted invocation handle evidence is incomplete")
    return 0, canonical_dumps(
        {
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "accepted": accepted,
        }
    )


def _cursor_replay_after_detach_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_coding_agent_background_callback(
        application,
        scenario_path,
        signed_delivery=False,
    )
    if evidence["detach"] != {
        "statusCode": 202,
        "lastCursor": "run-coding-agent-acceptance-1:2",
    }:
        raise RuntimeError(
            "cursor replay gate did not detach at the waiting callback cursor"
        )
    if evidence["replayEventKinds"] != [
        "ExternalCallbackReceived",
        "RunResuming",
        "RunFailed",
    ]:
        raise RuntimeError(
            "cursor replay gate did not replay all events emitted after detach"
        )
    if evidence["replayLastCursor"] != "run-coding-agent-acceptance-1:5":
        raise RuntimeError("cursor replay gate did not advance to the terminal cursor")
    return 0, canonical_dumps(
        {
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "detach": evidence["detach"],
            "replayEventKinds": evidence["replayEventKinds"],
            "replayLastCursor": evidence["replayLastCursor"],
        }
    )


def _callback_journal_before_resume_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_coding_agent_background_callback(
        application,
        scenario_path,
        signed_delivery=False,
    )
    if evidence["waitingStatus"] != "waiting_callback":
        raise RuntimeError(
            "callback journal gate did not observe a published callback checkpoint"
        )
    if evidence["eventKinds"] != [
        "RunStarted",
        "AsyncOperationWaitingCallback",
        "ExternalCallbackReceived",
        "RunResuming",
        "RunFailed",
    ]:
        raise RuntimeError(
            "callback journal gate did not preserve journal-before-resume event order"
        )
    if evidence["callback"] != {
        "statusCode": 202,
        "status": "accepted",
        "duplicateStatusCode": 200,
        "duplicateStatus": "duplicate",
        "receiptCount": 1,
    }:
        raise RuntimeError(
            "callback journal gate did not authenticate and deduplicate the callback receipt"
        )
    if (
        evidence["downstreamExecutions"] != 1
        or evidence["completionStatus"] != "failed"
    ):
        raise RuntimeError(
            "callback journal gate did not resume the retained checkpoint exactly once"
        )
    return 0, canonical_dumps(
        {
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "eventKinds": evidence["eventKinds"],
            "callback": evidence["callback"],
            "downstreamExecutions": evidence["downstreamExecutions"],
            "completionStatus": evidence["completionStatus"],
        }
    )


def _signed_webhook_delivery_check(
    application: AcceptanceApplication,
    scenario_path: Path,
) -> tuple[int, str]:
    evidence = _exercise_coding_agent_background_callback(
        application,
        scenario_path,
        signed_delivery=True,
    )
    signed = evidence["signedDelivery"]
    if not isinstance(signed, Mapping):
        raise RuntimeError("signed webhook delivery evidence is missing")
    deliveries = signed.get("deliveries")
    receiver_requests = signed.get("receiverRequests")
    if signed.get("registrationStatus") != 201 or signed.get("selectedEventKinds") != [
        "RunFailed"
    ]:
        raise RuntimeError(
            "signed webhook gate did not select the scenario's retained terminal event"
        )
    if (
        not isinstance(deliveries, list)
        or len(deliveries) != 1
        or deliveries[0].get("status") != "delivered"
    ):
        raise RuntimeError("signed webhook gate did not record a successful delivery")
    if signed.get("secretRefLookups") != ["secret://callbacks/ide-relay"]:
        raise RuntimeError(
            "signed webhook gate did not resolve the registered secret reference"
        )
    if (
        not isinstance(receiver_requests, list)
        or len(receiver_requests) != 1
        or receiver_requests[0].get("verified") is not True
        or receiver_requests[0].get("url")
        != "https://ide-relay.example.com/graphblocks/events"
        or receiver_requests[0].get("resolvedAddresses") != ["93.184.216.34"]
    ):
        raise RuntimeError(
            "signed webhook gate receiver did not verify the canonical HMAC request"
        )
    return 0, canonical_dumps(
        {
            "applicationDigest": evidence["applicationDigest"],
            "scenarioDigest": evidence["scenarioDigest"],
            "scenarioApplicationDigest": evidence["scenarioApplicationDigest"],
            "scenarioGraphDigest": evidence["scenarioGraphDigest"],
            "signedDelivery": signed,
        }
    )


class AcceptanceGateRunner:
    def __init__(
        self,
        *,
        custom_handlers: Mapping[str, AcceptanceGateHandler] | None = None,
    ) -> None:
        self._builtin_semantic_handlers: dict[str, AcceptanceGateHandler] = {
            "bounded task plan check": _bounded_task_plan_check,
            "task budget delegation check": _task_budget_delegation_check,
            "replan patch CAS check": _replan_patch_cas_check,
            "budget lease reservation check": _budget_lease_reservation_check,
            "review invalidation check": _review_invalidation_check,
            "governed trial commit gate": _governed_trial_commit_gate,
            "release bundle verification": _release_bundle_verification,
            "canary quality gate": _canary_quality_gate,
            "rollback and drain gate": _rollback_and_drain_gate,
            "duplex session contract check": _duplex_session_contract_check,
            "interruption authority check": _interruption_authority_check,
            "playback ledger check": _playback_ledger_check,
            "OTel projection check": _otel_projection_check,
            "Langfuse projection check": _langfuse_projection_check,
            "telemetry outage correctness check": _telemetry_outage_correctness_check,
            "source reference check": _source_reference_check,
            "generated artifact check": _generated_artifact_check,
            "parser fallback check": _parser_fallback_check,
            "ACL propagation check": _acl_propagation_check,
            "rag citation validation": _rag_citation_validation,
            "abstention check": _abstention_check,
            "conversation CAS check": _conversation_cas_check,
            "draft retract commit check": _draft_retract_commit_check,
            "accepted invocation handle check": _accepted_invocation_handle_check,
            "cursor replay after detach": _cursor_replay_after_detach_check,
            "callback journal-before-resume check": _callback_journal_before_resume_check,
            "signed webhook delivery check": _signed_webhook_delivery_check,
        }
        self._custom_handlers = dict(custom_handlers or {})

    def run_manifest(
        self,
        manifest: AcceptanceManifest,
        *,
        root: Path,
    ) -> AcceptanceRunReport:
        return AcceptanceRunReport(
            manifest_digest=manifest.content_digest(),
            applications=tuple(
                self.run_application(application, root=root)
                for application in manifest.applications
            ),
        )

    def run_application(
        self,
        application: AcceptanceApplication,
        *,
        root: Path,
    ) -> AcceptanceApplicationReport:
        scenario_path = _acceptance_scenario_path_beneath_root(
            root, application.scenario_path
        )
        try:
            scenario_digest = canonical_hash(load_documents(scenario_path))
        except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
            scenario_digest = canonical_hash(
                {
                    "scenario_path": application.scenario_path,
                    "error": type(error).__name__,
                }
            )
        results: list[AcceptanceGateResult] = []
        for gate_index, gate in enumerate(application.gates):
            command: tuple[str, ...] = ()
            output = ""
            exit_code: int | None = None
            diagnostic: AcceptanceGateDiagnostic | None = None
            if gate == "graphblocks validate":
                command = ("graphblocks", "validate", application.scenario_path)
                arguments = ["validate", str(scenario_path)]
                if application.allow_unknown_blocks:
                    command += ("--allow-unknown-blocks",)
                    arguments.append("--allow-unknown-blocks")
                output_buffer = io.StringIO()
                try:
                    with redirect_stdout(output_buffer):
                        exit_code = graphblocks_cli_main(arguments)
                    output = output_buffer.getvalue()
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    yaml.YAMLError,
                ) as error:
                    output = output_buffer.getvalue()
                    diagnostic = AcceptanceGateDiagnostic(
                        code="AcceptanceGateExecutionFailed",
                        message=(
                            str(error)
                            .replace(str(scenario_path), application.scenario_path)
                            .replace(str(root), ".")
                        ),
                        path=f"$.applications.{application.application_id}.gates[{gate_index}]",
                    )
            elif gate == "graphblocks plan --expand":
                command = (
                    "graphblocks",
                    "plan",
                    application.scenario_path,
                    "--expand",
                )
                arguments = ["plan", str(scenario_path), "--expand"]
                if application.allow_unknown_blocks:
                    command += ("--allow-unknown-blocks",)
                    arguments.append("--allow-unknown-blocks")
                output_buffer = io.StringIO()
                try:
                    with redirect_stdout(output_buffer):
                        exit_code = graphblocks_cli_main(arguments)
                    output = output_buffer.getvalue()
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    yaml.YAMLError,
                ) as error:
                    output = output_buffer.getvalue()
                    diagnostic = AcceptanceGateDiagnostic(
                        code="AcceptanceGateExecutionFailed",
                        message=(
                            str(error)
                            .replace(str(scenario_path), application.scenario_path)
                            .replace(str(root), ".")
                        ),
                        path=f"$.applications.{application.application_id}.gates[{gate_index}]",
                    )
            elif gate in self._builtin_semantic_handlers:
                try:
                    exit_code, output = self._builtin_semantic_handlers[gate](
                        application,
                        scenario_path,
                    )
                    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                        raise TypeError(
                            "acceptance gate handler exit code must be an integer"
                        )
                    if not isinstance(output, str):
                        raise TypeError(
                            "acceptance gate handler output must be a string"
                        )
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    yaml.YAMLError,
                ) as error:
                    diagnostic = AcceptanceGateDiagnostic(
                        code="AcceptanceGateExecutionFailed",
                        message=(
                            str(error)
                            .replace(str(scenario_path), application.scenario_path)
                            .replace(str(root), ".")
                        ),
                        path=f"$.applications.{application.application_id}.gates[{gate_index}]",
                    )
            elif gate in self._custom_handlers:
                try:
                    exit_code, output = self._custom_handlers[gate](
                        application,
                        scenario_path,
                    )
                    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                        raise TypeError(
                            "acceptance gate handler exit code must be an integer"
                        )
                    if not isinstance(output, str):
                        raise TypeError(
                            "acceptance gate handler output must be a string"
                        )
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    yaml.YAMLError,
                ) as error:
                    diagnostic = AcceptanceGateDiagnostic(
                        code="AcceptanceGateExecutionFailed",
                        message=(
                            str(error)
                            .replace(str(scenario_path), application.scenario_path)
                            .replace(str(root), ".")
                        ),
                        path=f"$.applications.{application.application_id}.gates[{gate_index}]",
                    )
            else:
                diagnostic = AcceptanceGateDiagnostic(
                    code="AcceptanceGateHandlerMissing",
                    message="acceptance gate has no registered exact handler",
                    path=f"$.applications.{application.application_id}.gates[{gate_index}]",
                )
            if diagnostic is None and exit_code != 0:
                diagnostic = AcceptanceGateDiagnostic(
                    code="AcceptanceGateFailed",
                    message=f"acceptance gate exited with status {exit_code}",
                    path=f"$.applications.{application.application_id}.gates[{gate_index}]",
                )
            normalized_output = output.replace(
                str(scenario_path), application.scenario_path
            ).replace(str(root), ".")
            results.append(
                AcceptanceGateResult(
                    application_id=application.application_id,
                    gate=gate,
                    status="passed" if diagnostic is None else "failed",
                    command=command,
                    output_digest=canonical_hash(
                        {
                            "exit_code": exit_code,
                            "output": normalized_output,
                        }
                    ),
                    diagnostics=(() if diagnostic is None else (diagnostic,)),
                )
            )
        return AcceptanceApplicationReport(
            application_id=application.application_id,
            scenario_path=application.scenario_path,
            application_digest=canonical_hash(application.application_contract()),
            scenario_digest=scenario_digest,
            results=tuple(results),
        )
