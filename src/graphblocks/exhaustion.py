from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from .budget import BudgetPermit, UsageAmount
from .output_policy import OutputCutoff


ContinuationWork = Literal[
    "current_provider_call",
    "already_admitted_child_work",
    "declared_finalization",
    "checkpoint",
    "cleanup",
    "read_only_tool",
]
ForbiddenWork = Literal[
    "new_turn",
    "plan_expansion",
    "optional_task",
    "new_trial",
    "state_changing_effect",
    "unreserved_provider_call",
]
WorkKind = ContinuationWork | ForbiddenWork
ExhaustionPreset = Literal[
    "finish_current_turn",
    "finish_current_call",
    "finish_current_step",
    "checkpoint_and_pause",
    "hard_stop",
    "degrade_then_finalize",
    "request_extension",
]
InFlightPolicy = Literal[
    "finish_current_unit",
    "checkpoint_then_pause",
    "degrade_and_continue",
    "request_topup_or_approval",
    "cancel_immediately",
]
ExhaustionUnit = Literal["provider_call", "node", "agent_step", "turn", "map_item", "task", "trial", "run"]
ClientDelivery = Literal["stop_immediately", "continue_to_boundary", "buffer_until_commit"]
DurableResult = Literal[
    "none",
    "retract",
    "mark_incomplete",
    "commit_partial",
    "commit_with_exhaustion_notice",
]
EffectPolicy = Literal["preserve_atomicity", "cancel_if_safe", "finish_committing_effect", "compensate_if_committed"]
AfterUnitPolicy = Literal["reject", "pause", "fallback", "close"]


class ExhaustionPolicyError(RuntimeError):
    pass


class MissingExhaustionBoundaryError(ExhaustionPolicyError):
    pass


@dataclass(frozen=True, slots=True)
class ContinuationEnvelope:
    allowed_work: set[ContinuationWork] = field(default_factory=set)
    forbidden_work: set[ForbiddenWork] = field(default_factory=set)
    max_additional_usage: list[UsageAmount] = field(default_factory=list)
    max_additional_steps: int | None = None
    deadline: str | None = None

    @property
    def is_bounded(self) -> bool:
        return bool(self.max_additional_usage) or self.max_additional_steps is not None or self.deadline is not None


@dataclass(frozen=True, slots=True)
class PartialOutputPolicy:
    client_delivery: ClientDelivery = "stop_immediately"
    durable_result: DurableResult = "mark_incomplete"


@dataclass(frozen=True, slots=True)
class ExhaustionPolicy:
    preset: ExhaustionPreset | None
    in_flight: InFlightPolicy
    unit: ExhaustionUnit
    deny_new_work: bool = True
    continuation: ContinuationEnvelope | None = None
    max_overdraft: list[UsageAmount] = field(default_factory=list)
    deadline: str | None = None
    output: PartialOutputPolicy = field(default_factory=PartialOutputPolicy)
    effects: EffectPolicy = "preserve_atomicity"
    after_unit: AfterUnitPolicy = "reject"

    @classmethod
    def from_preset(
        cls,
        preset: ExhaustionPreset,
        *,
        unit: ExhaustionUnit,
        continuation: ContinuationEnvelope | None = None,
    ) -> ExhaustionPolicy:
        if preset == "finish_current_turn":
            envelope = _merge_envelope(
                ContinuationEnvelope(
                    allowed_work={"already_admitted_child_work", "declared_finalization", "checkpoint", "cleanup"},
                    forbidden_work={"new_turn", "plan_expansion", "optional_task", "state_changing_effect"},
                ),
                continuation,
            )
            return cls(
                preset=preset,
                in_flight="finish_current_unit",
                unit=unit,
                continuation=envelope,
                output=PartialOutputPolicy(
                    client_delivery="continue_to_boundary",
                    durable_result="commit_with_exhaustion_notice",
                ),
                after_unit="reject",
            )
        if preset == "hard_stop":
            envelope = _merge_envelope(
                ContinuationEnvelope(
                    allowed_work={"cleanup"},
                    forbidden_work={"new_turn", "plan_expansion", "unreserved_provider_call", "state_changing_effect"},
                ),
                continuation,
            )
            return cls(
                preset=preset,
                in_flight="cancel_immediately",
                unit=unit,
                continuation=envelope,
                output=PartialOutputPolicy(client_delivery="stop_immediately", durable_result="mark_incomplete"),
                after_unit="reject",
            )
        if preset == "checkpoint_and_pause":
            envelope = _merge_envelope(
                ContinuationEnvelope(
                    allowed_work={"checkpoint", "cleanup"},
                    forbidden_work={"new_turn", "optional_task", "new_trial"},
                ),
                continuation,
            )
            return cls(
                preset=preset,
                in_flight="checkpoint_then_pause",
                unit=unit,
                continuation=envelope,
                output=PartialOutputPolicy(client_delivery="stop_immediately", durable_result="commit_partial"),
                after_unit="pause",
            )
        if preset == "degrade_then_finalize":
            envelope = _merge_envelope(
                ContinuationEnvelope(
                    allowed_work={"declared_finalization", "cleanup"},
                    forbidden_work={"state_changing_effect", "optional_task"},
                ),
                continuation,
            )
            return cls(
                preset=preset,
                in_flight="degrade_and_continue",
                unit=unit,
                continuation=envelope,
                output=PartialOutputPolicy(
                    client_delivery="continue_to_boundary",
                    durable_result="commit_with_exhaustion_notice",
                ),
                after_unit="fallback",
            )
        if preset == "request_extension":
            envelope = _merge_envelope(
                ContinuationEnvelope(
                    allowed_work={"checkpoint", "cleanup"},
                    forbidden_work={"new_turn", "plan_expansion", "optional_task", "new_trial"},
                ),
                continuation,
            )
            return cls(
                preset=preset,
                in_flight="request_topup_or_approval",
                unit=unit,
                continuation=envelope,
                output=PartialOutputPolicy(client_delivery="stop_immediately", durable_result="commit_partial"),
                after_unit="pause",
            )
        return cls(
            preset=preset,
            in_flight="finish_current_unit",
            unit=unit,
            continuation=continuation or ContinuationEnvelope(allowed_work={"current_provider_call", "cleanup"}),
        )


def _merge_envelope(default: ContinuationEnvelope, override: ContinuationEnvelope | None) -> ContinuationEnvelope:
    if override is None:
        return default
    return ContinuationEnvelope(
        allowed_work=set(default.allowed_work) | set(override.allowed_work),
        forbidden_work=set(default.forbidden_work) | set(override.forbidden_work),
        max_additional_usage=list(override.max_additional_usage or default.max_additional_usage),
        max_additional_steps=override.max_additional_steps
        if override.max_additional_steps is not None
        else default.max_additional_steps,
        deadline=override.deadline or default.deadline,
    )


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    reason: str


@dataclass(slots=True)
class ExhaustionController:
    policy: ExhaustionPolicy
    atomic_unit_id: str
    admission_epoch: int
    continuation_permit: BudgetPermit | None = None
    used_additional_steps: int = 0
    used_additional_usage: list[UsageAmount] = field(default_factory=list)

    def admit(
        self,
        work_kind: WorkKind,
        *,
        work_epoch: int,
        permit: BudgetPermit | None = None,
        requested_usage: list[UsageAmount] | None = None,
    ) -> AdmissionDecision:
        envelope = self.policy.continuation
        requested_usage_list = list(requested_usage or [])
        if envelope is None:
            return AdmissionDecision(False, "missing_continuation")
        if work_kind in envelope.forbidden_work:
            return AdmissionDecision(False, "forbidden_work")
        if self.policy.deny_new_work and work_kind not in envelope.allowed_work:
            return AdmissionDecision(False, "new_work_denied")
        if work_kind == "already_admitted_child_work" and work_epoch <= self.admission_epoch:
            return AdmissionDecision(True, "already_admitted")
        if self.policy.preset == "hard_stop" and work_kind != "cleanup":
            return AdmissionDecision(False, "hard_stop")
        if permit is not None and not self._valid_permit(permit):
            return AdmissionDecision(False, "invalid_permit")
        if work_epoch > self.admission_epoch and work_kind not in {"declared_finalization", "checkpoint", "cleanup"}:
            return AdmissionDecision(False, "new_work_denied")
        if work_epoch > self.admission_epoch:
            effective_permit = permit or self.continuation_permit
            if effective_permit is None:
                return AdmissionDecision(False, "missing_continuation_permit")
            if not self._valid_permit(effective_permit):
                return AdmissionDecision(False, "invalid_permit")
            if requested_usage_list and not effective_permit.allows(requested_usage_list):
                return AdmissionDecision(False, "usage_exceeds_permit")
            if requested_usage_list and envelope.max_additional_usage:
                allowed_usage: dict[tuple[str, str, tuple[tuple[str, str], ...]], Decimal] = {}
                for amount in envelope.max_additional_usage:
                    key = (amount.kind, amount.unit, tuple(sorted(amount.dimensions.items())))
                    allowed_usage[key] = allowed_usage.get(key, Decimal("0")) + amount.amount
                projected_usage: dict[tuple[str, str, tuple[tuple[str, str], ...]], Decimal] = {}
                for amount in [*self.used_additional_usage, *requested_usage_list]:
                    key = (amount.kind, amount.unit, tuple(sorted(amount.dimensions.items())))
                    projected_usage[key] = projected_usage.get(key, Decimal("0")) + amount.amount
                if any(amount > allowed_usage.get(key, Decimal("0")) for key, amount in projected_usage.items()):
                    return AdmissionDecision(False, "max_additional_usage_exceeded")
        if envelope.max_additional_steps is not None and self.used_additional_steps >= envelope.max_additional_steps:
            return AdmissionDecision(False, "max_additional_steps_exceeded")
        if work_epoch > self.admission_epoch:
            self.used_additional_steps += 1
            self.used_additional_usage.extend(requested_usage_list)
        return AdmissionDecision(True, "allowed")

    def _valid_permit(self, permit: BudgetPermit) -> bool:
        return (
            permit.atomic_unit.resource_id == self.atomic_unit_id
            and permit.continuation_profile == self.policy.preset
            and permit.admission_epoch == self.admission_epoch
        )


def validate_exhaustion_policy(policy: ExhaustionPolicy, *, production: bool = False) -> list[str]:
    issues: list[str] = []
    if policy.preset is None:
        issues.append("missing_preset")
    if not policy.unit:
        issues.append("missing_unit")
    if policy.preset == "finish_current_turn" and production:
        if policy.continuation is None or not policy.continuation.is_bounded:
            raise MissingExhaustionBoundaryError(
                "finish_current_turn requires max additional usage, steps, or deadline"
            )
    return issues
