from __future__ import annotations

from dataclasses import dataclass, field, replace
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

    def accepts(self, output: GenerationChunk | int) -> bool:
        sequence = output if isinstance(output, int) else output.sequence
        if isinstance(output, GenerationChunk):
            if output.stream_id != self.stream_id or output.response_id != self.response_id:
                return False
        return sequence <= self.last_client_delivered_sequence
