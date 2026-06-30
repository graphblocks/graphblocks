from __future__ import annotations

from graphblocks.output_policy import (
    DeclarativeOutputPolicyEvaluator,
    DeclarativeOutputPolicyRule,
    DeliveryMode,
    DraftDisposition,
    FlushBoundary,
    GenerationChunk,
    OutputCutoff,
    OutputDeliveryGate,
    OutputDeliveryPolicy,
    OutputDeliveryPolicyError,
    OutputDisposition,
    OutputDurableResult,
    OutputGateError,
    OutputGateUpdate,
    OutputPolicyDecision,
    PendingToolCallsDisposition,
    ProviderCancellation,
    TerminalReason,
    ViolationAction,
)
from graphblocks.policy import (
    EnforcementPoint,
    EntitlementSnapshot,
    PolicyBundle,
    PolicyDecision,
    PolicyEnforcementResult,
    PolicyEffect,
    PolicyEnforcementRecord,
    PolicyEnforcementStatus,
    PolicyEnforcer,
    PolicyFailMode,
    PolicyObligation,
    PolicyProfile,
    PolicyRequest,
    PolicyRule,
    PolicySnapshot,
    PolicyTestCase,
    PolicyTestExpectation,
    PolicyTestReport,
    PolicyTestResult,
    PolicyUnavailableError,
    PrincipalRef,
    ResourceRef,
    RuleEffect,
    StaticPolicyEvaluator,
    VALID_ENFORCEMENT_POINTS,
    VALID_ENFORCEMENT_STATUSES,
    VALID_POLICY_EFFECTS,
    VALID_POLICY_FAIL_MODES,
    VALID_RULE_EFFECTS,
    resolve_policy_snapshot,
    run_policy_tests,
    unavailable_policy_decision,
)


def evaluate_native_output_gate(
    gate: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_output_gate

    return evaluate_output_gate(gate, operations)


def evaluate_native_declarative_output_policy(
    rules: object,
    chunk: dict[str, object],
    *,
    evaluated_at_unix_ms: int,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_declarative_output_policy

    return evaluate_declarative_output_policy(
        rules,
        chunk,
        evaluated_at_unix_ms=evaluated_at_unix_ms,
    )


def evaluate_native_retry_policy(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    from graphblocks_runtime import evaluate_retry_policy

    return evaluate_retry_policy(policy, request)


def evaluate_native_provider_limit_policy(
    policy: dict[str, object],
    incident: dict[str, object],
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_provider_limit_policy

    return evaluate_provider_limit_policy(policy, incident)


def evaluate_native_timeout_deadline(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    from graphblocks_runtime import evaluate_timeout_deadline

    return evaluate_timeout_deadline(policy, request)


__all__ = [
    "DeclarativeOutputPolicyEvaluator",
    "DeclarativeOutputPolicyRule",
    "DeliveryMode",
    "DraftDisposition",
    "EnforcementPoint",
    "EntitlementSnapshot",
    "FlushBoundary",
    "GenerationChunk",
    "OutputCutoff",
    "OutputDeliveryGate",
    "OutputDeliveryPolicy",
    "OutputDeliveryPolicyError",
    "OutputDisposition",
    "OutputDurableResult",
    "OutputGateError",
    "OutputGateUpdate",
    "OutputPolicyDecision",
    "PendingToolCallsDisposition",
    "PolicyBundle",
    "PolicyDecision",
    "PolicyEnforcementResult",
    "PolicyEffect",
    "PolicyEnforcementRecord",
    "PolicyEnforcementStatus",
    "PolicyEnforcer",
    "PolicyFailMode",
    "PolicyObligation",
    "PolicyProfile",
    "PolicyRequest",
    "PolicyRule",
    "PolicySnapshot",
    "PolicyTestCase",
    "PolicyTestExpectation",
    "PolicyTestReport",
    "PolicyTestResult",
    "PolicyUnavailableError",
    "PrincipalRef",
    "ProviderCancellation",
    "ResourceRef",
    "RuleEffect",
    "StaticPolicyEvaluator",
    "TerminalReason",
    "VALID_ENFORCEMENT_POINTS",
    "VALID_ENFORCEMENT_STATUSES",
    "VALID_POLICY_EFFECTS",
    "VALID_POLICY_FAIL_MODES",
    "VALID_RULE_EFFECTS",
    "ViolationAction",
    "evaluate_native_declarative_output_policy",
    "evaluate_native_output_gate",
    "evaluate_native_provider_limit_policy",
    "evaluate_native_retry_policy",
    "evaluate_native_timeout_deadline",
    "resolve_policy_snapshot",
    "run_policy_tests",
    "unavailable_policy_decision",
]
