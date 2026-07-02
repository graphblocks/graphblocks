from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import contextmanager

import graphblocks


@contextmanager
def raises_value_error(pattern: str):
    try:
        yield
    except ValueError as error:
        assert re.search(pattern, str(error)), str(error)
    else:
        raise AssertionError("expected ValueError")


def test_async_operation_result_preserves_committed_effect_after_cancel() -> None:
    result = graphblocks.AsyncOperationResult.cancelled("op-1").with_external_effects(
        [
            graphblocks.ExternalEffectRecord(
                effect_id="effect-ticket-1",
                target="ticket-system",
                operation="ticket.create",
                outcome="committed",
                idempotency_key="idem-ticket-1",
                provider_effect_id="ticket-123",
            )
        ]
    )

    assert result.status == "cancelled"
    assert result.external_effect_was_committed() is True
    assert result.to_json()["external_effects"] == [
        {
            "effect_id": "effect-ticket-1",
            "target": "ticket-system",
            "operation": "ticket.create",
            "outcome": "committed",
            "idempotency_key": "idem-ticket-1",
            "provider_effect_id": "ticket-123",
        }
    ]


def test_async_operation_result_preserves_committed_effect_after_incomplete_late_callback() -> None:
    result = graphblocks.AsyncOperationResult.incomplete("op-1").with_external_effects(
        [
            graphblocks.ExternalEffectRecord(
                effect_id="effect-ci-1",
                target="github-actions",
                operation="workflow_dispatch",
                outcome="committed",
                provider_effect_id="gha-run-1",
            )
        ]
    )

    assert result.status == "incomplete"
    assert result.external_effect_was_committed() is True
    assert result.to_json()["external_effects"][0]["provider_effect_id"] == "gha-run-1"


def test_async_operation_result_rejects_invalid_external_effect_records() -> None:
    with raises_value_error("external effect effect_id must not be empty"):
        graphblocks.ExternalEffectRecord(
            effect_id=" ",
            target="ticket-system",
            operation="ticket.create",
            outcome="committed",
        )

    with raises_value_error("provider identity but no committed external effect"):
        graphblocks.AsyncOperationResult.failed("op-2").with_external_effects(
            [
                graphblocks.ExternalEffectRecord(
                    effect_id="effect-denied",
                    target="ticket-system",
                    operation="ticket.create",
                    outcome="no_external_effect",
                    provider_effect_id="ticket-123",
                )
            ]
        )


def test_async_operation_result_exports_are_available() -> None:
    assert "AsyncOperationResult" in graphblocks.__all__
    assert "ExternalEffectRecord" in graphblocks.__all__
    assert graphblocks.AsyncOperationResultStatus.CANCELLED == "cancelled"


def run_direct() -> None:
    tests: tuple[Callable[[], None], ...] = (
        test_async_operation_result_preserves_committed_effect_after_cancel,
        test_async_operation_result_preserves_committed_effect_after_incomplete_late_callback,
        test_async_operation_result_rejects_invalid_external_effect_records,
        test_async_operation_result_exports_are_available,
    )
    for test in tests:
        test()


if __name__ == "__main__":
    run_direct()
