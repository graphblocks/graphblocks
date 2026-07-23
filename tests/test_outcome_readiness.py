from __future__ import annotations

import pytest

from graphblocks.outcome import InputDependency, Outcome, PortRef, Readiness, ReadinessTracker, ResolvedInput


def test_readiness_tracker_validates_and_freezes_restored_signals() -> None:
    source = PortRef("source", "value")
    signals = {source: Outcome.value("initial")}
    tracker = ReadinessTracker(signals)
    signals[source] = Outcome.value("mutated")

    assert tracker.signal(source) == Outcome.value("initial")
    with pytest.raises(TypeError):
        tracker.signals[source] = Outcome.value("direct")  # type: ignore[index]
    with pytest.raises(AttributeError, match="cannot be replaced"):
        tracker.signals = {}  # type: ignore[assignment]
    with pytest.raises(ValueError, match="values must be Outcome"):
        ReadinessTracker({source: object()})  # type: ignore[dict-item]


def test_missing_dependency_waits_but_null_value_is_ready() -> None:
    source = PortRef("source", "value")
    dependency = InputDependency.value("message", source)
    tracker = ReadinessTracker()

    assert tracker.readiness([dependency]) == Readiness.waiting([source])

    tracker.publish(source, Outcome.value(None))

    assert tracker.readiness([dependency]) == Readiness.ready({"message": ResolvedInput.value(None)})


def test_absent_dependency_blocks_required_value_input() -> None:
    source = PortRef("branch", "maybe_value")
    dependency = InputDependency.value("value", source)
    tracker = ReadinessTracker()

    tracker.publish(source, Outcome.absent())

    assert tracker.readiness([dependency]) == Readiness.blocked("value", source, Outcome.absent())


def test_failed_and_cancelled_dependencies_remain_distinct_terminal_outcomes() -> None:
    failed_source = PortRef("model", "answer")
    cancelled_source = PortRef("tool", "result")
    failed = Outcome.failed("provider.timeout", message="provider timed out", retryable=True)
    cancelled = Outcome.cancelled("user_cancel")
    tracker = ReadinessTracker()

    tracker.publish(failed_source, failed)
    tracker.publish(cancelled_source, cancelled)

    assert tracker.readiness([InputDependency.value("answer", failed_source)]) == Readiness.blocked(
        "answer", failed_source, failed
    )
    assert tracker.readiness([InputDependency.value("result", cancelled_source)]) == Readiness.blocked(
        "result", cancelled_source, cancelled
    )


def test_outcome_input_explicitly_accepts_terminal_outcome() -> None:
    source = PortRef("optional_branch", "value")
    dependency = InputDependency.outcome("branch_outcome", source)
    skipped = Outcome.skipped("condition_false")
    tracker = ReadinessTracker()

    tracker.publish(source, skipped)

    assert tracker.readiness([dependency]) == Readiness.ready({"branch_outcome": ResolvedInput.outcome(skipped)})


def test_readiness_reports_all_missing_dependencies_in_input_order() -> None:
    first = PortRef("a", "value")
    second = PortRef("b", "value")
    tracker = ReadinessTracker()

    assert tracker.readiness(
        [
            InputDependency.value("first", first),
            InputDependency.value("second", second),
        ]
    ) == Readiness.waiting([first, second])


def test_outcome_records_validate_identity_status_and_metadata() -> None:
    metadata = {"attempt": 1, "scope": {"labels": ["runtime"]}}
    outcome = Outcome(
        "failed",
        code="provider.timeout",
        message="provider timed out",
        retryable=True,
        metadata=metadata,
    )
    metadata["attempt"] = 2
    metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]

    assert outcome.metadata == {"attempt": 1, "scope": {"labels": ("runtime",)}}
    with pytest.raises(TypeError):
        outcome.metadata["attempt"] = 2
    with pytest.raises(TypeError):
        outcome.metadata["scope"]["labels"] = ("mutated",)  # type: ignore[index]
    with pytest.raises(AttributeError):
        outcome.metadata["scope"]["labels"].append("mutated")  # type: ignore[index, union-attr]
    with pytest.raises(ValueError, match="invalid outcome status"):
        Outcome("unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires code"):
        Outcome("failed")
    with pytest.raises(ValueError, match="only failed outcomes may be retryable"):
        Outcome("cancelled", code="user", retryable=True)
    with pytest.raises(ValueError, match="outcome retryable must be a boolean"):
        Outcome("failed", code="provider.timeout", retryable="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="outcome metadata key must not be empty"):
        Outcome("value", metadata={" ": "bad"})
    with pytest.raises(ValueError, match="metadata key must not contain surrounding whitespace"):
        Outcome("value", metadata={" unstable ": "bad"})
    with pytest.raises(ValueError, match="metadata numbers must be finite"):
        Outcome("value", metadata={"score": float("nan")})
    with pytest.raises(ValueError, match="metadata values must be JSON-compatible"):
        Outcome("value", metadata={"unsupported": object()})


def test_outcome_metadata_rejects_cycles_and_excessive_depth() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    with pytest.raises(ValueError, match="outcome metadata must not be recursive"):
        Outcome("value", metadata=recursive)

    overdeep: object = "leaf"
    for _ in range(65):
        overdeep = {"nested": overdeep}
    with pytest.raises(ValueError, match="outcome metadata exceeds maximum depth 64"):
        Outcome("value", metadata=overdeep)  # type: ignore[arg-type]


def test_outcome_metadata_normalizes_unstable_mapping_failures() -> None:
    class BrokenMetadata(dict[str, object]):
        def items(self):
            raise RuntimeError("mapping changed during iteration")

    with pytest.raises(ValueError, match="outcome metadata must be a stable mapping"):
        Outcome("value", metadata=BrokenMetadata(attempt=1))


def test_readiness_records_validate_shapes_and_copy_inputs() -> None:
    source = PortRef(" node ", " output ")
    resolved = ResolvedInput.value("payload")
    inputs = {" value ": resolved}
    ready = Readiness.ready(inputs)
    inputs["extra"] = ResolvedInput.value("mutated")

    assert source == PortRef("node", "output")
    assert ready.inputs == {"value": resolved}
    with pytest.raises(TypeError):
        ready.inputs["other"] = resolved
    with pytest.raises(ValueError, match="port ref node must not be empty"):
        PortRef(" ", "value")
    with pytest.raises(ValueError, match="input dependency source must be PortRef"):
        InputDependency("value", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid input dependency mode"):
        InputDependency("value", source, mode="raw")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="resolved input outcome payload must be Outcome"):
        ResolvedInput("outcome", "not-an-outcome")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="waiting readiness requires missing dependencies"):
        Readiness(kind="waiting")
    with pytest.raises(ValueError, match="blocked readiness requires input, source, and outcome only"):
        Readiness(kind="blocked", input="value", source=source)
    with pytest.raises(ValueError, match="blocked readiness outcome must not be a value outcome"):
        Readiness.blocked("value", source, Outcome.value("payload"))
    with pytest.raises(ValueError, match="ready readiness must not carry missing or blocked fields"):
        Readiness(kind="ready", inputs={"value": resolved}, missing=(source,))
    with pytest.raises(ValueError, match="duplicate normalized keys"):
        Readiness(kind="ready", inputs={"value": resolved, " value ": resolved})


def test_readiness_tracker_rejects_invalid_publish_and_dependency_records() -> None:
    tracker = ReadinessTracker()
    source = PortRef("source", "value")

    with pytest.raises(ValueError, match="readiness signal port must be PortRef"):
        tracker.publish(object(), Outcome.value("ok"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="readiness signal outcome must be Outcome"):
        tracker.publish(source, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="readiness signal port must be PortRef"):
        tracker.signal(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="readiness dependencies must be InputDependency"):
        tracker.readiness([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="readiness dependencies must not contain duplicate inputs"):
        tracker.readiness(
            [
                InputDependency.value("value", source),
                InputDependency.outcome("value", PortRef("other", "value")),
            ]
        )
    with pytest.raises(ValueError, match="readiness dependencies must be a collection"):
        tracker.readiness(None)  # type: ignore[arg-type]
