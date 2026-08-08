from __future__ import annotations

from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

import graphblocks.migration as migration


def test_public_migration_facade_dispatches_to_native_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def migrate_resource(document: dict[str, object]) -> dict[str, object]:
        calls.append(document)
        return {
            "document": {
                "apiVersion": "graphblocks.ai/v1",
                "kind": "Graph",
                "metadata": {
                    "annotations": {
                        "graphblocks.ai/migratedFrom": "graphblocks.ai/v1alpha3"
                    },
                    "name": "legacy",
                },
                "spec": {"nodes": {}},
            },
            "ok": True,
        }

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            migrate_resource=migrate_resource,
        ),
    )
    document = {
        "kind": "Graph",
        "spec": {"nodes": {}},
        "metadata": {"name": "legacy"},
        "apiVersion": "graphblocks.ai/v1alpha3",
    }
    source = deepcopy(document)

    migrated = migration.migrate_document(document)

    assert migrated["apiVersion"] == "graphblocks.ai/v1"
    assert calls == [source]
    assert document == source


def test_public_migration_facade_maps_native_semantic_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            migrate_resource=lambda document: {
                "error": {
                    "code": "GB0002",
                    "message": "future Graph versions are unsupported",
                    "path": "$.apiVersion",
                },
                "ok": False,
            },
        ),
    )

    with pytest.raises(migration.MigrationError) as captured:
        migration.migrate_document(
            {
                "apiVersion": "graphblocks.ai/v2",
                "kind": "Graph",
                "metadata": {"name": "future"},
                "spec": {"nodes": {}},
            }
        )

    assert captured.value.code == "GB0002"
    assert captured.value.path == "$.apiVersion"
    assert captured.value.message == "future Graph versions are unsupported"


def test_public_migration_facade_fails_closed_without_native_authority(
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
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "legacy"},
        "spec": {"nodes": {}},
    }

    with pytest.raises(
        migration.NativeMigrationUnavailableError,
        match="migrate_document_reference explicitly: binding unavailable",
    ):
        migration.migrate_document(document)
    assert migration.migrate_document_reference(document)["apiVersion"] == (
        "graphblocks.ai/v1"
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (None, "closed result object"),
        ({"ok": True}, "success must contain one document"),
        ({"document": [], "ok": True}, "success must contain one document"),
        (
            {"document": {"invalid": object()}, "ok": True},
            "document must contain canonical JSON values",
        ),
        ({"error": {}, "ok": False}, "error must be closed"),
        (
            {
                "error": {"code": "GB0002", "message": 1, "path": "$.apiVersion"},
                "ok": False,
            },
            "error must be closed",
        ),
        (
            {
                "error": {"code": "", "message": "missing", "path": "$.kind"},
                "ok": False,
            },
            "error must be closed",
        ),
    ),
)
def test_public_migration_facade_rejects_invalid_native_results(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    message: str,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            migrate_resource=lambda document: payload,
        ),
    )

    with pytest.raises(migration.NativeMigrationContractError, match=message):
        migration.migrate_document({"kind": "Application"})


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
def test_public_migration_facade_closes_failed_native_handshakes(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    message: str,
) -> None:
    native = SimpleNamespace(
        native_extension_available=lambda: False,
        native_extension_status=lambda: {"error": "binding unavailable"},
        migrate_resource=lambda document: {"document": document, "ok": True},
    )

    def fail() -> object:
        raise RuntimeError("hostile handshake")

    setattr(native, attribute, fail)
    monkeypatch.setitem(sys.modules, "graphblocks_runtime", native)

    with pytest.raises(migration.NativeMigrationUnavailableError, match=message):
        migration.migrate_document({"kind": "Application"})


def test_public_migration_facade_closes_failed_native_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(document: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("native failure")

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            migrate_resource=reject,
        ),
    )

    with pytest.raises(
        migration.NativeMigrationContractError,
        match="rejected a reference-valid document",
    ):
        migration.migrate_document({"kind": "Application"})
