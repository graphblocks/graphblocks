from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

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


class OutputDeliveryPolicyError(ValueError):
    pass


class OutputGateError(RuntimeError):
    pass


class GenerationChunk:
    def __init__(self, stream_id: str, response_id: str, sequence: int, text: str) -> None:
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
        object.__setattr__(self, "replacement_parts", tuple(self.replacement_parts))
        object.__setattr__(
            self,
            "redactions",
            tuple(MappingProxyType(dict(redaction)) for redaction in self.redactions),
        )
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

    def accepts(self, output: GenerationChunk) -> bool:
        if not isinstance(output, GenerationChunk):
            raise TypeError("OutputCutoff.accepts requires a GenerationChunk")
        return (
            output.stream_id == self.stream_id
            and output.response_id == self.response_id
            and self.accepts_sequence(output.sequence)
        )

    def accepts_sequence(self, sequence: int) -> bool:
        return sequence <= self.last_client_delivered_sequence


@dataclass(frozen=True, slots=True)
class OutputGateUpdate:
    deliverable: list[GenerationChunk]
    cutoff: OutputCutoff | None = None
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
        self.delivery_policy.validate()

    def record_chunk(self, chunk: GenerationChunk) -> None:
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
        self.last_generated_sequence = chunk.sequence
        self.pending[chunk.sequence] = chunk

    def commit_accepted_output(self) -> list[GenerationChunk]:
        if self.cutoff is not None:
            return []
        deliverable: list[GenerationChunk] = []
        for sequence in sorted(self.pending):
            if sequence <= self.last_client_delivered_sequence:
                continue
            if sequence > self.last_policy_accepted_sequence:
                break
            deliverable.append(self.pending[sequence])
        for chunk in deliverable:
            self.pending.pop(chunk.sequence, None)
            self.last_client_delivered_sequence = chunk.sequence
        return deliverable

    def apply_decision(self, decision: OutputPolicyDecision, *, occurred_at: str) -> OutputGateUpdate:
        if self.cutoff is not None:
            raise OutputGateError("output gate is policy stopped")

        if decision.disposition == "allow":
            if decision.accepted_through_sequence is not None:
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
            for index, part in enumerate(decision.replacement_parts):
                sequence = (decision.accepted_through_sequence or self.last_generated_sequence) + index
                self.pending[sequence] = GenerationChunk.text(
                    self.stream_id,
                    self.response_id,
                    sequence,
                    part.text or "",
                )
            if self.delivery_policy.mode == "buffer_until_commit":
                return OutputGateUpdate(deliverable=[])
            return OutputGateUpdate(deliverable=self.commit_accepted_output())

        if decision.disposition in {"abort_response", "abort_turn", "deny_commit"}:
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
            return OutputGateUpdate(deliverable=[], cutoff=cutoff, pending_tool_calls=decision.pending_tool_calls)

        raise OutputGateError(f"unknown output policy disposition {decision.disposition}")
