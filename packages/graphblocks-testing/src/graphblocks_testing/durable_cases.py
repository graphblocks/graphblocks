"""Typed durable TCK case decoding and kind-specific handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
from types import MappingProxyType, ModuleType
from typing import ClassVar, Self

from graphblocks.conversation import ContentPart
from graphblocks.tools import ToolResult

from .durable_contracts import DURABLE_CASE_KINDS, DurableCaseKind
from .models import TckCase
from .reports import TckResult


@dataclass(frozen=True, slots=True)
class DurableCaseEnvelope:
    kind: str
    fixture: Mapping[str, object]
    expected: Mapping[str, object]
    expected_diagnostics: tuple[dict[object, object], ...] | None

    @classmethod
    def decode(
        cls,
        case: TckCase,
        diagnostics: list[dict[str, str]],
    ) -> DurableCaseEnvelope:
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
            "expectedDiagnostics",
            fixture.get("expected_diagnostics"),
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
                        "message": (
                            "durable TCK expectedDiagnostics must be a sequence"
                        ),
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
                                "message": (
                                    "durable TCK expected diagnostic must be object"
                                ),
                                "path": (f"$.expectedDiagnostics[{index}]"),
                            }
                        )
                    else:
                        expected_diagnostic_values.append(dict(raw_diagnostic))
                expected_diagnostics = tuple(expected_diagnostic_values)
        return cls(
            kind=kind,
            fixture=fixture,
            expected=expected,
            expected_diagnostics=expected_diagnostics,
        )


@dataclass(frozen=True, slots=True)
class DurableCaseContext:
    kind: ClassVar[DurableCaseKind]

    durable: ModuleType
    expected: Mapping[str, object]
    diagnostics: list[dict[str, str]]
    expected_keys_with_structural_diagnostics: set[str]

    @classmethod
    def decode(
        cls,
        envelope: DurableCaseEnvelope,
        *,
        durable: ModuleType,
        diagnostics: list[dict[str, str]],
        expected_keys_with_structural_diagnostics: set[str],
    ) -> Self:
        if envelope.kind != cls.kind:
            raise ValueError(
                f"durable case decoder for {cls.kind!r} received {envelope.kind!r}"
            )
        return cls(
            durable=durable,
            expected=envelope.expected,
            diagnostics=diagnostics,
            expected_keys_with_structural_diagnostics=(
                expected_keys_with_structural_diagnostics
            ),
            **cls._decode_fixture(envelope.fixture),
        )

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        raise NotImplementedError


def _fixture_value(
    fixture: Mapping[str, object],
    key: str,
    alias: str | None = None,
    default: object = None,
) -> object:
    if key in fixture:
        return fixture[key]
    if alias is not None and alias in fixture:
        return fixture[alias]
    return default


def _require_list(value: object, message: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(message)
    return value


def _require_mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(message)
    return value


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class SourceReplayCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "source_replay"

    _events: object
    guarantee: object
    _first_poll: object
    _commit_cursor: object
    _after_commit_poll: object
    _replay_poll: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_events": _fixture_value(fixture, "events", default=[]),
            "guarantee": _fixture_value(fixture, "guarantee", default=""),
            "_first_poll": _fixture_value(fixture, "firstPoll", "first_poll", {}),
            "_commit_cursor": _fixture_value(
                fixture, "commitCursor", "commit_cursor", {}
            ),
            "_after_commit_poll": _fixture_value(
                fixture, "afterCommitPoll", "after_commit_poll", {}
            ),
            "_replay_poll": _fixture_value(fixture, "replayPoll", "replay_poll", {}),
        }

    @property
    def events(self) -> list[object]:
        return _require_list(
            self._events,
            "durable source_replay case requires events",
        )

    @property
    def first_poll(self) -> Mapping[str, object]:
        return _mapping_or_empty(self._first_poll)

    @property
    def commit_cursor(self) -> Mapping[str, object]:
        return _require_mapping(
            self._commit_cursor,
            "durable source_replay case requires commitCursor",
        )

    @property
    def after_commit_poll(self) -> Mapping[str, object]:
        return _mapping_or_empty(self._after_commit_poll)

    @property
    def replay_poll(self) -> Mapping[str, object]:
        return _require_mapping(
            self._replay_poll,
            "durable source_replay case requires replayPoll",
        )


@dataclass(frozen=True, slots=True)
class SourceErrorsCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "source_errors"

    _events: object
    guarantee: object
    _committed_cursor: object
    _stale_cursor: object
    _unknown_cursor: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_events": _fixture_value(fixture, "events", default=[]),
            "guarantee": _fixture_value(fixture, "guarantee", default=""),
            "_committed_cursor": _fixture_value(
                fixture, "committedCursor", "committed_cursor", {}
            ),
            "_stale_cursor": _fixture_value(fixture, "staleCursor", "stale_cursor", {}),
            "_unknown_cursor": _fixture_value(
                fixture, "unknownCursor", "unknown_cursor", {}
            ),
        }

    @property
    def events(self) -> list[object]:
        return _require_list(
            self._events,
            "durable source_errors case requires events",
        )

    @property
    def committed_cursor(self) -> Mapping[str, object]:
        return _require_mapping(
            self._committed_cursor,
            "durable source_errors case requires committed, stale, and unknown cursors",
        )

    @property
    def stale_cursor(self) -> Mapping[str, object]:
        return _require_mapping(
            self._stale_cursor,
            "durable source_errors case requires committed, stale, and unknown cursors",
        )

    @property
    def unknown_cursor(self) -> Mapping[str, object]:
        return _require_mapping(
            self._unknown_cursor,
            "durable source_errors case requires committed, stale, and unknown cursors",
        )


@dataclass(frozen=True, slots=True)
class SourceOffsetReuseCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "source_offset_reuse"

    _events: object
    guarantee: object
    poll_demand: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_events": _fixture_value(fixture, "events", default=[]),
            "guarantee": _fixture_value(fixture, "guarantee", default=""),
            "poll_demand": _fixture_value(fixture, "pollDemand", "poll_demand", 1),
        }

    @property
    def events(self) -> list[object]:
        return _require_list(
            self._events,
            "durable source_offset_reuse case requires events",
        )


@dataclass(frozen=True, slots=True)
class WindowLatenessCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "window_lateness"

    _policy: object
    _events: object
    _watermarks: object
    _late_events: object
    _late_event: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_policy": _fixture_value(fixture, "policy", default={}),
            "_events": _fixture_value(fixture, "events", default=[]),
            "_watermarks": _fixture_value(fixture, "watermarks", default=[]),
            "_late_events": _fixture_value(fixture, "lateEvents", "late_events", []),
            "_late_event": _fixture_value(fixture, "lateEvent", "late_event", {}),
        }

    @property
    def policy(self) -> Mapping[str, object]:
        return _require_mapping(self._policy, self._error_message)

    @property
    def events(self) -> list[object]:
        return _require_list(self._events, self._error_message)

    @property
    def watermarks(self) -> list[object]:
        return _require_list(self._watermarks, self._error_message)

    @property
    def late_events(self) -> list[object]:
        return _require_list(self._late_events, self._error_message)

    @property
    def late_event(self) -> Mapping[str, object]:
        return _require_mapping(self._late_event, self._error_message)

    @property
    def _error_message(self) -> str:
        return (
            "durable window_lateness case requires policy, events, "
            "watermarks, and lateEvent"
        )


@dataclass(frozen=True, slots=True)
class WindowBoundaryCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "window_boundary"

    _policy: object
    _event: object
    watermark_unix_ms: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_policy": _fixture_value(fixture, "policy", default={}),
            "_event": _fixture_value(fixture, "event", default={}),
            "watermark_unix_ms": _fixture_value(
                fixture, "watermarkUnixMs", "watermark_unix_ms", 0
            ),
        }

    @property
    def policy(self) -> Mapping[str, object]:
        return _require_mapping(
            self._policy,
            "durable window_boundary case requires policy and event",
        )

    @property
    def event(self) -> Mapping[str, object]:
        return _require_mapping(
            self._event,
            "durable window_boundary case requires policy and event",
        )


@dataclass(frozen=True, slots=True)
class SinkIdempotencyCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "sink_idempotency"

    _request: object
    sink_id: object
    conflict_payload: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_request": _fixture_value(fixture, "request", default={}),
            "sink_id": _fixture_value(fixture, "sinkId", "sink_id", ""),
            "conflict_payload": _fixture_value(
                fixture, "conflictPayload", "conflict_payload"
            ),
        }

    @property
    def request(self) -> Mapping[str, object]:
        return _require_mapping(
            self._request,
            "durable sink_idempotency case requires request",
        )


@dataclass(frozen=True, slots=True)
class CheckpointReplayCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "checkpoint_replay"

    _missing_plan_barrier: object
    _barrier: object
    _checkpoints: object
    _lookup: object
    _missing_lookup: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_missing_plan_barrier": _fixture_value(
                fixture,
                "missingPlanBarrier",
                "missing_plan_barrier",
                {},
            ),
            "_barrier": _fixture_value(fixture, "barrier", default={}),
            "_checkpoints": _fixture_value(fixture, "checkpoints", default=[]),
            "_lookup": _fixture_value(fixture, "lookup", default={}),
            "_missing_lookup": _fixture_value(
                fixture, "missingLookup", "missing_lookup", {}
            ),
        }

    @property
    def missing_plan_barrier(self) -> Mapping[str, object]:
        return _require_mapping(
            self._missing_plan_barrier,
            self._barrier_error_message,
        )

    @property
    def barrier(self) -> Mapping[str, object]:
        return _require_mapping(self._barrier, self._barrier_error_message)

    @property
    def checkpoints(self) -> list[object]:
        return _require_list(self._checkpoints, self._barrier_error_message)

    @property
    def lookup(self) -> Mapping[str, object]:
        return _require_mapping(self._lookup, self._lookup_error_message)

    @property
    def missing_lookup(self) -> Mapping[str, object]:
        return _require_mapping(self._missing_lookup, self._lookup_error_message)

    @property
    def _barrier_error_message(self) -> str:
        return (
            "durable checkpoint_replay case requires missingPlanBarrier, "
            "barrier, and checkpoints"
        )

    @property
    def _lookup_error_message(self) -> str:
        return "durable checkpoint_replay case requires lookup and missingLookup"


@dataclass(frozen=True, slots=True)
class ToolTerminalFromToolResultCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "tool_terminal_from_tool_result"

    _tool_result: object
    _record: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_tool_result": _fixture_value(fixture, "toolResult", "tool_result", {}),
            "_record": _fixture_value(fixture, "record", default={}),
        }

    @property
    def tool_result(self) -> Mapping[str, object]:
        return _require_mapping(self._tool_result, self._error_message)

    @property
    def record(self) -> Mapping[str, object]:
        return _require_mapping(self._record, self._error_message)

    @property
    def _error_message(self) -> str:
        return (
            "durable tool_terminal_from_tool_result case requires toolResult and record"
        )


@dataclass(frozen=True, slots=True)
class ToolTerminalPolicyStopCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "tool_terminal_policy_stop"

    _policy_stop: object
    _late_durable_result: object
    _audited_late_effect: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_policy_stop": _fixture_value(fixture, "policyStop", "policy_stop", {}),
            "_late_durable_result": _fixture_value(
                fixture,
                "lateDurableResult",
                "late_durable_result",
                {},
            ),
            "_audited_late_effect": _fixture_value(
                fixture,
                "auditedLateEffect",
                "audited_late_effect",
                {},
            ),
        }

    @property
    def policy_stop(self) -> Mapping[str, object]:
        return _require_mapping(self._policy_stop, self._error_message)

    @property
    def late_durable_result(self) -> Mapping[str, object]:
        return _require_mapping(self._late_durable_result, self._error_message)

    @property
    def audited_late_effect(self) -> Mapping[str, object]:
        return _require_mapping(self._audited_late_effect, self._error_message)

    @property
    def _error_message(self) -> str:
        return (
            "durable tool_terminal_policy_stop case requires "
            "policyStop and terminal records"
        )


@dataclass(frozen=True, slots=True)
class ToolTerminalEffectInvariantCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "tool_terminal_effect_invariant"

    _record: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {"_record": _fixture_value(fixture, "record", default={})}

    @property
    def record(self) -> Mapping[str, object]:
        return _require_mapping(
            self._record,
            "durable tool_terminal_effect_invariant case requires record",
        )


@dataclass(frozen=True, slots=True)
class BackgroundRunEventStreamCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "background_run_event_stream"

    _events: object
    _attach: object
    _detach: object
    _retention: object
    lifetime: object
    response_mode: object
    response_mode_path: str
    initial_response: object
    source_of_truth: object
    source_of_truth_path: str

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_events": _fixture_value(fixture, "events", default=[]),
            "_attach": _fixture_value(fixture, "attach", default={}),
            "_detach": _fixture_value(fixture, "detach", default={}),
            "_retention": _fixture_value(fixture, "retention", default={}),
            "lifetime": _fixture_value(fixture, "lifetime"),
            "response_mode": _fixture_value(fixture, "responseMode", "response_mode"),
            "response_mode_path": (
                "responseMode"
                if "responseMode" in fixture or "response_mode" not in fixture
                else "response_mode"
            ),
            "initial_response": _fixture_value(
                fixture, "initialResponse", "initial_response", {}
            ),
            "source_of_truth": _fixture_value(
                fixture, "sourceOfTruth", "source_of_truth"
            ),
            "source_of_truth_path": (
                "sourceOfTruth"
                if "sourceOfTruth" in fixture or "source_of_truth" not in fixture
                else "source_of_truth"
            ),
        }

    @property
    def events(self) -> list[object]:
        return _require_list(
            self._events,
            "durable background_run_event_stream case requires events and attach",
        )

    @property
    def attach(self) -> Mapping[str, object]:
        return _require_mapping(
            self._attach,
            "durable background_run_event_stream case requires events and attach",
        )

    @property
    def detach(self) -> Mapping[str, object]:
        return _mapping_or_empty(self._detach)

    @property
    def retention(self) -> Mapping[str, object]:
        return _mapping_or_empty(self._retention)


@dataclass(frozen=True, slots=True)
class CallbackDeliveryProjectionCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "callback_delivery_projection"

    _deliveries: object
    redrive: object
    redrive_present: bool
    subscription: object
    subscription_present: bool
    redrive_assertions: object
    redrive_assertions_present: bool
    non_mandatory_outage_blocks_run: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_deliveries": _fixture_value(fixture, "deliveries", default=[]),
            "redrive": _fixture_value(fixture, "redrive", default={}),
            "redrive_present": "redrive" in fixture,
            "subscription": _fixture_value(fixture, "subscription", default={}),
            "subscription_present": "subscription" in fixture,
            "redrive_assertions": _fixture_value(
                fixture,
                "redriveAssertions",
                "redrive_assertions",
                {},
            ),
            "redrive_assertions_present": (
                "redriveAssertions" in fixture or "redrive_assertions" in fixture
            ),
            "non_mandatory_outage_blocks_run": _fixture_value(
                fixture,
                "nonMandatoryOutageBlocksRun",
                "non_mandatory_outage_blocks_run",
            ),
        }

    @property
    def deliveries(self) -> list[object]:
        return _require_list(
            self._deliveries,
            "durable callback_delivery_projection case requires deliveries",
        )


@dataclass(frozen=True, slots=True)
class AsyncCallbackResumeGuardsCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "async_callback_resume_guards"

    _checks: object
    _resume: object
    _callback: object
    operation: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_checks": _fixture_value(fixture, "checks", default={}),
            "_resume": _fixture_value(fixture, "resume", default={}),
            "_callback": _fixture_value(fixture, "callback", default={}),
            "operation": _fixture_value(fixture, "operation"),
        }

    @property
    def checks(self) -> Mapping[str, object]:
        return _require_mapping(self._checks, self._error_message)

    @property
    def resume(self) -> Mapping[str, object]:
        return _require_mapping(self._resume, self._error_message)

    @property
    def callback(self) -> Mapping[str, object]:
        return _require_mapping(self._callback, self._error_message)

    @property
    def _error_message(self) -> str:
        return (
            "durable async_callback_resume_guards case requires "
            "checks, callback, and resume"
        )


@dataclass(frozen=True, slots=True)
class AsyncCallbackCancelRaceCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "async_callback_cancel_race"

    _journal: object
    _race: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_journal": _fixture_value(fixture, "journal", default=()),
            "_race": _fixture_value(fixture, "race", default={}),
        }

    @property
    def journal(self) -> Sequence[object]:
        if not isinstance(self._journal, Sequence) or isinstance(
            self._journal, (str, bytes)
        ):
            raise ValueError("durable async_callback_cancel_race case requires journal")
        return self._journal

    @property
    def race(self) -> Mapping[str, object]:
        return _require_mapping(
            self._race,
            "durable async_callback_cancel_race case requires race",
        )


@dataclass(frozen=True, slots=True)
class ExternalOperationReconciliationCase(DurableCaseContext):
    kind: ClassVar[DurableCaseKind] = "external_operation_reconciliation"

    _operation: object
    _late_callback: object
    _usage: object

    @staticmethod
    def _decode_fixture(fixture: Mapping[str, object]) -> dict[str, object]:
        return {
            "_operation": _fixture_value(fixture, "operation", default={}),
            "_late_callback": _fixture_value(
                fixture, "lateCallback", "late_callback", {}
            ),
            "_usage": _fixture_value(fixture, "usage", default={}),
        }

    @property
    def operation(self) -> Mapping[str, object]:
        return _require_mapping(self._operation, self._error_message)

    @property
    def late_callback(self) -> Mapping[str, object]:
        return _require_mapping(self._late_callback, self._error_message)

    @property
    def usage(self) -> Mapping[str, object]:
        return _require_mapping(self._usage, self._error_message)

    @property
    def _error_message(self) -> str:
        return (
            "durable external_operation_reconciliation case requires "
            "operation, lateCallback, and usage"
        )


def run_source_replay_case(context: SourceReplayCase) -> dict[str, object]:
    durable = context.durable
    raw_events = context.events
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
                event_time_unix_ms=int(event_time) if event_time is not None else None,
            )
        )
    source = durable.InMemoryDurableSource(str(context.guarantee), events)
    raw_first_poll = context.first_poll
    first = source.poll(None, demand=int(raw_first_poll.get("demand", 1)))
    raw_commit = context.commit_cursor
    source.commit(
        durable.SourceCursor(
            str(raw_commit.get("stream", "")),
            int(raw_commit.get("partition", 0)),
            int(raw_commit.get("offset", 0)),
        )
    )
    raw_after_commit = context.after_commit_poll
    after_commit = source.poll(None, demand=int(raw_after_commit.get("demand", 1)))
    raw_replay = context.replay_poll
    raw_replay_cursor = raw_replay.get("cursor", {})
    if not isinstance(raw_replay_cursor, Mapping):
        raise ValueError("durable source_replay case requires replay cursor")
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
        "afterCommitOffsets": [event.cursor.offset for event in after_commit.events],
        "replayOffsets": [event.cursor.offset for event in replay.events],
    }
    return observed


def run_source_errors_case(context: SourceErrorsCase) -> dict[str, object]:
    durable = context.durable
    raw_events = context.events
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
                event_time_unix_ms=int(event_time) if event_time is not None else None,
            )
        )
    source = durable.InMemoryDurableSource(str(context.guarantee), events)
    paused_error = None
    source.pause()
    try:
        source.poll(None, demand=1)
    except durable.SourcePausedError:
        paused_error = "source_paused"
    source.resume()
    raw_committed = context.committed_cursor
    raw_stale = context.stale_cursor
    raw_unknown = context.unknown_cursor
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
    return observed


def run_source_offset_reuse_case(
    context: SourceOffsetReuseCase,
) -> dict[str, object]:
    durable = context.durable
    raw_events = context.events
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
                event_time_unix_ms=int(event_time) if event_time is not None else None,
            )
        )
    source_error = None
    conflict_cursor = None
    source_events = ()
    try:
        source = durable.InMemoryDurableSource(
            str(context.guarantee),
            events,
        )
        batch = source.poll(
            None,
            demand=int(context.poll_demand),
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
    return observed


def run_window_lateness_case(context: WindowLatenessCase) -> dict[str, object]:
    durable = context.durable
    raw_policy = context.policy
    raw_events = context.events
    raw_watermarks = context.watermarks
    raw_late_events = context.late_events
    raw_late_event = context.late_event
    policy = durable.WindowPolicy.tumbling_event_time(
        size_ms=int(raw_policy.get("sizeMs", raw_policy.get("size_ms", 0))),
        allowed_lateness_ms=int(
            raw_policy.get(
                "allowedLatenessMs",
                raw_policy.get("allowed_lateness_ms", 0),
            )
        ),
        accumulation_mode=str(
            raw_policy.get("accumulationMode", raw_policy.get("accumulation_mode", ""))
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
                event_time_unix_ms=int(event_time) if event_time is not None else None,
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
                event_time_unix_ms=int(event_time) if event_time is not None else None,
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
        "beforePaneRevision": before_pane.revision if before_pane is not None else None,
        "beforePaneIsFinal": before_pane.is_final if before_pane is not None else None,
        "beforePaneOffsets": [event.cursor.offset for event in before_pane.events]
        if before_pane is not None
        else [],
        "paneStartUnixMs": first_pane.start_unix_ms if first_pane is not None else None,
        "paneEndUnixMs": first_pane.end_unix_ms if first_pane is not None else None,
        "paneRevision": first_pane.revision if first_pane is not None else None,
        "paneIsFinal": first_pane.is_final if first_pane is not None else None,
        "paneOffsets": [event.cursor.offset for event in first_pane.events]
        if first_pane is not None
        else [],
        "lateError": late_error,
        "lateWatermarkUnixMs": late_watermark_unix_ms,
    }
    return observed


def run_window_boundary_case(context: WindowBoundaryCase) -> dict[str, object]:
    durable = context.durable
    raw_policy = context.policy
    raw_event = context.event
    size_ms = int(raw_policy.get("sizeMs", raw_policy.get("size_ms", 0)))
    allowed_lateness_ms = int(
        raw_policy.get("allowedLatenessMs", raw_policy.get("allowed_lateness_ms", 0))
    )
    event_time = raw_event.get("eventTimeUnixMs", raw_event.get("event_time_unix_ms"))
    if event_time is None:
        raise ValueError("durable window_boundary case requires eventTimeUnixMs")
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
            durable.Watermark.event_time(int(context.watermark_unix_ms))
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
        "paneStartUnixMs": first_pane.start_unix_ms if first_pane is not None else None,
        "paneEndUnixMs": first_pane.end_unix_ms if first_pane is not None else None,
        "paneIsFinal": first_pane.is_final if first_pane is not None else None,
    }
    return observed


def run_sink_idempotency_case(context: SinkIdempotencyCase) -> dict[str, object]:
    durable = context.durable
    raw_request = context.request
    sink = durable.InMemoryDurableSink(str(context.sink_id))
    request = durable.SinkCommitRequest(
        run_id=str(raw_request.get("runId", raw_request.get("run_id", ""))),
        node_id=str(raw_request.get("nodeId", raw_request.get("node_id", ""))),
        node_attempt_id=str(
            raw_request.get("nodeAttemptId", raw_request.get("node_attempt_id", ""))
        ),
        idempotency_key=str(
            raw_request.get("idempotencyKey", raw_request.get("idempotency_key", ""))
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
        payload=deepcopy(context.conflict_payload),
    )
    if request.precondition_digest is not None:
        conflict = conflict.with_precondition_digest(request.precondition_digest)
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
    return observed


def run_checkpoint_replay_case(context: CheckpointReplayCase) -> dict[str, object]:
    durable = context.durable
    raw_missing_plan = context.missing_plan_barrier
    raw_barrier = context.barrier
    raw_checkpoints = context.checkpoints

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
        run_id=str(raw_missing_plan.get("runId", raw_missing_plan.get("run_id", ""))),
        release_id=str(
            raw_missing_plan.get("releaseId", raw_missing_plan.get("release_id", ""))
        ),
        deployment_revision_id=str(
            raw_missing_plan.get(
                "deploymentRevisionId",
                raw_missing_plan.get("deployment_revision_id", ""),
            )
        ),
        plan_hash=str(
            raw_missing_plan.get("planHash", raw_missing_plan.get("plan_hash", ""))
        ),
        checkpoint_schema=durable.SchemaRef(
            str(raw_schema.get("schemaId", raw_schema.get("schema_id", ""))),
            int(raw_schema.get("schemaVersion", raw_schema.get("schema_version", 0))),
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
            raw_barrier.get("checkpointId", raw_barrier.get("checkpoint_id", ""))
        ),
        run_id=str(raw_barrier.get("runId", raw_barrier.get("run_id", ""))),
        release_id=str(raw_barrier.get("releaseId", raw_barrier.get("release_id", ""))),
        deployment_revision_id=str(
            raw_barrier.get(
                "deploymentRevisionId",
                raw_barrier.get("deployment_revision_id", ""),
            )
        ),
        plan_hash=str(raw_barrier.get("planHash", raw_barrier.get("plan_hash", ""))),
        checkpoint_schema=durable.SchemaRef(
            str(raw_schema.get("schemaId", raw_schema.get("schema_id", ""))),
            int(raw_schema.get("schemaVersion", raw_schema.get("schema_version", 0))),
        ),
        state_revision=int(
            raw_barrier.get("stateRevision", raw_barrier.get("state_revision", 0))
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
            raw_barrier.get("operatorState", raw_barrier.get("operator_state", {}))
        )
        if isinstance(
            raw_barrier.get("operatorState", raw_barrier.get("operator_state", {})),
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
            raw_barrier.get("schemaVersions", raw_barrier.get("schema_versions", {}))
        )
        if isinstance(
            raw_barrier.get("schemaVersions", raw_barrier.get("schema_versions", {})),
            Mapping,
        )
        else {},
        created_at_unix_ms=int(
            raw_barrier.get("createdAtUnixMs", raw_barrier.get("created_at_unix_ms", 0))
        ),
    )
    commit_plan = [
        f"{source_id}:{cursor.stream}:{cursor.partition}:{cursor.offset}"
        for source_id, cursor in barrier.validate().source_commit_plan().cursors
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
                    checkpoint_source_cursors[str(source_id)] = durable.SourceCursor(
                        str(raw_cursor.get("stream", "")),
                        int(raw_cursor.get("partition", 0)),
                        int(raw_cursor.get("offset", 0)),
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
                    raw_checkpoint.get("runId", raw_checkpoint.get("run_id", ""))
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
                    raw_checkpoint.get("planHash", raw_checkpoint.get("plan_hash", ""))
                ),
                checkpoint_schema=durable.SchemaRef(
                    str(raw_schema.get("schemaId", raw_schema.get("schema_id", ""))),
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
    raw_lookup = context.lookup
    raw_missing_lookup = context.missing_lookup
    latest = store.latest_compatible(
        run_id=str(raw_lookup.get("runId", raw_lookup.get("run_id", ""))),
        release_id=str(raw_lookup.get("releaseId", raw_lookup.get("release_id", ""))),
        deployment_revision_id=str(
            raw_lookup.get(
                "deploymentRevisionId",
                raw_lookup.get("deployment_revision_id", ""),
            )
        ),
        plan_hash=str(raw_lookup.get("planHash", raw_lookup.get("plan_hash", ""))),
    )
    missing = store.latest_compatible(
        run_id=str(
            raw_missing_lookup.get("runId", raw_missing_lookup.get("run_id", ""))
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
            raw_missing_lookup.get("planHash", raw_missing_lookup.get("plan_hash", ""))
        ),
    )
    observed = {
        "missingPlanError": missing_plan_error,
        "commitPlan": commit_plan,
        "latestCheckpointId": latest.checkpoint_id if latest is not None else None,
        "latestStateRevision": latest.state_revision if latest is not None else None,
        "missingCompatible": missing is None,
    }
    return observed


def run_tool_terminal_from_tool_result_case(
    context: ToolTerminalFromToolResultCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    durable = context.durable
    store = durable.InMemoryDurableToolTerminalStore()
    raw_result = context.tool_result
    raw_record = context.record

    status = str(raw_result.get("status", ""))
    tool_call_id = str(raw_result.get("toolCallId", raw_result.get("tool_call_id", "")))
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
        raise ValueError("durable tool_terminal_from_tool_result output must be a list")
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
                ContentPart(kind="text", text=text, metadata=dict(metadata_value))
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

    effect_outcome = raw_result.get("effectOutcome", raw_result.get("effect_outcome"))
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
            raw_record.get("argumentsDigest", raw_record.get("arguments_digest", ""))
        ),
        completed_at_unix_ms=int(
            raw_record.get(
                "completedAtUnixMs",
                raw_record.get("completed_at_unix_ms", 0),
            )
        ),
        idempotency_key=str(idempotency_key) if idempotency_key is not None else None,
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
    return observed


def run_tool_terminal_policy_stop_case(
    context: ToolTerminalPolicyStopCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    durable = context.durable
    store = durable.InMemoryDurableToolTerminalStore()
    raw_stop = context.policy_stop
    raw_late_result = context.late_durable_result
    raw_audited = context.audited_late_effect
    policy_stop = store.record_response_policy_stopped(
        str(raw_stop.get("responseId", raw_stop.get("response_id", ""))),
        str(raw_stop.get("policyDecisionId", raw_stop.get("policy_decision_id", ""))),
        last_policy_accepted_sequence=int(
            raw_stop.get(
                "lastPolicyAcceptedSequence",
                raw_stop.get("last_policy_accepted_sequence", 0),
            )
        ),
        occurred_at_unix_ms=int(
            raw_stop.get("occurredAtUnixMs", raw_stop.get("occurred_at_unix_ms", 0))
        ),
    )
    replay = store.record_response_policy_stopped(
        str(raw_stop.get("responseId", raw_stop.get("response_id", ""))),
        str(raw_stop.get("policyDecisionId", raw_stop.get("policy_decision_id", ""))),
        last_policy_accepted_sequence=int(
            raw_stop.get(
                "lastPolicyAcceptedSequence",
                raw_stop.get("last_policy_accepted_sequence", 0),
            )
        ),
        occurred_at_unix_ms=int(
            raw_stop.get("occurredAtUnixMs", raw_stop.get("occurred_at_unix_ms", 0))
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
                    raw_late_result.get("runId", raw_late_result.get("run_id", ""))
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
        if "effectCommitted" in raw_audited or "effect_committed" not in raw_audited
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
            run_id=str(raw_audited.get("runId", raw_audited.get("run_id", ""))),
            response_id=str(
                raw_audited.get("responseId", raw_audited.get("response_id", ""))
            ),
            tool_call_id=str(
                raw_audited.get("toolCallId", raw_audited.get("tool_call_id", ""))
            ),
            revision=int(raw_audited.get("revision", 0)),
            terminal_state=str(
                raw_audited.get("terminalState", raw_audited.get("terminal_state", ""))
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
                str(raw_audited.get("outputDigest", raw_audited.get("output_digest")))
                if raw_audited.get("outputDigest", raw_audited.get("output_digest"))
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
    return observed


def run_tool_terminal_effect_invariant_case(
    context: ToolTerminalEffectInvariantCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    durable = context.durable
    store = durable.InMemoryDurableToolTerminalStore()
    raw_record = context.record
    record_error = None
    try:
        record_effect_committed_path = (
            "effectCommitted"
            if "effectCommitted" in raw_record or "effect_committed" not in raw_record
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
            run_id=str(raw_record.get("runId", raw_record.get("run_id", ""))),
            response_id=str(
                raw_record.get("responseId", raw_record.get("response_id", ""))
            ),
            tool_call_id=str(
                raw_record.get("toolCallId", raw_record.get("tool_call_id", ""))
            ),
            revision=int(raw_record.get("revision", 0)),
            terminal_state=str(
                raw_record.get("terminalState", raw_record.get("terminal_state", ""))
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
                str(raw_record.get("outputDigest", raw_record.get("output_digest")))
                if raw_record.get("outputDigest", raw_record.get("output_digest"))
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
        if "denied terminal records cannot have committed effects" in message:
            record_error = "denied_effect_committed"
        elif "expired terminal records cannot have committed effects" in message:
            record_error = "expired_effect_committed"
        else:
            record_error = type(error).__name__
    observed = {
        "recordError": record_error,
        "toolTerminalCount": store.tool_terminal_count(),
    }
    return observed


def run_background_run_event_stream_case(
    context: BackgroundRunEventStreamCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    raw_events = context.events
    raw_attach = context.attach
    raw_detach = context.detach
    raw_retention = context.retention
    raw_lifetime = context.lifetime
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
    response_mode_path = context.response_mode_path
    raw_response_mode = context.response_mode
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
    raw_initial_response = context.initial_response
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
                if isinstance(initial_status, str) and initial_status.strip()
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
                if isinstance(initial_run_id, str) and initial_run_id.strip()
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
                if isinstance(initial_websocket, str) and initial_websocket.strip()
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
                    and f"/runs/{valid_initial_run_id}/" not in valid_initial_websocket
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
                if isinstance(initial_cancel, str) and initial_cancel.strip()
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
                    and f"/runs/{valid_initial_run_id}/" not in valid_initial_cancel
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
        if isinstance(raw_initial_cursor, str) and raw_initial_cursor.strip():
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
            "runId" if "runId" in raw_event or "run_id" not in raw_event else "run_id"
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
        event_release_id = raw_event.get("releaseId", raw_event.get("release_id"))
        event_release_id_path = (
            "releaseId"
            if "releaseId" in raw_event or "release_id" not in raw_event
            else "release_id"
        )
        if not isinstance(event_release_id, str) or not event_release_id.strip():
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
                if metadata_field in raw_event or metadata_snake_field not in raw_event
                else metadata_snake_field
            )
            metadata_value = raw_event.get(
                metadata_field, raw_event.get(metadata_snake_field)
            )
            if metadata_value is not None and (
                not isinstance(metadata_value, str) or not metadata_value.strip()
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
        elif initial_cursor is not None and cursor.strip() == initial_cursor.strip():
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
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
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
        if isinstance(event_cursor, str) and event_cursor not in cursor_positions:
            cursor_positions[event_cursor] = event_index
    has_last_cursor = "lastCursor" in raw_attach or "last_cursor" in raw_attach
    raw_last_cursor = raw_attach.get("lastCursor", raw_attach.get("last_cursor"))
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
        or (last_cursor_index is not None and event_index > last_cursor_index)
    ]
    has_expired_cursor = "expiredCursor" in raw_attach or "expired_cursor" in raw_attach
    raw_expired_cursor = raw_attach.get(
        "expiredCursor", raw_attach.get("expired_cursor", "")
    )
    if has_expired_cursor and (
        not isinstance(raw_expired_cursor, str) or not raw_expired_cursor.strip()
    ):
        expired_cursor_path = (
            "expiredCursor"
            if "expiredCursor" in raw_attach or "expired_cursor" not in raw_attach
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
        "retainedFromCursor" in raw_retention or "retained_from_cursor" in raw_retention
    )
    raw_retained_from = raw_retention.get(
        "retainedFromCursor",
        raw_retention.get("retained_from_cursor", ""),
    )
    if has_retained_from and (
        not isinstance(raw_retained_from, str) or not raw_retained_from.strip()
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
    raw_cancel_run = raw_detach.get("cancelRun", raw_detach.get("cancel_run", False))
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
    source_of_truth_path = context.source_of_truth_path
    source_of_truth = context.source_of_truth
    authoritative_stream = (
        isinstance(source_of_truth, str) and source_of_truth == "ApplicationEventStream"
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
        "runContinuesAfterDetach": lifetime in {"background", "job"} and not cancel_run,
        "acceptedResponseReturnsRunId": accepted_response_has_run_id,
        "replayEventIds": replay_after_cursor,
        "cursorExpired": expired_cursor_index is not None
        and retained_from_index is not None
        and expired_cursor_index < retained_from_index,
        "summaryIncluded": summary_included,
        "authoritativeStream": authoritative_stream,
        "diagnosticCount": len(diagnostics),
    }
    return observed


def run_callback_delivery_projection_case(
    context: CallbackDeliveryProjectionCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    expected = context.expected
    expected_keys_with_structural_diagnostics = (
        context.expected_keys_with_structural_diagnostics
    )
    raw_deliveries = context.deliveries
    raw_redrive = context.redrive
    raw_subscription = context.subscription
    subscription_supplied = context.subscription_present and isinstance(
        raw_subscription, Mapping
    )
    if not raw_deliveries:
        diagnostics.append(
            {
                "code": "DurableCallbackDeliveryInvalid",
                "message": "callback delivery requires at least one delivery",
                "path": "$.deliveries",
            }
        )
    if context.subscription_present and not isinstance(raw_subscription, Mapping):
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
        if not isinstance(subscription_id, str) or not subscription_id.strip():
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
    if context.redrive_present and not isinstance(raw_redrive, Mapping):
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
    raw_redrive_assertions = context.redrive_assertions
    if not isinstance(raw_redrive_assertions, Mapping):
        if context.redrive_assertions_present:
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
            value = raw_redrive_assertions.get(key, raw_redrive_assertions.get(alias))
            path = (
                key
                if key in raw_redrive_assertions or alias not in raw_redrive_assertions
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
    raw_non_mandatory_outage_blocks_run = context.non_mandatory_outage_blocks_run
    if isinstance(raw_non_mandatory_outage_blocks_run, bool):
        non_mandatory_outage_blocks_run = raw_non_mandatory_outage_blocks_run
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
        redrive_event_id = raw_redrive.get("eventId", raw_redrive.get("event_id"))
        original_event_id = raw_redrive.get(
            "originalEventId", raw_redrive.get("original_event_id")
        )
        if (
            isinstance(redrive_event_id, str)
            and redrive_event_id.strip()
            and isinstance(original_event_id, str)
            and original_event_id.strip()
        ):
            redrive_event_id_preserved = redrive_event_id == original_event_id
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
            redrive_creates_application_event = raw_creates_application_event
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
            or raw_redrive_assertions.get("deadLetterPreservesEventId") is True
            or raw_redrive_assertions.get("dead_letter_preserves_event_id") is True
        ):
            diagnostics.append(
                {
                    "code": "DurableCallbackRedriveInvalid",
                    "message": "callback redrive evidence required for deadLetterPreservesEventId",
                    "path": "$.redrive",
                }
            )
            expected_keys_with_structural_diagnostics.add("deadLetterPreservesEventId")
        if (
            expected.get("redriveCreatesApplicationEvent") is True
            or raw_redrive_assertions.get("redriveCreatesApplicationEvent") is True
            or raw_redrive_assertions.get("redrive_creates_application_event") is True
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
        delivery_id = delivery.get("deliveryId", delivery.get("delivery_id"))
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
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
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
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
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
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
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
                subscription_id.strip() if isinstance(subscription_id, str) else "",
                event_id.strip() if isinstance(event_id, str) else "",
            )
            normalized_idempotency_key = idempotency_key.strip()
            previous_delivery = seen_idempotency_keys.get(normalized_idempotency_key)
            if previous_delivery is None:
                seen_idempotency_keys[normalized_idempotency_key] = logical_delivery
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
            isinstance(raw_status, str) and raw_status in valid_delivery_statuses
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
        raw_next_retry_at = delivery.get("nextRetryAt", delivery.get("next_retry_at"))
        next_retry_at = None
        if raw_next_retry_at is not None:
            if not isinstance(raw_next_retry_at, str) or not raw_next_retry_at.strip():
                diagnostics.append(
                    {
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": "callback delivery requires nextRetryAt timestamp",
                        "path": f"$.deliveries[{index}].nextRetryAt",
                    }
                )
            else:
                next_retry_at_text = raw_next_retry_at.strip()
                if len(next_retry_at_text) <= 10 or next_retry_at_text[10] != "T":
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
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "callback delivery requires nextRetryAt timestamp",
                                "path": f"$.deliveries[{index}].nextRetryAt",
                            }
                        )
                    else:
                        if next_retry_at_text.endswith("Z"):
                            next_retry_at_text = f"{next_retry_at_text[:-1]}+00:00"
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
        if receiver_status == 410 and status_is_valid and status != "cancelled":
            diagnostics.append(
                {
                    "code": "DurableCallbackDeliveryInvalid",
                    "message": "410 callback delivery requires cancelled status",
                    "path": f"$.deliveries[{index}].status",
                }
            )
        if receiver_status == 410 and status == "cancelled":
            last_error = delivery.get("lastError", delivery.get("last_error"))
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
            last_error = delivery.get("lastError", delivery.get("last_error"))
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
            raw_delivered_at = delivery.get("deliveredAt", delivery.get("delivered_at"))
            if not isinstance(raw_delivered_at, str) or not raw_delivered_at.strip():
                diagnostics.append(
                    {
                        "code": "DurableCallbackDeliveryInvalid",
                        "message": f"{status} callback delivery requires deliveredAt",
                        "path": f"$.deliveries[{index}].deliveredAt",
                    }
                )
            else:
                delivered_at_text = raw_delivered_at.strip()
                if len(delivered_at_text) <= 10 or delivered_at_text[10] != "T":
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
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": f"{status} callback delivery requires deliveredAt",
                                "path": f"$.deliveries[{index}].deliveredAt",
                            }
                        )
                    else:
                        if delivered_at_text.endswith("Z"):
                            delivered_at_text = f"{delivered_at_text[:-1]}+00:00"
                        try:
                            delivered_at = datetime.fromisoformat(delivered_at_text)
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
                if len(acknowledged_at_text) <= 10 or acknowledged_at_text[10] != "T":
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
                                "code": "DurableCallbackDeliveryInvalid",
                                "message": "acknowledged callback delivery requires acknowledgedAt",
                                "path": f"$.deliveries[{index}].acknowledgedAt",
                            }
                        )
                    else:
                        if acknowledged_at_text.endswith("Z"):
                            acknowledged_at_text = f"{acknowledged_at_text[:-1]}+00:00"
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
            last_error = delivery.get("lastError", delivery.get("last_error"))
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
        delivery_id = str(delivery.get("deliveryId", delivery.get("delivery_id", "")))
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
        if receiver_status == 409 and str(delivery.get("status", "")) == "acknowledged":
            acknowledged_duplicates.append(delivery_id)
        if (
            receiver_status == 410
            and str(delivery.get("status", "")) == "cancelled"
            and str(delivery.get("lastError", delivery.get("last_error", "")))
            == "subscription_gone"
        ):
            subscription_gone_ids.append(delivery_id)
        if (
            receiver_status is not None
            and 400 <= receiver_status <= 499
            and receiver_status not in {409, 410, 429}
            and str(delivery.get("status", "")) == "failed"
            and str(delivery.get("lastError", delivery.get("last_error", "")))
            == "non_retryable"
        ):
            non_retryable_4xx_ids.append(delivery_id)
    observed = {
        "retryScheduledAfter5xx": bool(scheduled_retry_ids),
        "retryScheduledAfterRetryableStatus": bool(scheduled_retryable_status_ids),
        "deliveredAfter2xx": bool(delivered_after_2xx_ids),
        "duplicate409Acknowledged": bool(acknowledged_duplicates),
        "subscriptionGoneAfter410": bool(subscription_gone_ids),
        "nonRetryable4xxTerminal": bool(non_retryable_4xx_ids),
        "idempotencyKeysUniquePerSubscriptionEvent": idempotency_keys_unique_per_subscription_event,
        "deadLetterPreservesEventId": redrive_event_id_preserved,
        "redriveCreatesApplicationEvent": redrive_creates_application_event,
        "nonMandatoryOutageBlocksRun": non_mandatory_outage_blocks_run,
    }
    return observed


def run_async_callback_resume_guards_case(
    context: AsyncCallbackResumeGuardsCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    raw_checks = context.checks
    raw_resume = context.resume
    raw_callback = context.callback
    raw_operation = context.operation
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
                key if key in raw_operation or alias not in raw_operation else alias
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
        if not isinstance(resume_token_hash, str) or not resume_token_hash.strip():
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
        if not isinstance(expected_schema, str) or not expected_schema.strip():
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
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume operation requires ISO deadline",
                            "path": "$.operation.deadline",
                        }
                    )
                else:
                    if deadline_text.endswith("Z"):
                        deadline_text = f"{deadline_text[:-1]}+00:00"
                    try:
                        operation_deadline_at = datetime.fromisoformat(deadline_text)
                        if operation_deadline_at.tzinfo is None:
                            operation_deadline_at = operation_deadline_at.replace(
                                tzinfo=timezone.utc
                            )
                        operation_deadline_at = operation_deadline_at.astimezone(
                            timezone.utc
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
            if "budgetState" in raw_operation or "budget_state" not in raw_operation
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
                if "eventType" in raw_callback or "event_type" not in raw_callback
                else "event_type"
            )
            event_type = raw_callback.get("eventType", raw_callback.get("event_type"))
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
        if "signatureVerified" in raw_callback or "signature_verified" in raw_callback:
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
            if "callbackId" in raw_callback or "callback_id" not in raw_callback
            else "callback_id"
        )
        callback_id = raw_callback.get("callbackId", raw_callback.get("callback_id"))
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
            if "payloadDigest" in raw_callback or "payload_digest" not in raw_callback
            else "payload_digest"
        )
        payload_digest = raw_callback.get(
            "payloadDigest", raw_callback.get("payload_digest")
        )
        if not isinstance(payload_digest, str) or not payload_digest.strip():
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
            if "verifiedBy" in raw_callback or "verified_by" not in raw_callback
            else "verified_by"
        )
        verified_by = raw_callback.get("verifiedBy", raw_callback.get("verified_by"))
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
            if "idempotencyKey" in raw_callback or "idempotency_key" not in raw_callback
            else "idempotency_key"
        )
        idempotency_key = raw_callback.get(
            "idempotencyKey", raw_callback.get("idempotency_key")
        )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
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
            if "receivedAt" in raw_callback or "received_at" not in raw_callback
            else "received_at"
        )
        received_at = raw_callback.get("receivedAt", raw_callback.get("received_at"))
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
                            "code": "DurableAsyncCallbackResumeInvalid",
                            "message": "async callback resume callback requires ISO receivedAt",
                            "path": f"$.callback.{received_at_path}",
                        }
                    )
                else:
                    if received_at_text.endswith("Z"):
                        received_at_text = f"{received_at_text[:-1]}+00:00"
                    try:
                        callback_received_at = datetime.fromisoformat(received_at_text)
                        if callback_received_at.tzinfo is None:
                            callback_received_at = callback_received_at.replace(
                                tzinfo=timezone.utc
                            )
                        callback_received_at = callback_received_at.astimezone(
                            timezone.utc
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
            if "releaseId" in raw_callback or "release_id" not in raw_callback
            else "release_id"
        )
        release_id = raw_callback.get("releaseId", raw_callback.get("release_id"))
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
        tenant_id = raw_callback.get("tenantId", raw_callback.get("tenant_id"))
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
                key if key in raw_callback or alias not in raw_callback else alias
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
                callback_provider_operation_id != callback_provider_operation_id.strip()
            ):
                diagnostics.append(
                    {
                        "code": "DurableAsyncCallbackResumeInvalid",
                        "message": "async callback resume callback providerOperationId must not contain surrounding whitespace",
                        "path": f"$.callback.{provider_operation_id_path}",
                    }
                )
            elif callback_provider_operation_id != operation_provider_operation_id:
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
                callback_value = raw_callback.get(key, raw_callback.get(alias))
                operation_value = raw_operation.get(key, raw_operation.get(alias))
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
        for reevaluate_index, reevaluate in enumerate(raw_resume_reevaluates):
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
        reevaluates_shape_valid = len(resume_reevaluates) == len(raw_resume_reevaluates)
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
        if reevaluates_shape_valid and "idempotency" not in resume_reevaluates:
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
        "schemaFailureResumesRun": async_resume_guard_values["schemaFailureResumesRun"],
        "timeoutCallbackResumesExpiredOperation": async_resume_guard_values[
            "timeoutCallbackResumesExpiredOperation"
        ],
        "cancelledCallbackCommitsResult": async_resume_guard_values[
            "cancelledCallbackCommitsResult"
        ],
        "staleAttemptCanResume": async_resume_guard_values["staleAttemptCanResume"],
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
        "receiptJournaledBeforeResume": callback_journal_sequence < resume_sequence,
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
    return observed


def run_async_callback_cancel_race_case(
    context: AsyncCallbackCancelRaceCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    raw_journal = context.journal
    raw_race = context.race
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
        ownership_fence = entry.get("ownershipFence", entry.get("ownership_fence"))
        if not isinstance(ownership_fence, str) or not ownership_fence.strip():
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
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
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
        and raw_race.get("resumeAttempted", raw_race.get("resume_attempted")) is True
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
        and raw_race.get("resultCommitted", raw_race.get("result_committed")) is True
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
        and raw_race.get("usageReconciled", raw_race.get("usage_reconciled")) is False
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
        "callbackReceiptRecorded": cancel_race_boolean_values["callbackReceiptRecorded"]
        and bool(callback_entries),
        "cancelWinsBlocksResume": (
            str(raw_race.get("winner", "")) == "cancel"
            and not cancel_race_boolean_values["resumeAttempted"]
        ),
        "lateCallbackCommitsResult": cancel_race_boolean_values["resultCommitted"],
        "lateUsageReconciled": cancel_race_boolean_values["usageReconciled"],
        "ownershipFenceStable": len(fences) == 1 and "" not in fences,
    }
    return observed


def run_external_operation_reconciliation_case(
    context: ExternalOperationReconciliationCase,
) -> dict[str, object]:
    diagnostics = context.diagnostics
    raw_operation = context.operation
    raw_late_callback = context.late_callback
    raw_usage = context.usage
    operation_id_path = (
        "operationId"
        if "operationId" in raw_operation or "operation_id" not in raw_operation
        else "operation_id"
    )
    operation_id = raw_operation.get("operationId", raw_operation.get("operation_id"))
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
    if not isinstance(provider_operation_id, str) or not provider_operation_id.strip():
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
        if "idempotencyKey" in raw_operation or "idempotency_key" not in raw_operation
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
    if not isinstance(resume_token_hash, str) or not resume_token_hash.strip():
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
        if "expectedSchema" in raw_operation or "expected_schema" not in raw_operation
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
    created_at = raw_operation.get("createdAt", raw_operation.get("created_at"))
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
        if "submittedAt" in raw_operation or "submitted_at" not in raw_operation
        else "submitted_at"
    )
    submitted_at = raw_operation.get("submittedAt", raw_operation.get("submitted_at"))
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
    expires_at = raw_operation.get("expiresAt", raw_operation.get("expires_at"))
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
    attempt_id = raw_operation.get("attemptId", raw_operation.get("attempt_id"))
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
    release_id = raw_operation.get("releaseId", raw_operation.get("release_id"))
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
    tenant_id = raw_operation.get("tenantId", raw_operation.get("tenant_id"))
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
    elif operation_policy_snapshot_id != operation_policy_snapshot_id.strip():
        diagnostics.append(
            {
                "code": "DurableExternalOperationInvalid",
                "message": "external operation reconciliation operation policySnapshotId must not contain surrounding whitespace",
                "path": f"$.operation.{operation_policy_snapshot_path}",
            }
        )
    callback_id_path = (
        "callbackId"
        if "callbackId" in raw_late_callback or "callback_id" not in raw_late_callback
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
        if "operationId" in raw_late_callback or "operation_id" not in raw_late_callback
        else "operation_id"
    )
    callback_operation_id = raw_late_callback.get(
        "operationId", raw_late_callback.get("operation_id")
    )
    if not isinstance(callback_operation_id, str) or not callback_operation_id.strip():
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
    elif callback_provider_operation_id != callback_provider_operation_id.strip():
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
    callback_run_id = raw_late_callback.get("runId", raw_late_callback.get("run_id"))
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
        if "nodeId" in raw_late_callback or "node_id" not in raw_late_callback
        else "node_id"
    )
    callback_node_id = raw_late_callback.get("nodeId", raw_late_callback.get("node_id"))
    if not isinstance(callback_node_id, str) or not callback_node_id.strip():
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
        if "attemptId" in raw_late_callback or "attempt_id" not in raw_late_callback
        else "attempt_id"
    )
    callback_attempt_id = raw_late_callback.get(
        "attemptId", raw_late_callback.get("attempt_id")
    )
    if not isinstance(callback_attempt_id, str) or not callback_attempt_id.strip():
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
        if "releaseId" in raw_late_callback or "release_id" not in raw_late_callback
        else "release_id"
    )
    callback_release_id = raw_late_callback.get(
        "releaseId", raw_late_callback.get("release_id")
    )
    if not isinstance(callback_release_id, str) or not callback_release_id.strip():
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
        if "tenantId" in raw_late_callback or "tenant_id" not in raw_late_callback
        else "tenant_id"
    )
    callback_tenant_id = raw_late_callback.get(
        "tenantId", raw_late_callback.get("tenant_id")
    )
    if not isinstance(callback_tenant_id, str) or not callback_tenant_id.strip():
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
        if "verifiedBy" in raw_late_callback or "verified_by" not in raw_late_callback
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
    if not isinstance(policy_snapshot_id, str) or not policy_snapshot_id.strip():
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
        and operation_policy_snapshot_id == operation_policy_snapshot_id.strip()
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
        if "receivedAt" in raw_late_callback or "received_at" not in raw_late_callback
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
        if "effectState" in raw_operation or "effect_state" not in raw_operation
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
        if "effectJournaled" in raw_operation or "effect_journaled" not in raw_operation
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
            for usage_index, usage_record in enumerate(raw_provider_usage_records):
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
                    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
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
            raw_operation.get("effectState", raw_operation.get("effect_state", ""))
        )
        == "committed"
        and raw_effect_journaled is True,
        "lateCallbackCommitsResult": external_reconciliation_values[
            ("lateCallback", "commitsResult")
        ],
        "lateCallbackRecordedDiagnostic": external_reconciliation_values[
            ("lateCallback", "diagnosticRecorded")
        ],
        "lateUsageReconciled": external_reconciliation_values[("usage", "reconciled")],
        "largePayloadUsesArtifactRef": external_reconciliation_values[
            ("lateCallback", "payloadConvertedToArtifactRef")
        ],
        "diagnosticCount": len(diagnostics),
    }
    return observed


DurableCaseDecoder = type[DurableCaseContext]
DURABLE_CASE_DECODERS: Mapping[DurableCaseKind, DurableCaseDecoder] = MappingProxyType(
    {
        "source_replay": SourceReplayCase,
        "source_errors": SourceErrorsCase,
        "source_offset_reuse": SourceOffsetReuseCase,
        "window_lateness": WindowLatenessCase,
        "window_boundary": WindowBoundaryCase,
        "sink_idempotency": SinkIdempotencyCase,
        "checkpoint_replay": CheckpointReplayCase,
        "tool_terminal_from_tool_result": ToolTerminalFromToolResultCase,
        "tool_terminal_policy_stop": ToolTerminalPolicyStopCase,
        "tool_terminal_effect_invariant": ToolTerminalEffectInvariantCase,
        "background_run_event_stream": BackgroundRunEventStreamCase,
        "callback_delivery_projection": CallbackDeliveryProjectionCase,
        "async_callback_resume_guards": AsyncCallbackResumeGuardsCase,
        "async_callback_cancel_race": AsyncCallbackCancelRaceCase,
        "external_operation_reconciliation": (ExternalOperationReconciliationCase),
    }
)
DurableCaseHandler = Callable[[DurableCaseContext], dict[str, object]]
DURABLE_CASE_HANDLERS: Mapping[DurableCaseKind, DurableCaseHandler] = MappingProxyType(
    {
        "source_replay": run_source_replay_case,
        "source_errors": run_source_errors_case,
        "source_offset_reuse": run_source_offset_reuse_case,
        "window_lateness": run_window_lateness_case,
        "window_boundary": run_window_boundary_case,
        "sink_idempotency": run_sink_idempotency_case,
        "checkpoint_replay": run_checkpoint_replay_case,
        "tool_terminal_from_tool_result": run_tool_terminal_from_tool_result_case,
        "tool_terminal_policy_stop": run_tool_terminal_policy_stop_case,
        "tool_terminal_effect_invariant": run_tool_terminal_effect_invariant_case,
        "background_run_event_stream": run_background_run_event_stream_case,
        "callback_delivery_projection": run_callback_delivery_projection_case,
        "async_callback_resume_guards": run_async_callback_resume_guards_case,
        "async_callback_cancel_race": run_async_callback_cancel_race_case,
        "external_operation_reconciliation": run_external_operation_reconciliation_case,
    }
)

if not (
    frozenset(DURABLE_CASE_DECODERS)
    == frozenset(DURABLE_CASE_HANDLERS)
    == DURABLE_CASE_KINDS
):
    raise RuntimeError("durable case decoders, handlers, and kind contract drifted")
if any(kind != decoder.kind for kind, decoder in DURABLE_CASE_DECODERS.items()):
    raise RuntimeError("durable case decoder declares the wrong kind")
if len(set(DURABLE_CASE_DECODERS.values())) != len(DURABLE_CASE_DECODERS):
    raise RuntimeError("durable case kinds must have unique decoders")
if len(set(DURABLE_CASE_HANDLERS.values())) != len(DURABLE_CASE_HANDLERS):
    raise RuntimeError("durable case kinds must have unique handlers")


def run_durable_case(case: TckCase) -> TckResult:
    diagnostics: list[dict[str, str]] = []
    envelope = DurableCaseEnvelope.decode(case, diagnostics)
    handler = DURABLE_CASE_HANDLERS.get(envelope.kind)
    decoder = DURABLE_CASE_DECODERS.get(envelope.kind)
    if handler is None or decoder is None:
        diagnostics.append(
            {
                "code": "DurableKindUnknown",
                "message": (f"durable TCK kind {envelope.kind!r} is not supported"),
                "path": "$.kind",
            }
        )
        return TckResult(
            case_id=case.case_id,
            kind=case.kind,
            status="failed",
            diagnostics=tuple(diagnostics),
            observed={},
        )

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

    expected_keys_with_structural_diagnostics: set[str] = set()
    observed: dict[str, object] = {}
    try:
        observed = handler(
            decoder.decode(
                envelope,
                durable=durable,
                diagnostics=diagnostics,
                expected_keys_with_structural_diagnostics=(
                    expected_keys_with_structural_diagnostics
                ),
            )
        )
    except Exception as error:
        diagnostics.append(
            {
                "code": "DurableExecutionError",
                "message": str(error),
                "path": "$",
            }
        )

    if envelope.expected_diagnostics is not None:
        actual_diagnostics = tuple(dict(diagnostic) for diagnostic in diagnostics)
        diagnostics_match = actual_diagnostics == envelope.expected_diagnostics
        observed["expectedDiagnosticsMatched"] = diagnostics_match
        diagnostics = []
        if not diagnostics_match:
            diagnostics.append(
                {
                    "code": "DurableExpectedDiagnosticsMismatch",
                    "message": (
                        "durable diagnostics did not match expected diagnostics"
                    ),
                    "path": "$.expectedDiagnostics",
                }
            )

    for key, expected_value in envelope.expected.items():
        if str(key) in expected_keys_with_structural_diagnostics:
            continue
        if observed.get(str(key)) != expected_value:
            diagnostics.append(
                {
                    "code": "DurableExpectedMismatch",
                    "message": (f"durable observed {key} did not match expected value"),
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


__all__ = [
    "DURABLE_CASE_DECODERS",
    "DURABLE_CASE_HANDLERS",
    "DURABLE_CASE_KINDS",
    "DurableCaseContext",
    "DurableCaseEnvelope",
    "DurableCaseKind",
    "run_durable_case",
]
