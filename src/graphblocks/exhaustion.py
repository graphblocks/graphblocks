from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from .budget import BudgetPermit, UsageAmount
from .output_policy import OutputCutoff as OutputCutoff


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
VALID_CONTINUATION_WORK = frozenset(
    {
        "current_provider_call",
        "already_admitted_child_work",
        "declared_finalization",
        "checkpoint",
        "cleanup",
        "read_only_tool",
    }
)
VALID_FORBIDDEN_WORK = frozenset(
    {
        "new_turn",
        "plan_expansion",
        "optional_task",
        "new_trial",
        "state_changing_effect",
        "unreserved_provider_call",
    }
)
VALID_WORK_KINDS = VALID_CONTINUATION_WORK | VALID_FORBIDDEN_WORK
VALID_EXHAUSTION_PRESETS = frozenset(
    {
        "finish_current_turn",
        "finish_current_call",
        "finish_current_step",
        "checkpoint_and_pause",
        "hard_stop",
        "degrade_then_finalize",
        "request_extension",
    }
)
VALID_IN_FLIGHT_POLICIES = frozenset(
    {
        "finish_current_unit",
        "checkpoint_then_pause",
        "degrade_and_continue",
        "request_topup_or_approval",
        "cancel_immediately",
    }
)
VALID_EXHAUSTION_UNITS = frozenset(
    {"provider_call", "node", "agent_step", "turn", "map_item", "task", "trial", "run"}
)
VALID_CLIENT_DELIVERY = frozenset(
    {"stop_immediately", "continue_to_boundary", "buffer_until_commit"}
)
VALID_DURABLE_RESULTS = frozenset(
    {
        "none",
        "retract",
        "mark_incomplete",
        "commit_partial",
        "commit_with_exhaustion_notice",
    }
)
VALID_EFFECT_POLICIES = frozenset(
    {
        "preserve_atomicity",
        "cancel_if_safe",
        "finish_committing_effect",
        "compensate_if_committed",
    }
)
VALID_AFTER_UNIT_POLICIES = frozenset({"reject", "pause", "fallback", "close"})
_MAX_U64 = (1 << 64) - 1


class ExhaustionPolicyError(RuntimeError):
    pass


class MissingExhaustionBoundaryError(ExhaustionPolicyError):
    pass


def _validate_non_negative_integer(owner: str, field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{owner} {field_name} must be non-negative")
    if value > _MAX_U64:
        raise ValueError(
            f"{owner} {field_name} exceeds the supported integer range"
        )
    return value


def _validate_exact_non_empty_string(
    owner: str,
    field_name: str,
    value: object,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        ) from error
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return value


def _parse_iso_datetime(owner: str, field_name: str, value: object) -> datetime:
    value = _validate_exact_non_empty_string(owner, field_name, value)
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(
            f"{owner} {field_name} must be an ISO datetime"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"{owner} {field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ContinuationEnvelope:
    allowed_work: set[ContinuationWork] = field(default_factory=set)
    forbidden_work: set[ForbiddenWork] = field(default_factory=set)
    max_additional_usage: list[UsageAmount] = field(default_factory=list)
    max_additional_steps: int | None = None
    deadline: str | None = None

    def __post_init__(self) -> None:
        for field_name, values, valid_values in (
            ("allowed_work", self.allowed_work, VALID_CONTINUATION_WORK),
            ("forbidden_work", self.forbidden_work, VALID_FORBIDDEN_WORK),
        ):
            if isinstance(values, (str, bytes, bytearray)):
                raise ValueError(
                    f"continuation envelope {field_name} must be a collection"
                )
            try:
                normalized = frozenset(values)
            except Exception as error:
                raise ValueError(
                    f"continuation envelope {field_name} must be a collection"
                ) from error
            invalid = sorted(
                repr(value) for value in normalized if value not in valid_values
            )
            if invalid:
                raise ValueError(
                    f"continuation envelope {field_name} contains invalid work "
                    f"{invalid[0]}"
                )
            object.__setattr__(self, field_name, normalized)
        if isinstance(self.max_additional_usage, (str, bytes, bytearray)):
            raise ValueError(
                "continuation envelope max_additional_usage must contain UsageAmount values"
            )
        try:
            max_additional_usage = tuple(self.max_additional_usage)
        except Exception as error:
            raise ValueError(
                "continuation envelope max_additional_usage must contain UsageAmount values"
            ) from error
        if any(
            not isinstance(amount, UsageAmount)
            for amount in max_additional_usage
        ):
            raise ValueError(
                "continuation envelope max_additional_usage must contain UsageAmount values"
            )
        if self.max_additional_steps is not None:
            _validate_non_negative_integer(
                "continuation envelope",
                "max_additional_steps",
                self.max_additional_steps,
            )
        if self.deadline is not None:
            _parse_iso_datetime(
                "continuation envelope",
                "deadline",
                self.deadline,
            )
        object.__setattr__(
            self,
            "max_additional_usage",
            max_additional_usage,
        )

    @property
    def is_bounded(self) -> bool:
        return bool(self.max_additional_usage) or self.max_additional_steps is not None or self.deadline is not None


@dataclass(frozen=True, slots=True)
class PartialOutputPolicy:
    client_delivery: ClientDelivery = "stop_immediately"
    durable_result: DurableResult = "mark_incomplete"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.client_delivery, str)
            or self.client_delivery not in VALID_CLIENT_DELIVERY
        ):
            raise ValueError(
                f"invalid exhaustion client delivery {self.client_delivery!r}"
            )
        if (
            not isinstance(self.durable_result, str)
            or self.durable_result not in VALID_DURABLE_RESULTS
        ):
            raise ValueError(
                f"invalid exhaustion durable result {self.durable_result!r}"
            )


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

    def __post_init__(self) -> None:
        if self.preset is not None and (
            not isinstance(self.preset, str)
            or self.preset not in VALID_EXHAUSTION_PRESETS
        ):
            raise ValueError(f"invalid exhaustion preset {self.preset!r}")
        if (
            not isinstance(self.in_flight, str)
            or self.in_flight not in VALID_IN_FLIGHT_POLICIES
        ):
            raise ValueError(f"invalid in-flight policy {self.in_flight!r}")
        if (
            not isinstance(self.unit, str)
            or self.unit not in VALID_EXHAUSTION_UNITS
        ):
            raise ValueError(f"invalid exhaustion unit {self.unit!r}")
        if not isinstance(self.deny_new_work, bool):
            raise ValueError("exhaustion deny_new_work must be a boolean")
        if self.continuation is not None and not isinstance(
            self.continuation,
            ContinuationEnvelope,
        ):
            raise ValueError(
                "exhaustion continuation must be a ContinuationEnvelope"
            )
        if isinstance(self.max_overdraft, (str, bytes, bytearray)):
            raise ValueError(
                "exhaustion max_overdraft must contain UsageAmount values"
            )
        try:
            max_overdraft = tuple(self.max_overdraft)
        except Exception as error:
            raise ValueError(
                "exhaustion max_overdraft must contain UsageAmount values"
            ) from error
        if any(not isinstance(amount, UsageAmount) for amount in max_overdraft):
            raise ValueError(
                "exhaustion max_overdraft must contain UsageAmount values"
            )
        if self.deadline is not None:
            _parse_iso_datetime(
                "exhaustion",
                "deadline",
                self.deadline,
            )
        if not isinstance(self.output, PartialOutputPolicy):
            raise ValueError(
                "exhaustion output must be a PartialOutputPolicy"
            )
        if (
            not isinstance(self.effects, str)
            or self.effects not in VALID_EFFECT_POLICIES
        ):
            raise ValueError(f"invalid exhaustion effect policy {self.effects!r}")
        if (
            not isinstance(self.after_unit, str)
            or self.after_unit not in VALID_AFTER_UNIT_POLICIES
        ):
            raise ValueError(
                f"invalid exhaustion after-unit policy {self.after_unit!r}"
            )
        object.__setattr__(self, "max_overdraft", max_overdraft)

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

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("admission decision allowed must be a boolean")
        _validate_exact_non_empty_string(
            "admission decision",
            "reason",
            self.reason,
        )


@dataclass(frozen=True, slots=True)
class ExhaustionController:
    policy: ExhaustionPolicy
    atomic_unit_id: str
    admission_epoch: int
    continuation_permit: BudgetPermit | None = None
    validation_time: str | None = None
    used_additional_steps: int = 0
    used_additional_usage: tuple[UsageAmount, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ExhaustionPolicy):
            raise ValueError("exhaustion policy must be an ExhaustionPolicy")
        _validate_exact_non_empty_string(
            "exhaustion",
            "atomic_unit_id",
            self.atomic_unit_id,
        )
        _validate_non_negative_integer("exhaustion", "admission_epoch", self.admission_epoch)
        _validate_non_negative_integer(
            "exhaustion",
            "used_additional_steps",
            self.used_additional_steps,
        )
        if self.continuation_permit is not None and not isinstance(
            self.continuation_permit,
            BudgetPermit,
        ):
            raise ValueError(
                "exhaustion continuation_permit must be a BudgetPermit"
            )
        if self.validation_time is not None:
            _parse_iso_datetime(
                "exhaustion",
                "validation_time",
                self.validation_time,
            )
        deadlines = (
            self.policy.deadline,
            self.policy.continuation.deadline
            if self.policy.continuation is not None
            else None,
        )
        if any(deadline is not None for deadline in deadlines):
            if self.validation_time is None:
                raise ValueError(
                    "exhaustion deadline enforcement requires validation_time"
                )
        if isinstance(self.used_additional_usage, (str, bytes, bytearray)):
            raise ValueError(
                "exhaustion used_additional_usage must contain UsageAmount values"
            )
        try:
            used_additional_usage = tuple(self.used_additional_usage)
        except Exception as error:
            raise ValueError(
                "exhaustion used_additional_usage must contain UsageAmount values"
            ) from error
        if any(
            not isinstance(amount, UsageAmount)
            for amount in used_additional_usage
        ):
            raise ValueError(
                "exhaustion used_additional_usage must contain UsageAmount values"
            )
        object.__setattr__(
            self,
            "used_additional_usage",
            used_additional_usage,
        )

    def admit(
        self,
        work_kind: WorkKind,
        *,
        work_epoch: int,
        permit: BudgetPermit | None = None,
        requested_usage: list[UsageAmount] | None = None,
    ) -> AdmissionDecision:
        work_epoch = _validate_non_negative_integer("exhaustion", "work_epoch", work_epoch)
        if (
            not isinstance(work_kind, str)
            or work_kind not in VALID_WORK_KINDS
        ):
            raise ValueError(f"invalid exhaustion work kind {work_kind!r}")
        if permit is not None and not isinstance(permit, BudgetPermit):
            raise ValueError("exhaustion permit must be a BudgetPermit")
        envelope = self.policy.continuation
        try:
            requested_usage_list = (
                [] if requested_usage is None else list(requested_usage)
            )
        except Exception as error:
            raise ValueError(
                "exhaustion requested_usage must contain UsageAmount values"
            ) from error
        if any(
            not isinstance(amount, UsageAmount)
            for amount in requested_usage_list
        ):
            raise ValueError(
                "exhaustion requested_usage must contain UsageAmount values"
            )
        if envelope is None:
            return AdmissionDecision(False, "missing_continuation")
        if self.validation_time is not None:
            validation_time = _parse_iso_datetime(
                "exhaustion",
                "validation_time",
                self.validation_time,
            )
            deadlines = tuple(
                _parse_iso_datetime("exhaustion", "deadline", deadline)
                for deadline in (self.policy.deadline, envelope.deadline)
                if deadline is not None
            )
            if any(validation_time >= deadline for deadline in deadlines):
                return AdmissionDecision(False, "continuation_deadline_exceeded")
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
            permits_checkpoint_pause_safety_work = self.policy.preset == "checkpoint_and_pause" and work_kind in {
                "checkpoint",
                "cleanup",
            }
            permits_degraded_finalization = self.policy.preset == "degrade_then_finalize" and work_kind in {
                "declared_finalization",
                "cleanup",
            }
            effective_permit = permit or self.continuation_permit
            if effective_permit is None and not permits_checkpoint_pause_safety_work and not permits_degraded_finalization:
                return AdmissionDecision(False, "missing_continuation_permit")
            if effective_permit is not None and not self._valid_permit(effective_permit):
                return AdmissionDecision(False, "invalid_permit")
            if requested_usage_list:
                if effective_permit is None:
                    return AdmissionDecision(False, "missing_continuation_permit")
                if not effective_permit.allows(requested_usage_list):
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
        if (
            work_epoch > self.admission_epoch
            and envelope.max_additional_steps is not None
            and self.used_additional_steps >= envelope.max_additional_steps
        ):
            return AdmissionDecision(False, "max_additional_steps_exceeded")
        if work_epoch > self.admission_epoch:
            object.__setattr__(
                self,
                "used_additional_steps",
                self.used_additional_steps + 1,
            )
            object.__setattr__(
                self,
                "used_additional_usage",
                (*self.used_additional_usage, *requested_usage_list),
            )
        return AdmissionDecision(True, "allowed")

    def _valid_permit(self, permit: BudgetPermit) -> bool:
        return (
            permit.atomic_unit.resource_id == self.atomic_unit_id
            and permit.continuation_profile == self.policy.preset
            and permit.admission_epoch == self.admission_epoch
            and (self.validation_time is None or permit.is_active_at(self.validation_time))
        )


def validate_exhaustion_policy(policy: ExhaustionPolicy, *, production: bool = False) -> list[str]:
    if not isinstance(policy, ExhaustionPolicy):
        raise ValueError("policy must be an ExhaustionPolicy")
    if not isinstance(production, bool):
        raise ValueError("production must be a boolean")
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
