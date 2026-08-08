from __future__ import annotations

import json

import pytest

from graphblocks.server import (
    GraphBlocksServerApp,
    MAX_SERVER_IDENTIFIER_BYTES,
    MAX_SERVER_REASON_BYTES,
    MAX_SERVER_TIMESTAMP_BYTES,
    ServerRequest,
    _validate_exact_non_empty_string,
    _validate_iso_datetime,
    _validate_non_empty_string,
)


def _empty_graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "server-field-limits"},
        "spec": {"nodes": {}},
    }


def _invoke(app: GraphBlocksServerApp, run_id: str, **extra: object):
    return app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": _empty_graph(),
                    "inputs": {},
                    "runId": run_id,
                    **extra,
                }
            ).encode("utf-8"),
        )
    )


def test_server_identifier_policy_has_exact_byte_and_ascii_boundaries() -> None:
    boundary = "a" * MAX_SERVER_IDENTIFIER_BYTES
    assert _validate_exact_non_empty_string("test", "runId", boundary) == boundary
    assert (
        _validate_exact_non_empty_string(
            "test",
            "runId",
            "run/accepted?query#fragment",
        )
        == "run/accepted?query#fragment"
    )

    with pytest.raises(
        ValueError,
        match=rf"{MAX_SERVER_IDENTIFIER_BYTES} UTF-8 bytes",
    ):
        _validate_exact_non_empty_string("test", "runId", f"{boundary}a")
    with pytest.raises(ValueError, match="printable ASCII"):
        _validate_exact_non_empty_string("test", "runId", "실행-1")


@pytest.mark.parametrize(
    "field_name",
    (
        "runId",
        "tenant_id",
        "idempotency_key",
        "reason_codes",
        "payload_digest",
    ),
)
def test_server_identifier_field_families_share_the_closed_policy(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match="printable ASCII"):
        _validate_exact_non_empty_string("test", field_name, "identifier-é")


@pytest.mark.parametrize(
    "value",
    (
        "identifier\x00suffix",
        "identifier\r\nforged-log",
        "identifier\u202espoofed",
        "identifier\u2028forged-line",
    ),
)
def test_server_text_policy_rejects_control_and_directional_characters(
    value: str,
) -> None:
    with pytest.raises(ValueError, match="control or directional"):
        _validate_exact_non_empty_string("test", "runId", value)
    with pytest.raises(ValueError, match="control or directional"):
        _validate_non_empty_string("test", "reason", value)


def test_server_free_text_policy_is_unicode_normalized_and_byte_bounded() -> None:
    reason = "요청 취소"
    assert _validate_non_empty_string("test", "reason", reason) == reason
    assert (
        _validate_non_empty_string(
            "test",
            "reason",
            "a" * MAX_SERVER_REASON_BYTES,
        )
        == "a" * MAX_SERVER_REASON_BYTES
    )

    with pytest.raises(ValueError, match="NFC Unicode normalization"):
        _validate_non_empty_string("test", "reason", "cafe\u0301")
    with pytest.raises(ValueError, match="4096 UTF-8 bytes"):
        _validate_non_empty_string(
            "test",
            "reason",
            "a" * (MAX_SERVER_REASON_BYTES + 1),
        )


def test_server_timestamp_policy_applies_before_datetime_parsing() -> None:
    timestamp = "2026-08-08T00:00:00Z"
    assert _validate_iso_datetime("test", "occurredAt", timestamp) == timestamp

    with pytest.raises(
        ValueError,
        match=rf"{MAX_SERVER_TIMESTAMP_BYTES} UTF-8 bytes",
    ):
        _validate_iso_datetime(
            "test",
            "occurredAt",
            timestamp + ("0" * MAX_SERVER_TIMESTAMP_BYTES),
        )
    with pytest.raises(ValueError, match="control or directional"):
        _validate_iso_datetime("test", "occurredAt", f"{timestamp}\r\n")


@pytest.mark.parametrize(
    "run_id",
    (
        "a" * (MAX_SERVER_IDENTIFIER_BYTES + 1),
        "run\x00suffix",
        "run\r\nforged-log",
        "run\u202espoofed",
        "cafe\u0301",
        "실행-1",
    ),
)
def test_run_invocation_rejects_unsafe_ids_without_reflecting_them(
    run_id: str,
) -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)

    response = _invoke(app, run_id)
    payload = json.loads(response.body)

    assert response.status_code == 400
    assert payload["errorCode"] == "server.run.invalid_request"
    assert payload["message"] == "The run request is invalid."
    assert run_id not in response.body.decode("utf-8")


@pytest.mark.parametrize(
    "reason",
    (
        "a" * (MAX_SERVER_REASON_BYTES + 1),
        "cancel\x00hidden",
        "cancel\r\nforged-log",
        "cancel\u202espoofed",
        "cafe\u0301",
    ),
)
def test_detach_rejects_unsafe_reasons_without_recording_them(reason: str) -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)
    created = _invoke(app, "run-reason-limits")
    assert created.status_code == 200

    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-reason-limits/detach",
            headers={},
            query={},
            cookies={},
            body=json.dumps({"clientId": "client-1", "reason": reason}).encode("utf-8"),
        )
    )

    assert response.status_code == 400
    assert app.detachments("run-reason-limits") == ()


def test_multi_megabyte_identifier_is_stopped_by_request_budget_first() -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)

    response = _invoke(app, "a" * (2 * 1024 * 1024))

    assert response.status_code == 413
