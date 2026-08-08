from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from graphblocks import schema


def _valid_identity() -> dict[str, object]:
    return {
        "canonical": "schemas/Message@1",
        "majorVersion": 1,
        "name": "schemas/Message",
    }


def test_public_schema_id_facade_dispatches_to_native_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def parse_schema_id(value: str) -> dict[str, object]:
        calls.append(value)
        return _valid_identity()

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            parse_schema_id=parse_schema_id,
        ),
    )

    parsed = schema.SchemaId.parse("schemas/Message@1")
    constructed = schema.SchemaId("schemas/Message@1")

    assert parsed == constructed
    assert parsed.name == "schemas/Message"
    assert parsed.major_version == 1
    assert calls == ["schemas/Message@1", "schemas/Message@1"]


def test_public_schema_id_facade_fails_closed_without_native_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: False,
            native_extension_status=lambda: {"error": "binding unavailable"},
        ),
    )

    with pytest.raises(
        schema.NativeSchemaUnavailableError,
        match="SchemaId.parse_reference explicitly: binding unavailable",
    ):
        schema.SchemaId.parse("schemas/Message@1")
    assert schema.SchemaId.parse_reference("schemas/Message@1").major_version == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({"canonical": "schemas/Message@1"}, "closed identity object"),
        (
            {
                "canonical": "schemas/Message@1",
                "majorVersion": True,
                "name": "schemas/Message",
            },
            "invalid identity fields",
        ),
        (
            {
                "canonical": "schemas/Other@1",
                "majorVersion": 1,
                "name": "schemas/Other",
            },
            "differs from the reference oracle",
        ),
    ),
)
def test_public_schema_id_facade_rejects_invalid_native_results(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    message: str,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            parse_schema_id=lambda value: payload,
        ),
    )

    with pytest.raises(schema.NativeSchemaContractError, match=message):
        schema.SchemaId.parse("schemas/Message@1")


@pytest.mark.parametrize(
    ("attribute", "message"),
    (
        (
            "native_extension_available",
            "native extension availability check failed",
        ),
        ("native_extension_status", "native extension status check failed"),
    ),
)
def test_public_schema_id_facade_closes_failed_native_handshakes(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    message: str,
) -> None:
    native = SimpleNamespace(
        native_extension_available=lambda: False,
        native_extension_status=lambda: {"error": "binding unavailable"},
        parse_schema_id=lambda value: _valid_identity(),
    )

    def fail() -> object:
        raise RuntimeError("hostile handshake")

    setattr(native, attribute, fail)
    monkeypatch.setitem(sys.modules, "graphblocks_runtime", native)

    with pytest.raises(schema.NativeSchemaUnavailableError, match=message):
        schema.SchemaId.parse("schemas/Message@1")


def test_public_schema_id_facade_preserves_reference_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(value: str) -> dict[str, object]:
        raise ValueError("native rejected input")

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            parse_schema_id=reject,
        ),
    )

    with pytest.raises(schema.SchemaIdError, match="include a major version"):
        schema.SchemaId.parse("schemas/Message")
    with pytest.raises(
        schema.NativeSchemaContractError,
        match="rejected a reference-valid identity",
    ):
        schema.SchemaId.parse("schemas/Message@1")
