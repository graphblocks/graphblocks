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


def test_event_filter_matches_authoritative_application_event_metadata() -> None:
    event = graphblocks.ApplicationEvent.new(
        "RunStarted",
        graphblocks.ApplicationEventMetadata(
            event_id="evt-1",
            run_id="run-1",
            response_id="response-1",
            sequence=7,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-07-02T00:00:00Z",
            cursor="run-1:7",
            node_id="node-plan",
            operation_id="operation-ci",
            visibility="operator",
        ),
        payload={"severity": "warning"},
    )

    assert graphblocks.EventFilter(
        types=["RunStarted"],
        visibility=["operator"],
        node_ids=["node-plan"],
        operation_ids=["operation-ci"],
        severity_min="info",
    ).matches(event)
    assert not graphblocks.EventFilter(visibility=["client"]).matches(event)
    assert not graphblocks.EventFilter(node_ids=["node-other"]).matches(event)
    assert not graphblocks.EventFilter(operation_ids=["operation-other"]).matches(event)
    assert not graphblocks.EventFilter(severity_min="error").matches(event)


def test_event_filter_matches_application_protocol_operation_metadata() -> None:
    event = graphblocks.ApplicationProtocolEvent.new(
        "ExternalCallbackReceived",
        graphblocks.ApplicationProtocolEventMetadata(
            event_id="evt-callback-1",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=9,
            occurred_at_unix_ms=1_765_843_202_000,
            cursor="run-1:9",
            operation_id="operation-ci",
        ),
        payload={"severity": "info"},
    )
    wrong_operation = graphblocks.ApplicationProtocolEvent.new(
        "ExternalCallbackReceived",
        graphblocks.ApplicationProtocolEventMetadata(
            event_id="evt-callback-2",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=10,
            occurred_at_unix_ms=1_765_843_202_100,
            cursor="run-1:10",
            operation_id="operation-other",
        ),
        payload={"severity": "info"},
    )

    assert graphblocks.EventFilter(operation_ids=["operation-ci"]).matches(event)
    assert not graphblocks.EventFilter(operation_ids=["operation-ci"]).matches(wrong_operation)


def test_event_filter_defaults_missing_protocol_visibility_to_client() -> None:
    default_client_event = graphblocks.ApplicationProtocolEvent.new(
        "RunStarted",
        graphblocks.ApplicationProtocolEventMetadata(
            event_id="evt-default-client",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=11,
            occurred_at_unix_ms=1_765_843_202_000,
            cursor="run-1:11",
        ),
        payload={},
    )
    malformed_visibility_event = graphblocks.ApplicationProtocolEvent.new(
        "RunStarted",
        graphblocks.ApplicationProtocolEventMetadata(
            event_id="evt-malformed-visibility",
            protocol_version="graphblocks.app.v1",
            run_id="run-1",
            sequence=12,
            occurred_at_unix_ms=1_765_843_202_100,
            cursor="run-1:12",
        ),
        payload={"visibility": True},
    )
    filter_ = graphblocks.EventFilter(types=["RunStarted"], visibility=["client"]).authorized_for_visibility(
        ["client"]
    )

    assert filter_.matches(default_client_event)
    assert not filter_.matches(malformed_visibility_event)


def test_event_filter_visibility_is_constrained_by_subscriber_authorization() -> None:
    requested = graphblocks.EventFilter(
        types=["RunStarted"],
        visibility=["client", "operator"],
        node_ids=["node-plan"],
    )
    authorized = requested.authorized_for_visibility(["client"])
    client_event = graphblocks.ApplicationEvent.new(
        "RunStarted",
        graphblocks.ApplicationEventMetadata(
            event_id="evt-client",
            run_id="run-1",
            response_id="response-1",
            sequence=7,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-07-02T00:00:00Z",
            cursor="run-1:7",
            node_id="node-plan",
            visibility="client",
        ),
        payload={},
    )
    operator_event = graphblocks.ApplicationEvent.new(
        "RunStarted",
        graphblocks.ApplicationEventMetadata(
            event_id="evt-operator",
            run_id="run-1",
            response_id="response-1",
            sequence=8,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-07-02T00:00:01Z",
            cursor="run-1:8",
            node_id="node-plan",
            visibility="operator",
        ),
        payload={},
    )

    assert authorized.visibility == ("client",)
    assert authorized.matches(client_event)
    assert not authorized.matches(operator_event)
    assert graphblocks.EventFilter(visibility=["operator"]).authorized_for_visibility(["client"]).visibility == ()
    with raises_value_error("event filter authorized visibility must contain only valid visibility values"):
        requested.authorized_for_visibility(["private"])


def test_event_filter_excludes_terminal_events_when_disabled() -> None:
    failed = graphblocks.ApplicationEvent.new(
        "RunFailed",
        graphblocks.ApplicationEventMetadata(
            event_id="evt-2",
            run_id="run-1",
            response_id="response-1",
            sequence=8,
            release_id="release-1",
            policy_snapshot_id="policy-1",
            occurred_at="2026-07-02T00:00:01Z",
            cursor="run-1:8",
        ),
        payload={"severity": "error"},
    )

    assert graphblocks.EventFilter(include_terminal_events=True).matches(failed)
    assert not graphblocks.EventFilter(include_terminal_events=False).matches(failed)


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
    retry_scheduled = graphblocks.CallbackDelivery(
        delivery_id="del-retry-1",
        subscription_id="sub-1",
        event_id="evt-retry-1",
        run_id="run-1",
        sequence=9,
        cursor="run-1:9",
        attempt=2,
        idempotency_key="sub-1:evt-retry-1",
        status="failed",
        next_retry_at="2026-07-02T00:00:30Z",
        last_error="receiver returned 503",
    )

    assert delivered.to_json()["status"] == "delivered"
    assert acknowledged.to_json()["acknowledged_at"] == "2026-07-02T00:00:02Z"
    assert retry_scheduled.to_json()["next_retry_at"] == "2026-07-02T00:00:30Z"

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

    with raises_value_error("callback delivery acknowledged_at requires acknowledged status"):
        graphblocks.CallbackDelivery(
            delivery_id="del-5",
            subscription_id="sub-1",
            event_id="evt-5",
            run_id="run-1",
            sequence=11,
            cursor="run-1:11",
            attempt=1,
            idempotency_key="sub-1:evt-5",
            status="delivered",
            delivered_at="2026-07-02T00:00:01Z",
            acknowledged_at="2026-07-02T00:00:02Z",
        )

    for status in ("failed", "dead_lettered", "cancelled", "expired"):
        with raises_value_error("terminal failure callback delivery requires last_error"):
            graphblocks.CallbackDelivery(
                delivery_id=f"del-{status}",
                subscription_id="sub-1",
                event_id=f"evt-{status}",
                run_id="run-1",
                sequence=12,
                cursor=f"run-1:{status}",
                attempt=1,
                idempotency_key=f"sub-1:evt-{status}",
                status=status,
            )

    for status in ("delivered", "acknowledged", "dead_lettered", "cancelled", "expired"):
        with raises_value_error("terminal callback delivery must not have next_retry_at"):
            graphblocks.CallbackDelivery(
                delivery_id=f"del-terminal-retry-{status}",
                subscription_id="sub-1",
                event_id=f"evt-terminal-retry-{status}",
                run_id="run-1",
                sequence=13,
                cursor=f"run-1:terminal-retry-{status}",
                attempt=1,
                idempotency_key=f"sub-1:evt-terminal-retry-{status}",
                status=status,
                delivered_at="2026-07-02T00:00:01Z" if status in {"delivered", "acknowledged"} else None,
                acknowledged_at="2026-07-02T00:00:02Z" if status == "acknowledged" else None,
                next_retry_at="2026-07-02T00:00:30Z",
                last_error="terminal delivery cannot retry" if status not in {"delivered", "acknowledged"} else None,
            )


def test_callback_schema_exports_are_available() -> None:
    assert "EventFilter" in graphblocks.__all__
    assert "CallbackSubscription" in graphblocks.__all__
    assert "CallbackDelivery" in graphblocks.__all__
