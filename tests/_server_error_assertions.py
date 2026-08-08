from __future__ import annotations

import json
from typing import Protocol


class _Response(Protocol):
    status_code: int
    headers: dict[str, str]
    body: bytes


def assert_safe_server_error(
    response: _Response,
    error_code: str,
) -> dict[str, object]:
    payload = json.loads(response.body)
    assert payload == {
        "ok": False,
        "errorCode": error_code,
        "message": payload["message"],
        "correlationId": payload["correlationId"],
    }
    assert isinstance(payload["message"], str)
    assert payload["message"] == payload["message"].strip()
    assert payload["message"]
    assert isinstance(payload["correlationId"], str)
    assert payload["correlationId"] == payload["correlationId"].strip()
    assert payload["correlationId"]
    assert response.headers["x-correlation-id"] == payload["correlationId"]
    return payload
