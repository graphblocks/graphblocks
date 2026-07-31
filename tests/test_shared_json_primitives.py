from __future__ import annotations

import pytest

import graphblocks.blob_store as blob_store
import graphblocks.client as client
import graphblocks.server as server
from graphblocks._json import reject_duplicate_json_keys


DUPLICATE_KEY_HOOKS = (
    blob_store._reject_duplicate_json_keys,
    client._reject_duplicate_json_keys,
    server._reject_duplicate_json_keys,
)
STRICT_JSON_LOADERS = (
    blob_store._loads_strict_json,
    client._strict_json_loads,
)
DUPLICATE_JSON_CASES = (
    ('{"trusted":1,"trusted":2}', "trusted"),
    ('{"outer":{"nested":1,"nested":2}}', "nested"),
    ('{"escaped":1,"\\u0065scaped":2}', "escaped"),
)


@pytest.mark.parametrize("hook", DUPLICATE_KEY_HOOKS)
def test_shared_duplicate_key_hook_preserves_order_and_values(hook) -> None:
    assert hook is reject_duplicate_json_keys
    first = object()
    second = object()

    decoded = hook([("first", first), ("second", second)])

    assert list(decoded) == ["first", "second"]
    assert decoded["first"] is first
    assert decoded["second"] is second


@pytest.mark.parametrize("hook", DUPLICATE_KEY_HOOKS)
def test_shared_duplicate_key_hook_rejects_exact_duplicates(hook) -> None:
    with pytest.raises(ValueError) as raised:
        hook([("trusted", 1), ("trusted", 2)])

    assert str(raised.value) == "duplicate JSON object key 'trusted'"


@pytest.mark.parametrize("loader", STRICT_JSON_LOADERS)
@pytest.mark.parametrize(("document", "duplicate_key"), DUPLICATE_JSON_CASES)
def test_shared_duplicate_key_hook_is_used_by_strict_loaders(
    loader,
    document: str,
    duplicate_key: str,
) -> None:
    assert loader('{"first":1,"second":2}') == {"first": 1, "second": 2}

    with pytest.raises(ValueError) as raised:
        loader(document)

    assert str(raised.value) == f"duplicate JSON object key {duplicate_key!r}"


@pytest.mark.parametrize(("document", "duplicate_key"), DUPLICATE_JSON_CASES)
def test_shared_duplicate_key_hook_is_used_by_server_request_parser(
    document: str,
    duplicate_key: str,
) -> None:
    request = server.ServerRequest(
        method="POST",
        path="/runs",
        headers={},
        query={},
        cookies={},
        body=document.encode(),
    )

    with pytest.raises(ValueError) as raised:
        server._server_request_json_body(request, "contract")

    assert str(raised.value) == "contract body must be valid JSON"
    assert str(raised.value.__cause__) == (
        f"duplicate JSON object key {duplicate_key!r}"
    )
