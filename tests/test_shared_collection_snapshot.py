from __future__ import annotations

from collections.abc import Iterator

import pytest

from graphblocks import conversation, workspace
from graphblocks._validation import snapshot_collection


COLLECTION_SNAPSHOTS = (
    conversation._snapshot_collection,
    workspace._snapshot_collection,
)


@pytest.mark.parametrize("snapshot", COLLECTION_SNAPSHOTS)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([1, 2], (1, 2)),
        ((1, 2), (1, 2)),
    ],
)
def test_shared_collection_snapshot_preserves_order(
    snapshot,
    value: object,
    expected: tuple[object, ...],
) -> None:
    assert snapshot is snapshot_collection
    assert snapshot("contract", "values", value) == expected


@pytest.mark.parametrize("snapshot", COLLECTION_SNAPSHOTS)
def test_shared_collection_snapshot_consumes_generators(snapshot) -> None:
    value = (item for item in (1, 2))

    assert snapshot("contract", "values", value) == (1, 2)


@pytest.mark.parametrize("snapshot", COLLECTION_SNAPSHOTS)
@pytest.mark.parametrize("value", ["value", b"value", bytearray(b"value"), {"key": 1}])
def test_shared_collection_snapshot_rejects_scalar_and_mapping_inputs(
    snapshot,
    value: object,
) -> None:
    with pytest.raises(ValueError) as raised:
        snapshot("contract", "values", value)

    assert str(raised.value) == "contract values must be a collection"
    assert raised.value.__cause__ is None


class _FailingIterator:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __iter__(self) -> Iterator[object]:
        raise self._error


@pytest.mark.parametrize("snapshot", COLLECTION_SNAPSHOTS)
@pytest.mark.parametrize(
    "error", [TypeError("type failure"), RuntimeError("runtime failure")]
)
def test_shared_collection_snapshot_normalizes_traversal_failures(
    snapshot,
    error: Exception,
) -> None:
    with pytest.raises(ValueError) as raised:
        snapshot("contract", "values", _FailingIterator(error))

    assert str(raised.value) == "contract values must be a collection"
    assert raised.value.__cause__ is error


@pytest.mark.parametrize("snapshot", COLLECTION_SNAPSHOTS)
def test_shared_collection_snapshot_preserves_unexpected_failures(snapshot) -> None:
    error = LookupError("unexpected")

    with pytest.raises(LookupError) as raised:
        snapshot("contract", "values", _FailingIterator(error))

    assert raised.value is error


@pytest.mark.parametrize("snapshot", COLLECTION_SNAPSHOTS)
def test_shared_collection_snapshot_is_ordered_and_shallow(snapshot) -> None:
    nested: list[object] = []
    source = [nested]

    result = snapshot("contract", "values", source)
    source.append("later")

    assert result == (nested,)
    assert result[0] is nested
