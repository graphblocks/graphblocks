from __future__ import annotations

from graphblocks.outcome import InputDependency, Outcome, PortRef, Readiness, ReadinessTracker, ResolvedInput


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
