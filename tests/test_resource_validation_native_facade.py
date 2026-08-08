from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import graphblocks.schema as schema


def _error(
    *,
    path: str = "$.kind",
    schema_path: str = "$",
    keyword: str = "type",
    message: str = "kind must be a string",
) -> dict[str, str]:
    return {
        "code": "GB0012",
        "keyword": keyword,
        "message": message,
        "path": path,
        "schemaPath": schema_path,
    }


def test_public_resource_validation_dispatches_to_native_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def resource_schema_errors(document: object) -> tuple[dict[str, str], ...]:
        calls.append(document)
        return (_error(),)

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            resource_schema_errors=resource_schema_errors,
        ),
    )
    document = {"apiVersion": "graphblocks.ai/v1", "kind": 42}

    violations = schema.resource_schema_errors(document)

    assert calls == [document]
    assert violations == (
        schema.ResourceSchemaViolation(
            code="GB0012",
            path="$.kind",
            keyword="type",
            message="kind must be a string",
        ),
    )
    with pytest.raises(schema.ResourceValidationError) as captured:
        schema.validate_resource(document)
    assert captured.value.violations == violations


def test_public_resource_validation_requires_explicit_reference_for_schema_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            resource_schema_errors=lambda document: (),
        ),
    )

    with pytest.raises(ValueError, match="resource_schema_errors_reference"):
        schema.resource_schema_errors({}, schema_root="schemas")


def test_public_resource_validation_fails_closed_without_native_authority(
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
    document = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "reference"},
        "spec": {"nodes": {}},
    }

    with pytest.raises(
        schema.NativeResourceValidationUnavailableError,
        match="resource_schema_errors_reference explicitly: binding unavailable",
    ):
        schema.resource_schema_errors(document)
    assert schema.resource_schema_errors_reference(document) == ()


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ([], "result must be a tuple"),
        (({"code": "GB0012"},), "error 0 must be closed"),
        ((_error(message=""),), "error 0 must be closed"),
        (
            (
                _error(path="$.spec", message="second"),
                _error(path="$.kind", message="first"),
            ),
            "deterministically ordered",
        ),
    ),
)
def test_public_resource_validation_rejects_invalid_native_results(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    message: str,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            resource_schema_errors=lambda document: payload,
        ),
    )

    with pytest.raises(schema.NativeResourceValidationContractError, match=message):
        schema.resource_schema_errors({})


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
def test_public_resource_validation_closes_failed_native_handshakes(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    message: str,
) -> None:
    native = SimpleNamespace(
        native_extension_available=lambda: False,
        native_extension_status=lambda: {"error": "binding unavailable"},
        resource_schema_errors=lambda document: (),
    )

    def fail() -> object:
        raise RuntimeError("hostile handshake")

    setattr(native, attribute, fail)
    monkeypatch.setitem(sys.modules, "graphblocks_runtime", native)

    with pytest.raises(
        schema.NativeResourceValidationUnavailableError,
        match=message,
    ):
        schema.resource_schema_errors({})


def test_public_resource_validation_closes_failed_native_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(document: object) -> tuple[dict[str, str], ...]:
        raise RuntimeError("native failure")

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            resource_schema_errors=reject,
        ),
    )

    with pytest.raises(
        schema.NativeResourceValidationContractError,
        match="invocation failed",
    ):
        schema.resource_schema_errors({})
