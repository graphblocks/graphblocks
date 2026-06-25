from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from .canonical import canonical_hash
from .conversation import ContentPart


OutputDisposition = Literal["allow", "hold", "redact", "replace", "abort_response", "abort_turn", "deny_commit"]
ProviderCancellation = Literal["none", "request", "required_if_supported"]
DraftDisposition = Literal["keep", "mark_incomplete", "retract"]
PendingToolCallsDisposition = Literal["keep", "deny", "cancel_admitted"]
DeliveryMode = Literal["buffer_until_commit", "bounded_holdback", "immediate_draft"]
FlushBoundary = Literal["token", "sentence", "paragraph", "content_part", "tool_call", "response"]
ViolationAction = Literal["abort_response", "abort_turn", "redact", "replace"]
TerminalReason = Literal["policy_denied", "budget_exhausted", "cancelled", "client_disconnected"]
OutputDurableResult = Literal["none", "incomplete", "partial"]

VALID_OUTPUT_DISPOSITIONS = frozenset(
    {"allow", "hold", "redact", "replace", "abort_response", "abort_turn", "deny_commit"}
)
VALID_PROVIDER_CANCELLATIONS = frozenset({"none", "request", "required_if_supported"})
VALID_DRAFT_DISPOSITIONS = frozenset({"keep", "mark_incomplete", "retract"})
VALID_PENDING_TOOL_CALLS_DISPOSITIONS = frozenset({"keep", "deny", "cancel_admitted"})
VALID_DELIVERY_MODES = frozenset({"buffer_until_commit", "bounded_holdback", "immediate_draft"})
VALID_FLUSH_BOUNDARIES = frozenset({"token", "sentence", "paragraph", "content_part", "tool_call", "response"})
VALID_VIOLATION_ACTIONS = frozenset({"abort_response", "abort_turn", "redact", "replace"})
VALID_TERMINAL_REASONS = frozenset({"policy_denied", "budget_exhausted", "cancelled", "client_disconnected"})
VALID_OUTPUT_DURABLE_RESULTS = frozenset({"none", "incomplete", "partial"})


class OutputDeliveryPolicyError(ValueError):
    pass


class OutputGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeclarativeOutputPolicyRule:
    rule_id: str
    literal: str
    disposition: OutputDisposition
    replacement: str | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    policy_refs: tuple[str, ...] = field(default_factory=tuple)
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("output policy rule_id must not be empty")
        if not self.literal:
            raise ValueError("output policy rule literal must not be empty")
        if self.disposition not in VALID_OUTPUT_DISPOSITIONS:
            raise ValueError(f"invalid output disposition {self.disposition}")
        if self.disposition in {"redact", "replace"} and self.replacement is None:
            raise ValueError(f"{self.disposition} output policy rules require a replacement")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "policy_refs", tuple(self.policy_refs))

    def policy_ref_tuple(self) -> tuple[str, ...]:
        return self.policy_refs or (self.rule_id,)

    def rule_contract(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "literal": self.literal,
            "disposition": self.disposition,
            "replacement": self.replacement,
            "reason_codes": list(self.reason_codes),
            "policy_refs": list(self.policy_refs),
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class DeclarativeOutputPolicyEvaluator:
    rules: tuple[DeclarativeOutputPolicyRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules))

    def evaluate_chunk(self, chunk: GenerationChunk, *, evaluated_at: str) -> OutputPolicyDecision:
        if not isinstance(chunk, GenerationChunk):
            raise TypeError("DeclarativeOutputPolicyEvaluator.evaluate_chunk requires a GenerationChunk")

        input_digest = canonical_hash(
            {
                "chunk": {
                    "stream_id": chunk.stream_id,
                    "response_id": chunk.response_id,
                    "sequence": chunk.sequence,
                    "text": chunk.text,
                },
                "rules": [rule.rule_contract() for rule in self.rules],
            }
        )
        for rule in sorted(self.rules, key=lambda item: (-item.priority, item.rule_id)):
            if rule.literal not in chunk.text:
                continue
            return self._decision_for_rule(rule, chunk, input_digest, evaluated_at)

        return OutputPolicyDecision.allow(
            self._decision_id(input_digest, "allow", None),
            accepted_through_sequence=chunk.sequence,
            input_digest=input_digest,
        ).evaluated_at_time(evaluated_at)

    def _decision_for_rule(
        self,
        rule: DeclarativeOutputPolicyRule,
        chunk: GenerationChunk,
        input_digest: str,
        evaluated_at: str,
    ) -> OutputPolicyDecision:
        decision_id = self._decision_id(input_digest, rule.disposition, rule.rule_id)
        if rule.disposition == "allow":
            decision = OutputPolicyDecision.allow(
                decision_id,
                accepted_through_sequence=chunk.sequence,
                input_digest=input_digest,
            )
        elif rule.disposition == "hold":
            decision = OutputPolicyDecision.hold(decision_id, input_digest=input_digest)
        elif rule.disposition == "redact":
            decision = OutputPolicyDecision.redact(
                decision_id,
                accepted_through_sequence=chunk.sequence,
                redactions=tuple(
                    {
                        "path": f"/chunks/{chunk.sequence}/text",
                        "start": start,
                        "end": start + len(rule.literal),
                        "replacement": rule.replacement or "",
                    }
                    for start in self._literal_offsets(chunk.text, rule.literal)
                ),
                input_digest=input_digest,
            )
        elif rule.disposition == "replace":
            decision = OutputPolicyDecision.replace(
                decision_id,
                accepted_through_sequence=chunk.sequence,
                replacement_parts=(ContentPart(kind="text", text=rule.replacement or ""),),
                input_digest=input_digest,
            )
        elif rule.disposition == "abort_response":
            decision = OutputPolicyDecision.abort_response(decision_id, input_digest=input_digest)
        elif rule.disposition == "abort_turn":
            decision = OutputPolicyDecision.abort_turn(decision_id, input_digest=input_digest)
        elif rule.disposition == "deny_commit":
            decision = OutputPolicyDecision.deny_commit(decision_id, input_digest=input_digest)
        else:
            raise OutputGateError(f"unknown output policy disposition {rule.disposition}")

        return (
            decision.with_reason_codes(rule.reason_codes)
            .with_policy_refs(rule.policy_ref_tuple())
            .evaluated_at_time(evaluated_at)
        )

    @staticmethod
    def _literal_offsets(text: str, literal: str) -> tuple[int, ...]:
        offsets: list[int] = []
        start = 0
        while True:
            index = text.find(literal, start)
            if index < 0:
                return tuple(offsets)
            offsets.append(index)
            start = index + len(literal)

    @staticmethod
    def _decision_id(input_digest: str, disposition: str, rule_id: str | None) -> str:
        return "output-decision:" + canonical_hash(
            {
                "input_digest": input_digest,
                "disposition": disposition,
                "rule_id": rule_id,
            }
        )


class GenerationChunk:
    def __init__(self, stream_id: str, response_id: str, sequence: int, text: str) -> None:
        if sequence < 0:
            raise ValueError("generation chunk sequence must be non-negative")
        if not stream_id.strip():
            raise ValueError("generation chunk stream_id must not be empty")
        if not response_id.strip():
            raise ValueError("generation chunk response_id must not be empty")
        self.stream_id = stream_id
        self.response_id = response_id
        self.sequence = sequence
        self.text = text

    @classmethod
    def text(cls, stream_id: str, response_id: str, sequence: int, text: str) -> GenerationChunk:
        return cls(stream_id=stream_id, response_id=response_id, sequence=sequence, text=text)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GenerationChunk):
            return NotImplemented
        return (
            self.stream_id == other.stream_id
            and self.response_id == other.response_id
            and self.sequence == other.sequence
            and self.text == other.text
        )

    def __repr__(self) -> str:
        return (
            "GenerationChunk("
            f"stream_id={self.stream_id!r}, "
            f"response_id={self.response_id!r}, "
            f"sequence={self.sequence!r}, "
            f"text={self.text!r})"
        )


@dataclass(frozen=True, slots=True)
class OutputPolicyDecision:
    decision_id: str
    disposition: OutputDisposition
    accepted_through_sequence: int | None = None
    replacement_parts: tuple[ContentPart, ...] = field(default_factory=tuple)
    redactions: tuple[dict[str, object], ...] = field(default_factory=tuple)
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    policy_refs: tuple[str, ...] = field(default_factory=tuple)
    provider_cancellation: ProviderCancellation = "request"
    draft_disposition: DraftDisposition = "retract"
    pending_tool_calls: PendingToolCallsDisposition = "deny"
    evaluated_at: str | None = None
    input_digest: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("output policy decisions require a decision id")
        if self.disposition not in VALID_OUTPUT_DISPOSITIONS:
            raise ValueError(f"invalid output disposition {self.disposition}")
        if self.provider_cancellation not in VALID_PROVIDER_CANCELLATIONS:
            raise ValueError(f"invalid provider cancellation {self.provider_cancellation}")
        if self.draft_disposition not in VALID_DRAFT_DISPOSITIONS:
            raise ValueError(f"invalid draft disposition {self.draft_disposition}")
        if self.pending_tool_calls not in VALID_PENDING_TOOL_CALLS_DISPOSITIONS:
            raise ValueError(f"invalid pending tool calls disposition {self.pending_tool_calls}")
        if not self.input_digest.strip():
            raise ValueError("output policy decisions require an input digest")
        if self.accepted_through_sequence is not None and self.accepted_through_sequence < 0:
            raise ValueError("accepted_through_sequence must be non-negative")
        redactions: list[MappingProxyType[str, object]] = []
        for redaction in self.redactions:
            redaction_copy = dict(redaction)
            path = redaction_copy.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("redaction path must not be empty")
            start = redaction_copy.get("start")
            end = redaction_copy.get("end")
            if start is not None or end is not None:
                if not isinstance(start, int) or not isinstance(end, int):
                    raise ValueError("redaction range must use integer start and end")
                if start < 0 or end < 0:
                    raise ValueError("redaction range must be non-negative")
                if start > end:
                    raise ValueError("redaction range must not be reversed")
            redactions.append(MappingProxyType(redaction_copy))
        object.__setattr__(self, "replacement_parts", tuple(self.replacement_parts))
        object.__setattr__(self, "redactions", tuple(redactions))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "policy_refs", tuple(self.policy_refs))

    @classmethod
    def allow(
        cls,
        decision_id: str,
        *,
        accepted_through_sequence: int | None,
        input_digest: str,
    ) -> OutputPolicyDecision:
        return cls(
            decision_id=decision_id,
            disposition="allow",
            accepted_through_sequence=accepted_through_sequence,
            provider_cancellation="request",
            draft_disposition="keep",
            pending_tool_calls="keep",
            input_digest=input_digest,
        )

    @classmethod
    def hold(cls, decision_id: str, *, input_digest: str) -> OutputPolicyDecision:
        return cls(
            decision_id=decision_id,
            disposition="hold",
            provider_cancellation="request",
            draft_disposition="keep",
            pending_tool_calls="keep",
            input_digest=input_digest,
        )

    @classmethod
    def redact(
        cls,
        decision_id: str,
        *,
        accepted_through_sequence: int | None,
        replacement_parts: tuple[ContentPart, ...] = (),
        redactions: tuple[dict[str, object], ...] = (),
        input_digest: str,
    ) -> OutputPolicyDecision:
        return cls(
            decision_id=decision_id,
            disposition="redact",
            accepted_through_sequence=accepted_through_sequence,
            replacement_parts=tuple(replacement_parts),
            redactions=tuple(redactions),
            provider_cancellation="request",
            draft_disposition="keep",
            pending_tool_calls="keep",
            input_digest=input_digest,
        )

    @classmethod
    def replace(
        cls,
        decision_id: str,
        *,
        accepted_through_sequence: int | None,
        replacement_parts: tuple[ContentPart, ...] = (),
        input_digest: str,
    ) -> OutputPolicyDecision:
        return cls(
            decision_id=decision_id,
            disposition="replace",
            accepted_through_sequence=accepted_through_sequence,
            replacement_parts=tuple(replacement_parts),
            provider_cancellation="request",
            draft_disposition="keep",
            pending_tool_calls="keep",
            input_digest=input_digest,
        )

    @classmethod
    def abort_response(cls, decision_id: str, *, input_digest: str) -> OutputPolicyDecision:
        return cls(decision_id=decision_id, disposition="abort_response", input_digest=input_digest)

    @classmethod
    def abort_turn(cls, decision_id: str, *, input_digest: str) -> OutputPolicyDecision:
        return cls(decision_id=decision_id, disposition="abort_turn", input_digest=input_digest)

    @classmethod
    def deny_commit(cls, decision_id: str, *, input_digest: str) -> OutputPolicyDecision:
        return cls(decision_id=decision_id, disposition="deny_commit", input_digest=input_digest)

    def with_provider_cancellation(self, provider_cancellation: ProviderCancellation) -> OutputPolicyDecision:
        return replace(self, provider_cancellation=provider_cancellation)

    def with_draft_disposition(self, draft_disposition: DraftDisposition) -> OutputPolicyDecision:
        return replace(self, draft_disposition=draft_disposition)

    def with_pending_tool_calls(self, pending_tool_calls: PendingToolCallsDisposition) -> OutputPolicyDecision:
        return replace(self, pending_tool_calls=pending_tool_calls)

    def with_reason_codes(self, reason_codes: tuple[str, ...]) -> OutputPolicyDecision:
        return replace(self, reason_codes=tuple(reason_codes))

    def with_policy_refs(self, policy_refs: tuple[str, ...]) -> OutputPolicyDecision:
        return replace(self, policy_refs=tuple(policy_refs))

    def with_redactions(self, redactions: tuple[dict[str, object], ...]) -> OutputPolicyDecision:
        return replace(self, redactions=tuple(redactions))

    def evaluated_at_time(self, evaluated_at: str) -> OutputPolicyDecision:
        return replace(self, evaluated_at=evaluated_at)


@dataclass(frozen=True, slots=True)
class OutputDeliveryPolicy:
    mode: DeliveryMode
    holdback_max_tokens: int | None = None
    holdback_max_bytes: int | None = None
    holdback_max_duration_ms: int | None = None
    flush_boundaries: frozenset[FlushBoundary] = field(default_factory=frozenset)
    on_violation: ViolationAction = "abort_response"
    delivered_draft_disposition: DraftDisposition = "retract"

    def __post_init__(self) -> None:
        if self.mode not in VALID_DELIVERY_MODES:
            raise ValueError(f"invalid output delivery mode {self.mode}")
        if self.on_violation not in VALID_VIOLATION_ACTIONS:
            raise ValueError(f"invalid violation action {self.on_violation}")
        if self.delivered_draft_disposition not in VALID_DRAFT_DISPOSITIONS:
            raise ValueError(f"invalid draft disposition {self.delivered_draft_disposition}")
        for boundary in self.flush_boundaries:
            if boundary not in VALID_FLUSH_BOUNDARIES:
                raise ValueError(f"invalid flush boundary {boundary}")
        object.__setattr__(self, "flush_boundaries", frozenset(self.flush_boundaries))

    @classmethod
    def buffer_until_commit(cls, *, on_violation: ViolationAction) -> OutputDeliveryPolicy:
        return cls(mode="buffer_until_commit", on_violation=on_violation, delivered_draft_disposition="retract")

    @classmethod
    def bounded_holdback(
        cls,
        *,
        on_violation: ViolationAction,
        delivered_draft_disposition: DraftDisposition = "retract",
        holdback_max_tokens: int | None = None,
        holdback_max_bytes: int | None = None,
        holdback_max_duration_ms: int | None = None,
        flush_boundaries: frozenset[FlushBoundary] = frozenset(),
    ) -> OutputDeliveryPolicy:
        return cls(
            mode="bounded_holdback",
            holdback_max_tokens=holdback_max_tokens,
            holdback_max_bytes=holdback_max_bytes,
            holdback_max_duration_ms=holdback_max_duration_ms,
            flush_boundaries=flush_boundaries,
            on_violation=on_violation,
            delivered_draft_disposition=delivered_draft_disposition,
        )

    @classmethod
    def immediate_draft(
        cls,
        *,
        on_violation: ViolationAction,
        delivered_draft_disposition: DraftDisposition,
    ) -> OutputDeliveryPolicy:
        return cls(
            mode="immediate_draft",
            on_violation=on_violation,
            delivered_draft_disposition=delivered_draft_disposition,
        )

    def validate(self) -> OutputDeliveryPolicy:
        for name, value in (
            ("holdback_max_tokens", self.holdback_max_tokens),
            ("holdback_max_bytes", self.holdback_max_bytes),
            ("holdback_max_duration_ms", self.holdback_max_duration_ms),
        ):
            if value is not None and value <= 0:
                raise OutputDeliveryPolicyError(f"{name} must be positive")

        if self.mode == "bounded_holdback" and (
            self.holdback_max_tokens is None
            and self.holdback_max_bytes is None
            and self.holdback_max_duration_ms is None
        ):
            raise OutputDeliveryPolicyError(
                "bounded_holdback output delivery requires a token, byte, or duration bound"
            )
        if self.mode == "immediate_draft" and self.delivered_draft_disposition == "keep":
            raise OutputDeliveryPolicyError("immediate_draft requires incomplete or retracted draft semantics")
        return self


@dataclass(frozen=True, slots=True)
class OutputCutoff:
    stream_id: str
    response_id: str
    turn_id: str | None = None
    last_generated_sequence: int = 0
    last_policy_accepted_sequence: int = 0
    last_client_delivered_sequence: int = 0
    terminal_reason: TerminalReason = "policy_denied"
    draft_disposition: DraftDisposition = "retract"
    durable_result: OutputDurableResult = "none"
    policy_decision_id: str | None = None
    occurred_at: str = ""

    def __post_init__(self) -> None:
        if not self.stream_id.strip():
            raise ValueError("output cutoff stream_id must not be empty")
        if not self.response_id.strip():
            raise ValueError("output cutoff response_id must not be empty")
        if self.turn_id is not None and not self.turn_id.strip():
            raise ValueError("output cutoff turn_id must not be empty")
        if self.policy_decision_id is not None and not self.policy_decision_id.strip():
            raise ValueError("output cutoff policy_decision_id must not be empty")
        if self.terminal_reason not in VALID_TERMINAL_REASONS:
            raise ValueError(f"invalid terminal reason {self.terminal_reason}")
        if self.draft_disposition not in VALID_DRAFT_DISPOSITIONS:
            raise ValueError(f"invalid draft disposition {self.draft_disposition}")
        if self.durable_result not in VALID_OUTPUT_DURABLE_RESULTS:
            raise ValueError(f"invalid output durable result {self.durable_result}")
        if self.last_generated_sequence < 0:
            raise ValueError("last_generated_sequence must be non-negative")
        if self.last_policy_accepted_sequence < 0:
            raise ValueError("last_policy_accepted_sequence must be non-negative")
        if self.last_client_delivered_sequence < 0:
            raise ValueError("last_client_delivered_sequence must be non-negative")
        if self.last_policy_accepted_sequence > self.last_generated_sequence:
            raise ValueError("last_policy_accepted_sequence cannot exceed last_generated_sequence")
        if self.last_client_delivered_sequence > self.last_generated_sequence:
            raise ValueError("last_client_delivered_sequence cannot exceed last_generated_sequence")

    def accepts(self, output: GenerationChunk) -> bool:
        if not isinstance(output, GenerationChunk):
            raise TypeError("OutputCutoff.accepts requires a GenerationChunk")
        return (
            output.stream_id == self.stream_id
            and output.response_id == self.response_id
            and self.accepts_sequence(output.sequence)
        )

    def accepts_sequence(self, sequence: int) -> bool:
        return sequence >= 0 and sequence <= self.last_client_delivered_sequence


@dataclass(frozen=True, slots=True)
class OutputGateUpdate:
    deliverable: list[GenerationChunk]
    cutoff: OutputCutoff | None = None
    provider_cancellation: ProviderCancellation | None = None
    pending_tool_calls: PendingToolCallsDisposition | None = None


@dataclass(slots=True)
class OutputDeliveryGate:
    stream_id: str
    response_id: str
    turn_id: str | None = None
    delivery_policy: OutputDeliveryPolicy = field(
        default_factory=lambda: OutputDeliveryPolicy.bounded_holdback(
            on_violation="abort_response",
            holdback_max_tokens=1,
        )
    )
    pending: dict[int, GenerationChunk] = field(default_factory=dict)
    last_generated_sequence: int = 0
    last_policy_accepted_sequence: int = 0
    last_client_delivered_sequence: int = 0
    cutoff: OutputCutoff | None = None

    def __post_init__(self) -> None:
        if not self.stream_id.strip():
            raise ValueError("output gate stream_id must not be empty")
        if not self.response_id.strip():
            raise ValueError("output gate response_id must not be empty")
        if self.turn_id is not None and not self.turn_id.strip():
            raise ValueError("output gate turn_id must not be empty")
        self.delivery_policy.validate()

    def record_chunk(self, chunk: GenerationChunk) -> list[GenerationChunk]:
        if self.cutoff is not None:
            raise OutputGateError("output gate is policy stopped")
        if chunk.stream_id != self.stream_id:
            raise OutputGateError(f"chunk stream_id {chunk.stream_id!r} does not match {self.stream_id!r}")
        if chunk.response_id != self.response_id:
            raise OutputGateError(f"chunk response_id {chunk.response_id!r} does not match {self.response_id!r}")
        if chunk.sequence <= self.last_generated_sequence:
            raise OutputGateError(
                f"chunk sequence {chunk.sequence} must be greater than {self.last_generated_sequence}"
            )
        expected_sequence = self.last_generated_sequence + 1
        if chunk.sequence != expected_sequence:
            raise OutputGateError(
                f"chunk sequence {chunk.sequence} must be next after {self.last_generated_sequence}"
            )
        self.last_generated_sequence = chunk.sequence
        self.pending[chunk.sequence] = chunk
        if self.delivery_policy.mode != "immediate_draft":
            return []

        delivered = self.pending.pop(chunk.sequence)
        self.last_client_delivered_sequence = delivered.sequence
        return [delivered]

    def commit_accepted_output(self) -> list[GenerationChunk]:
        if self.cutoff is not None:
            return []
        deliverable: list[GenerationChunk] = []
        next_sequence = self.last_client_delivered_sequence + 1
        while next_sequence <= self.last_policy_accepted_sequence and next_sequence in self.pending:
            deliverable.append(self.pending[next_sequence])
            next_sequence += 1
        for chunk in deliverable:
            self.pending.pop(chunk.sequence, None)
            self.last_client_delivered_sequence = chunk.sequence
        return deliverable

    def apply_decision(self, decision: OutputPolicyDecision, *, occurred_at: str) -> OutputGateUpdate:
        if self.cutoff is not None:
            raise OutputGateError("output gate is policy stopped")

        if decision.disposition == "allow":
            if decision.accepted_through_sequence is not None:
                if decision.accepted_through_sequence > self.last_generated_sequence:
                    raise OutputGateError(
                        "accepted sequence "
                        f"{decision.accepted_through_sequence} exceeds last generated sequence "
                        f"{self.last_generated_sequence}"
                    )
                self.last_policy_accepted_sequence = max(
                    self.last_policy_accepted_sequence,
                    decision.accepted_through_sequence,
                )
            if self.delivery_policy.mode == "buffer_until_commit":
                return OutputGateUpdate(deliverable=[])
            return OutputGateUpdate(deliverable=self.commit_accepted_output())

        if decision.disposition == "hold":
            return OutputGateUpdate(deliverable=[])

        if decision.disposition in {"redact", "replace"}:
            if decision.disposition == "redact" and not decision.replacement_parts and not decision.redactions:
                return OutputGateUpdate(deliverable=[])
            if decision.accepted_through_sequence is not None:
                if decision.accepted_through_sequence > self.last_generated_sequence:
                    raise OutputGateError(
                        "accepted sequence "
                        f"{decision.accepted_through_sequence} exceeds last generated sequence "
                        f"{self.last_generated_sequence}"
                    )
                self.last_policy_accepted_sequence = max(
                    self.last_policy_accepted_sequence,
                    decision.accepted_through_sequence,
                )
            if decision.disposition == "redact":
                redactions_by_sequence: dict[int, list[dict[str, object]]] = {}
                for redaction in decision.redactions:
                    path = redaction.get("path")
                    if not isinstance(path, str) or not path.startswith("/chunks/") or not path.endswith("/text"):
                        raise OutputGateError(f"invalid redaction path {path!r}")
                    sequence_text = path[len("/chunks/") : -len("/text")]
                    try:
                        sequence = int(sequence_text)
                    except ValueError as error:
                        raise OutputGateError(f"invalid redaction path {path!r}") from error
                    if sequence < 0:
                        raise OutputGateError(f"invalid redaction path {path!r}")
                    redactions_by_sequence.setdefault(sequence, []).append(redaction)

                for sequence, redactions in redactions_by_sequence.items():
                    if sequence <= self.last_client_delivered_sequence or sequence not in self.pending:
                        continue
                    text = self.pending[sequence].text
                    for redaction in sorted(redactions, key=lambda item: int(item.get("start", -1)), reverse=True):
                        start = redaction.get("start")
                        end = redaction.get("end")
                        replacement = redaction.get("replacement")
                        if (
                            not isinstance(start, int)
                            or not isinstance(end, int)
                            or not isinstance(replacement, str)
                            or start < 0
                            or end < start
                            or end > len(text)
                        ):
                            raise OutputGateError(f"invalid redaction range for {redaction.get('path')!r}")
                        text = text[:start] + replacement + text[end:]
                    self.pending[sequence] = GenerationChunk.text(
                        self.stream_id,
                        self.response_id,
                        sequence,
                        text,
                    )
            if decision.disposition == "replace" and decision.accepted_through_sequence is not None:
                for sequence in list(self.pending):
                    if self.last_client_delivered_sequence < sequence <= decision.accepted_through_sequence:
                        self.pending.pop(sequence, None)
            replacement_end_sequence = None
            replacement_base_sequence = (
                decision.accepted_through_sequence
                if decision.accepted_through_sequence is not None
                else self.last_generated_sequence
            )
            for index, part in enumerate(decision.replacement_parts):
                if part.kind != "text" or part.text is None:
                    raise OutputGateError(f"replacement part {index} must be text")
                sequence = replacement_base_sequence + index
                self.pending[sequence] = GenerationChunk.text(
                    self.stream_id,
                    self.response_id,
                    sequence,
                    part.text,
                )
                replacement_end_sequence = sequence
            if replacement_end_sequence is not None:
                self.last_policy_accepted_sequence = max(
                    self.last_policy_accepted_sequence,
                    replacement_end_sequence,
                )
                self.last_generated_sequence = max(
                    self.last_generated_sequence,
                    replacement_end_sequence,
                )
            if self.delivery_policy.mode == "buffer_until_commit":
                return OutputGateUpdate(deliverable=[])
            return OutputGateUpdate(deliverable=self.commit_accepted_output())

        if decision.disposition in {"abort_response", "abort_turn", "deny_commit"}:
            pending_tool_calls = "deny" if decision.pending_tool_calls == "keep" else decision.pending_tool_calls
            cutoff = OutputCutoff(
                stream_id=self.stream_id,
                response_id=self.response_id,
                turn_id=self.turn_id,
                last_generated_sequence=self.last_generated_sequence,
                last_policy_accepted_sequence=self.last_policy_accepted_sequence,
                last_client_delivered_sequence=self.last_client_delivered_sequence,
                terminal_reason="policy_denied",
                draft_disposition=decision.draft_disposition,
                durable_result="none",
                policy_decision_id=decision.decision_id,
                occurred_at=occurred_at,
            )
            self.pending.clear()
            self.cutoff = cutoff
            return OutputGateUpdate(
                deliverable=[],
                cutoff=cutoff,
                provider_cancellation=decision.provider_cancellation,
                pending_tool_calls=pending_tool_calls,
            )

        raise OutputGateError(f"unknown output policy disposition {decision.disposition}")
