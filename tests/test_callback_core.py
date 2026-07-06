from __future__ import annotations

import re

import graphblocks


def raises_value_error(pattern: str):
    class RaisesValueError:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                raise AssertionError("expected ValueError")
            if exc_type is not ValueError:
                return False
            assert re.search(pattern, str(exc)), str(exc)
            return True

    return RaisesValueError()


def test_callback_subscription_schema_freezes_event_filter() -> None:
    filter_types = ["ReviewRequested", "RunCompleted"]
    subscription = graphblocks.CallbackSubscription(
        subscription_id="sub-1",
        owner="principal:ide",
        scope="run",
        scope_id="run-1",
        event_filter=graphblocks.EventFilter(types=filter_types, visibility=["client"]),
        delivery_target="webhook:ide-relay",
        status="active",
        created_at="2026-07-02T00:00:00Z",
        expires_at="2026-07-03T00:00:00Z",
        replay_from_cursor="run-1:7",
        failure_policy="retry_then_dead_letter",
    )
    filter_types.append("RunFailed")
    projected = subscription.to_json()
    projected["event_filter"]["types"].append("RunFailed")  # type: ignore[index]

    assert subscription.event_filter.types == ("ReviewRequested", "RunCompleted")
    assert subscription.event_filter.visibility == ("client",)
    assert subscription.to_json() == {
        "subscription_id": "sub-1",
        "owner": "principal:ide",
        "scope": "run",
        "scope_id": "run-1",
        "event_filter": {
            "types": ["ReviewRequested", "RunCompleted"],
            "visibility": ["client"],
            "node_ids": None,
            "operation_ids": None,
            "severity_min": None,
            "include_terminal_events": True,
        },
        "delivery_target": "webhook:ide-relay",
        "status": "active",
        "created_at": "2026-07-02T00:00:00Z",
        "expires_at": "2026-07-03T00:00:00Z",
        "replay_from_cursor": "run-1:7",
        "failure_policy": "retry_then_dead_letter",
    }


def test_callback_subscription_rejects_invalid_scope_status_and_expiration() -> None:
    with raises_value_error("callback subscription scope must be one of"):
        graphblocks.CallbackSubscription(
            subscription_id="sub-1",
            owner="principal:ide",
            scope="global",
            scope_id="run-1",
            event_filter=graphblocks.EventFilter(),
            delivery_target="webhook:ide-relay",
            status="active",
            created_at="2026-07-02T00:00:00Z",
        )

    with raises_value_error("callback subscription expires_at must be after created_at"):
        graphblocks.CallbackSubscription(
            subscription_id="sub-1",
            owner="principal:ide",
            scope="run",
            scope_id="run-1",
            event_filter=graphblocks.EventFilter(),
            delivery_target="webhook:ide-relay",
            status="active",
            created_at="2026-07-02T00:00:00Z",
            expires_at="2026-07-01T00:00:00Z",
        )

    with raises_value_error("event filter visibility must contain only valid visibility values"):
        graphblocks.EventFilter(visibility=["private"])


def test_callback_delivery_schema_validates_terminal_timestamps() -> None:
    delivered = graphblocks.CallbackDelivery(
        delivery_id="del-1",
        subscription_id="sub-1",
        event_id="evt-1",
        run_id="run-1",
        sequence=7,
        cursor="run-1:7",
        attempt=1,
        idempotency_key="sub-1:evt-1",
        status="delivered",
        delivered_at="2026-07-02T00:00:01Z",
    )
    acknowledged = graphblocks.CallbackDelivery(
        delivery_id="del-2",
        subscription_id="sub-1",
        event_id="evt-2",
        run_id="run-1",
        sequence=8,
        cursor="run-1:8",
        attempt=1,
        idempotency_key="sub-1:evt-2",
        status="acknowledged",
        delivered_at="2026-07-02T00:00:01Z",
        acknowledged_at="2026-07-02T00:00:02Z",
    )

    assert delivered.to_json()["status"] == "delivered"
    assert acknowledged.to_json()["acknowledged_at"] == "2026-07-02T00:00:02Z"

    with raises_value_error("delivered callback delivery requires delivered_at"):
        graphblocks.CallbackDelivery(
            delivery_id="del-3",
            subscription_id="sub-1",
            event_id="evt-3",
            run_id="run-1",
            sequence=9,
            cursor="run-1:9",
            attempt=1,
            idempotency_key="sub-1:evt-3",
            status="delivered",
        )

    with raises_value_error("acknowledged callback delivery requires acknowledged_at"):
        graphblocks.CallbackDelivery(
            delivery_id="del-4",
            subscription_id="sub-1",
            event_id="evt-4",
            run_id="run-1",
            sequence=10,
            cursor="run-1:10",
            attempt=1,
            idempotency_key="sub-1:evt-4",
            status="acknowledged",
            delivered_at="2026-07-02T00:00:01Z",
        )


def test_callback_schema_exports_are_available() -> None:
    assert "EventFilter" in graphblocks.__all__
    assert "CallbackSubscription" in graphblocks.__all__
    assert "CallbackDelivery" in graphblocks.__all__
