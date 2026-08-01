from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sys
from pathlib import Path
from threading import Event

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


from graphblocks.policy import PrincipalRef  # noqa: E402
from graphblocks.server import (  # noqa: E402
    GraphBlocksServerApp,
    ServerCallbackDeliveryResult,
    ServerRequest,
    StaticBearerAuthHook,
)
from graphblocks.callbacks import (  # noqa: E402
    CallbackEnvelope,
    RegisteredSecretWebhookDispatcher,
    WebhookTransportResponse,
    verify_webhook_headers_hmac_sha256,
)


class RecordingSecretResolver:
    def __init__(self, secrets: dict[str, bytes]) -> None:
        self.secrets = secrets
        self.lookups: list[str] = []

    def resolve(self, secret_ref: str) -> bytes:
        self.lookups.append(secret_ref)
        return self.secrets[secret_ref]


class RecordingWebhookTransport:
    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.requests: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        body: bytes,
        headers: dict[str, str],
        resolved_addresses: tuple[str, ...],
    ) -> WebhookTransportResponse:
        self.requests.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "resolved_addresses": resolved_addresses,
            }
        )
        return WebhookTransportResponse(self.status_code)


def _app_with_terminal_event(
    resolver: RecordingSecretResolver,
    transport: RecordingWebhookTransport,
) -> GraphBlocksServerApp:
    dispatcher = RegisteredSecretWebhookDispatcher(
        secret_resolver=resolver,
        transport=transport,
        delivered_at_factory=lambda: "2026-07-10T01:00:00Z",
        hostname_resolver=lambda host, port: ("93.184.216.34",),
    )
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook({"token-1": PrincipalRef("user-1", tenant_id="tenant-1")}),
        callback_delivery_hook=dispatcher,
    )
    app._record_run_authorization(
        "run-delivery-1",
        PrincipalRef("user-1", tenant_id="tenant-1"),
        "2026-07-10T00:00:00Z",
    )
    app._events_by_run_id["run-delivery-1"] = (
        {
            "kind": "RunStarted",
            "metadata": {
                "eventId": "event-started-1",
                "runId": "run-delivery-1",
                "sequence": 1,
                "cursor": "run-delivery-1:1",
                "releaseId": "release-1",
                "occurredAt": "2026-07-10T00:00:00Z",
                "visibility": "client",
            },
            "payload": {"runId": "run-delivery-1"},
        },
        {
            "kind": "RunSucceeded",
            "metadata": {
                "eventId": "event/succeeded_1",
                "runId": "run-delivery-1",
                "sequence": 2,
                "cursor": "run-delivery-1:2",
                "releaseId": "release-1",
                "occurredAt": "2026-07-10T00:00:01Z",
                "visibility": "client",
            },
            "payload": {"outputs": {"prompt": "done"}},
        },
    )
    return app


def _register_request(
    secret_ref: str,
    *,
    event_types: tuple[str, ...] = ("RunSucceeded",),
    replay_from_cursor: str = "run-delivery-1:1",
) -> ServerRequest:
    return ServerRequest(
        method="POST",
        path="/callbacks/register",
        headers={"Authorization": "Bearer token-1"},
        query={},
        cookies={},
        body=json.dumps(
            {
                "subscriptionId": "callback-sub-delivery-1",
                "scope": "run",
                "scopeId": "run-delivery-1",
                "eventFilter": {"types": list(event_types)},
                "delivery": {
                    "kind": "webhook",
                    "url": "https://relay.example/events",
                    "signing": {
                        "algorithm": "hmac-sha256",
                        "secret_ref": secret_ref,
                        "key_id": "relay-key-1",
                    },
                },
                "replayFromCursor": replay_from_cursor,
                "failurePolicy": "retry_then_dead_letter",
                "deadLetterPolicy": "webhook-standard",
            }
        ).encode("utf-8"),
        requested_at="2026-07-10T01:00:00Z",
    )


def test_server_registration_delivers_replayed_event_with_resolved_hmac_secret() -> None:
    secret = b"registered-secret-value"
    resolver = RecordingSecretResolver({"secret://callbacks/ide-relay": secret})
    transport = RecordingWebhookTransport()
    app = _app_with_terminal_event(resolver, transport)

    response = app.handle(_register_request("secret://callbacks/ide-relay"))

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert resolver.lookups == ["secret://callbacks/ide-relay"]
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["url"] == "https://relay.example/events"
    assert request["resolved_addresses"] == ("93.184.216.34",)
    envelope_payload = json.loads(request["body"])
    envelope = CallbackEnvelope(**envelope_payload)
    assert envelope.to_payload() == {
        "delivery_id": "del_callback-sub-delivery-1_event%2Fsucceeded%5F1",
        "subscription_id": "callback-sub-delivery-1",
        "event_id": "event/succeeded_1",
        "run_id": "run-delivery-1",
        "sequence": 2,
        "cursor": "run-delivery-1:2",
        "type": "RunSucceeded",
        "payload": {"outputs": {"prompt": "done"}},
        "idempotency_key": "callback-sub-delivery-1:event%2Fsucceeded%5F1",
        "occurred_at": "2026-07-10T00:00:01Z",
        "delivered_at": "2026-07-10T01:00:00Z",
        "release_id": "release-1",
        "tenant_id": "tenant-1",
    }
    headers = request["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["GraphBlocks-Key-Id"] == "relay-key-1"
    assert verify_webhook_headers_hmac_sha256(
        envelope,
        headers,
        secret,
        now="2026-07-10T01:00:00Z",
    )
    assert payload["deliveries"] == [
        {
            "deliveryId": "del_callback-sub-delivery-1_event%2Fsucceeded%5F1",
            "subscriptionId": "callback-sub-delivery-1",
            "eventId": "event/succeeded_1",
            "runId": "run-delivery-1",
            "sequence": 2,
            "cursor": "run-delivery-1:2",
            "attempt": 1,
            "idempotencyKey": "callback-sub-delivery-1:event%2Fsucceeded%5F1",
            "status": "delivered",
            "stateVersion": 1,
            "statusCode": 202,
            "deliveredAt": "2026-07-10T01:00:00Z",
        }
    ]
    assert app.callback_delivery_results("callback-sub-delivery-1") == tuple(payload["deliveries"])
    serialized_evidence = json.dumps(payload, sort_keys=True)
    assert secret.decode("utf-8") not in serialized_evidence
    assert secret not in request["body"]
    assert all(secret.decode("utf-8") not in value for value in headers.values())


def test_server_registration_exact_retry_resumes_after_partial_delivery_failure() -> None:
    class FailSecondDeliveryOnce:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.failed = False

        def deliver(self, registration, event) -> ServerCallbackDeliveryResult:
            metadata = event["metadata"]
            event_id = metadata["eventId"]
            self.calls.append(event_id)
            if event_id == "event/succeeded_1" and not self.failed:
                self.failed = True
                raise RuntimeError("delivery transport unavailable")
            return ServerCallbackDeliveryResult(
                delivery_id=f"delivery-{event_id}",
                subscription_id=registration.subscription_id,
                event_id=event_id,
                run_id=metadata["runId"],
                sequence=metadata["sequence"],
                cursor=metadata["cursor"],
                attempt=1,
                idempotency_key=f"{registration.subscription_id}:{event_id}",
                status="delivered",
                status_code=202,
                delivered_at="2026-07-10T01:00:00Z",
            )

    resolver = RecordingSecretResolver({})
    transport = RecordingWebhookTransport()
    app = _app_with_terminal_event(resolver, transport)
    delivery_hook = FailSecondDeliveryOnce()
    app.callback_delivery_hook = delivery_hook
    request = _register_request(
        "secret://callbacks/ide-relay",
        event_types=("RunStarted", "RunSucceeded"),
        replay_from_cursor="run-delivery-1:0",
    )

    first = app.handle(request)
    retried = app.handle(request)

    assert first.status_code == 502
    assert retried.status_code == 200
    assert delivery_hook.calls == ["event-started-1", "event/succeeded_1", "event/succeeded_1"]
    assert [
        result["eventId"]
        for result in app.callback_delivery_results("callback-sub-delivery-1")
    ] == ["event-started-1", "event/succeeded_1"]
    assert len(app.callback_registrations()) == 1


def test_server_registration_claim_does_not_hold_lock_during_webhook_delivery() -> None:
    class BlockingDelivery:
        def __init__(self) -> None:
            self.entered = Event()
            self.release = Event()
            self.calls = 0

        def deliver(self, registration, event) -> ServerCallbackDeliveryResult:
            self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=2)
            metadata = event["metadata"]
            return ServerCallbackDeliveryResult(
                delivery_id="delivery-terminal",
                subscription_id=registration.subscription_id,
                event_id=metadata["eventId"],
                run_id=metadata["runId"],
                sequence=metadata["sequence"],
                cursor=metadata["cursor"],
                attempt=1,
                idempotency_key="callback-sub-delivery-1:event-terminal",
                status="delivered",
                status_code=202,
                delivered_at="2026-07-10T01:00:00Z",
            )

    app = _app_with_terminal_event(RecordingSecretResolver({}), RecordingWebhookTransport())
    delivery_hook = BlockingDelivery()
    app.callback_delivery_hook = delivery_hook
    request = _register_request("secret://callbacks/ide-relay")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(app.handle, request)
        assert delivery_hook.entered.wait(timeout=2)
        duplicate = executor.submit(app.handle, request).result(timeout=1)
        delivery_hook.release.set()
        first = first_future.result(timeout=2)

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert json.loads(duplicate.body)["state"] == "pending"
    assert delivery_hook.calls == 1


@pytest.mark.parametrize(
    ("operation", "expected_status", "expected_attempt"),
    (
        ("redrive", "pending", 2),
        ("dead-letter", "dead_lettered", 1),
    ),
)
def test_server_registration_replay_preserves_concurrent_delivery_control(
    operation: str,
    expected_status: str,
    expected_attempt: int,
) -> None:
    class BlockingSecondDelivery:
        def __init__(self) -> None:
            self.entered = Event()
            self.release = Event()

        def deliver(self, registration, event) -> ServerCallbackDeliveryResult:
            self.entered.set()
            assert self.release.wait(timeout=2)
            metadata = event["metadata"]
            return ServerCallbackDeliveryResult(
                delivery_id="delivery-event-succeeded-1",
                subscription_id=registration.subscription_id,
                event_id=metadata["eventId"],
                run_id=metadata["runId"],
                sequence=metadata["sequence"],
                cursor=metadata["cursor"],
                attempt=1,
                idempotency_key=(
                    f"{registration.subscription_id}:{metadata['eventId']}"
                ),
                status="delivered",
                status_code=202,
                delivered_at="2026-07-10T01:00:02Z",
            )

    app = _app_with_terminal_event(
        RecordingSecretResolver({}),
        RecordingWebhookTransport(),
    )
    delivery_hook = BlockingSecondDelivery()
    app.callback_delivery_hook = delivery_hook
    subscription_id = "callback-sub-delivery-1"
    controlled_delivery_id = "delivery-event-started-1"
    app._callback_delivery_results_by_subscription_id[subscription_id] = (
        ServerCallbackDeliveryResult(
            delivery_id=controlled_delivery_id,
            subscription_id=subscription_id,
            event_id="event-started-1",
            run_id="run-delivery-1",
            sequence=1,
            cursor="run-delivery-1:1",
            attempt=1,
            idempotency_key=f"{subscription_id}:event-started-1",
            status="failed",
            last_error="receiver unavailable",
        ),
    )
    registration_request = _register_request(
        "secret://callbacks/ide-relay",
        event_types=("RunStarted", "RunSucceeded"),
        replay_from_cursor="run-delivery-1:0",
    )
    control_request = ServerRequest(
        method="POST",
        path=f"/callbacks/deliveries/{controlled_delivery_id}/{operation}",
        headers={"Authorization": "Bearer token-1"},
        query={},
        cookies={},
        body=json.dumps({"reason": "receiver state changed"}).encode("utf-8"),
        requested_at="2026-07-10T01:00:01Z",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        registration_future = executor.submit(app.handle, registration_request)
        assert delivery_hook.entered.wait(timeout=2)
        control_response = app.handle(control_request)
        delivery_hook.release.set()
        registration_response = registration_future.result(timeout=2)

    assert control_response.status_code == 202
    assert registration_response.status_code == 201
    retained = app.callback_delivery_results(subscription_id)
    assert [result["eventId"] for result in retained] == [
        "event-started-1",
        "event/succeeded_1",
    ]
    assert retained[0]["status"] == expected_status
    assert retained[0]["attempt"] == expected_attempt
    assert retained[0]["stateVersion"] == 2
    response_deliveries = json.loads(registration_response.body)["deliveries"]
    assert response_deliveries == list(retained)


@pytest.mark.parametrize(
    (
        "retained_status",
        "retained_state_version",
        "expected_status_code",
        "expected_error",
    ),
    (
        ("delivered", 1, 201, None),
        ("failed", 2, 201, None),
        (
            "failed",
            1,
            502,
            "callback delivery hook completion conflicts with retained event state",
        ),
    ),
)
def test_server_registration_reconciles_duplicate_hook_completion_by_state_version(
    retained_status: str,
    retained_state_version: int,
    expected_status_code: int,
    expected_error: str | None,
) -> None:
    class BlockingDuplicateDelivery:
        def __init__(self) -> None:
            self.entered = Event()
            self.release = Event()

        def deliver(self, registration, event) -> ServerCallbackDeliveryResult:
            self.entered.set()
            assert self.release.wait(timeout=2)
            metadata = event["metadata"]
            return ServerCallbackDeliveryResult(
                delivery_id="delivery-event-succeeded-1",
                subscription_id=registration.subscription_id,
                event_id=metadata["eventId"],
                run_id=metadata["runId"],
                sequence=metadata["sequence"],
                cursor=metadata["cursor"],
                attempt=1,
                idempotency_key=(
                    f"{registration.subscription_id}:{metadata['eventId']}"
                ),
                status="delivered",
                status_code=202,
                delivered_at="2026-07-10T01:00:02Z",
            )

    app = _app_with_terminal_event(
        RecordingSecretResolver({}),
        RecordingWebhookTransport(),
    )
    delivery_hook = BlockingDuplicateDelivery()
    app.callback_delivery_hook = delivery_hook
    subscription_id = "callback-sub-delivery-1"
    request = _register_request("secret://callbacks/ide-relay")

    with ThreadPoolExecutor(max_workers=2) as executor:
        registration_future = executor.submit(app.handle, request)
        assert delivery_hook.entered.wait(timeout=2)
        retained_completion = ServerCallbackDeliveryResult(
            delivery_id="delivery-event-succeeded-1",
            subscription_id=subscription_id,
            event_id="event/succeeded_1",
            run_id="run-delivery-1",
            sequence=2,
            cursor="run-delivery-1:2",
            attempt=1,
            idempotency_key=f"{subscription_id}:event/succeeded_1",
            status=retained_status,
            state_version=retained_state_version,
            status_code=202 if retained_status == "delivered" else None,
            delivered_at=(
                "2026-07-10T01:00:02Z"
                if retained_status == "delivered"
                else None
            ),
            last_error=(
                "concurrent delivery failed" if retained_status == "failed" else None
            ),
        )
        with app._callback_registration_condition:
            app._callback_delivery_results_by_subscription_id[subscription_id] = (
                retained_completion,
            )
        delivery_hook.release.set()
        response = registration_future.result(timeout=2)

    assert response.status_code == expected_status_code
    assert app.callback_delivery_results(subscription_id) == (
        retained_completion.protocol_value(),
    )
    response_payload = json.loads(response.body)
    if expected_error is None:
        assert response_payload["deliveries"] == [
            retained_completion.protocol_value()
        ]
    else:
        assert response_payload["error"] == expected_error


def test_server_registration_retry_recovers_after_hook_identity_conflict() -> None:
    class ConflictingDeliveryOnce:
        def __init__(self) -> None:
            self.calls = 0

        def deliver(self, registration, event) -> ServerCallbackDeliveryResult:
            self.calls += 1
            metadata = event["metadata"]
            return ServerCallbackDeliveryResult(
                delivery_id=(
                    "delivery-event-started-1"
                    if self.calls == 1
                    else "delivery-event-succeeded-1"
                ),
                subscription_id=registration.subscription_id,
                event_id=metadata["eventId"],
                run_id=metadata["runId"],
                sequence=metadata["sequence"],
                cursor=metadata["cursor"],
                attempt=1,
                idempotency_key=f"{registration.subscription_id}:{metadata['eventId']}",
                status="delivered",
                status_code=202,
                delivered_at="2026-07-10T01:00:02Z",
            )

    app = _app_with_terminal_event(
        RecordingSecretResolver({}),
        RecordingWebhookTransport(),
    )
    delivery_hook = ConflictingDeliveryOnce()
    app.callback_delivery_hook = delivery_hook
    subscription_id = "callback-sub-delivery-1"
    app._callback_delivery_results_by_subscription_id[subscription_id] = (
        ServerCallbackDeliveryResult(
            delivery_id="delivery-event-started-1",
            subscription_id=subscription_id,
            event_id="event-started-1",
            run_id="run-delivery-1",
            sequence=1,
            cursor="run-delivery-1:1",
            attempt=1,
            idempotency_key=f"{subscription_id}:event-started-1",
            status="failed",
            last_error="receiver unavailable",
        ),
    )
    request = _register_request(
        "secret://callbacks/ide-relay",
        event_types=("RunStarted", "RunSucceeded"),
        replay_from_cursor="run-delivery-1:0",
    )

    conflict = app.handle(request)
    retried = app.handle(request)

    assert conflict.status_code == 502
    assert json.loads(conflict.body)["error"] == (
        "callback delivery hook completion reuses a retained delivery identity"
    )
    assert retried.status_code == 200
    assert delivery_hook.calls == 2
    assert [
        result["eventId"] for result in app.callback_delivery_results(subscription_id)
    ] == ["event-started-1", "event/succeeded_1"]


def test_server_registration_rejects_non_initial_hook_state_version() -> None:
    class NonInitialDelivery:
        def deliver(self, registration, event) -> ServerCallbackDeliveryResult:
            metadata = event["metadata"]
            return ServerCallbackDeliveryResult(
                delivery_id="delivery-event-succeeded-1",
                subscription_id=registration.subscription_id,
                event_id=metadata["eventId"],
                run_id=metadata["runId"],
                sequence=metadata["sequence"],
                cursor=metadata["cursor"],
                attempt=1,
                idempotency_key=(
                    f"{registration.subscription_id}:{metadata['eventId']}"
                ),
                status="delivered",
                state_version=2,
                status_code=202,
                delivered_at="2026-07-10T01:00:02Z",
            )

    app = _app_with_terminal_event(
        RecordingSecretResolver({}),
        RecordingWebhookTransport(),
    )
    app.callback_delivery_hook = NonInitialDelivery()

    response = app.handle(_register_request("secret://callbacks/ide-relay"))

    assert response.status_code == 502
    assert json.loads(response.body)["error"] == (
        "callback delivery hook returned a non-initial state version"
    )
    assert app.callback_delivery_results("callback-sub-delivery-1") == ()


def test_server_registration_rejects_webhook_hostname_resolving_to_private_address() -> None:
    resolver = RecordingSecretResolver({"secret://callbacks/ide-relay": b"registered-secret-value"})
    transport = RecordingWebhookTransport()
    app = _app_with_terminal_event(resolver, transport)
    app.callback_delivery_hook = RegisteredSecretWebhookDispatcher(
        secret_resolver=resolver,
        transport=transport,
        delivered_at_factory=lambda: "2026-07-10T01:00:00Z",
        hostname_resolver=lambda host, port: ("10.0.0.7",),
    )

    response = app.handle(_register_request("secret://callbacks/ide-relay"))

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert resolver.lookups == []
    assert transport.requests == []
    assert payload["deliveries"][0]["status"] == "failed"
    assert payload["deliveries"][0]["lastError"] == "unsafe_webhook_target"


def test_server_registration_fails_closed_when_webhook_hostname_resolution_errors() -> None:
    resolver = RecordingSecretResolver({"secret://callbacks/ide-relay": b"registered-secret-value"})
    transport = RecordingWebhookTransport()
    app = _app_with_terminal_event(resolver, transport)

    def unavailable_resolver(host: str, port: int) -> tuple[str, ...]:
        raise RuntimeError("resolver unavailable")

    app.callback_delivery_hook = RegisteredSecretWebhookDispatcher(
        secret_resolver=resolver,
        transport=transport,
        delivered_at_factory=lambda: "2026-07-10T01:00:00Z",
        hostname_resolver=unavailable_resolver,
    )

    response = app.handle(_register_request("secret://callbacks/ide-relay"))

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert resolver.lookups == []
    assert transport.requests == []
    assert payload["deliveries"][0]["status"] == "failed"
    assert payload["deliveries"][0]["lastError"] == "unsafe_webhook_target"


def test_server_registration_records_missing_secret_without_calling_transport() -> None:
    resolver = RecordingSecretResolver({})
    transport = RecordingWebhookTransport()
    app = _app_with_terminal_event(resolver, transport)

    response = app.handle(_register_request("secret://callbacks/missing"))

    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 201
    assert resolver.lookups == ["secret://callbacks/missing"]
    assert transport.requests == []
    assert payload["deliveries"] == [
        {
            "deliveryId": "del_callback-sub-delivery-1_event%2Fsucceeded%5F1",
            "subscriptionId": "callback-sub-delivery-1",
            "eventId": "event/succeeded_1",
            "runId": "run-delivery-1",
            "sequence": 2,
            "cursor": "run-delivery-1:2",
            "attempt": 1,
            "idempotencyKey": "callback-sub-delivery-1:event%2Fsucceeded%5F1",
            "status": "failed",
            "stateVersion": 1,
            "lastError": "secret_resolution_failed",
        }
    ]
    assert app.callback_delivery_results("callback-sub-delivery-1") == tuple(payload["deliveries"])


def test_webhook_dispatcher_validates_collaborators_and_freezes_response_headers() -> None:
    with pytest.raises(ValueError, match="secret_resolver"):
        RegisteredSecretWebhookDispatcher(
            secret_resolver=object(),
            transport=RecordingWebhookTransport(),
        )
    with pytest.raises(ValueError, match="transport"):
        RegisteredSecretWebhookDispatcher(
            secret_resolver=RecordingSecretResolver({}),
            transport=object(),
        )

    response = WebhookTransportResponse(
        429,
        {"Retry-After": "1"},
    )
    with pytest.raises(TypeError):
        response.headers["Retry-After"] = "999"
