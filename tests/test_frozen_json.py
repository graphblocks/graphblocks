"""Contract tests for the shared frozen JSON container pair."""

from __future__ import annotations

from copy import copy, deepcopy
import pickle

import pytest

from graphblocks._frozen_json import (
    FrozenJsonArray,
    FrozenJsonObject,
    freeze_json_container,
    thaw_json_container,
)


_SAMPLE = {
    "name": "run",
    "flags": [True, False, None],
    "nested": {"depth": 2, "items": [{"id": 1}, {"id": 2}]},
    "score": 0.5,
}


def test_freeze_produces_immutable_containers() -> None:
    frozen = freeze_json_container(_SAMPLE)
    assert isinstance(frozen, FrozenJsonObject)
    assert isinstance(frozen["flags"], FrozenJsonArray)
    assert isinstance(frozen["nested"], FrozenJsonObject)
    with pytest.raises(TypeError):
        frozen["name"] = "other"  # type: ignore[index]
    with pytest.raises(TypeError):
        del frozen["name"]  # type: ignore[attr-defined]


def test_frozen_array_stays_hashable_and_list_comparable() -> None:
    array = FrozenJsonArray((1, "two", None))
    assert array == [1, "two", None]
    assert array == (1, "two", None)
    assert array != [1, "two"]
    assert hash(array) == hash((1, "two", None))
    assert {array: "ok"}[array] == "ok"


def test_frozen_object_compares_by_mapping_value() -> None:
    frozen = freeze_json_container(_SAMPLE)
    assert frozen == _SAMPLE
    assert frozen != {**_SAMPLE, "name": "other"}
    assert repr(frozen).startswith("{")


def test_copy_returns_identity_and_deepcopy_detaches() -> None:
    frozen = freeze_json_container(_SAMPLE)
    assert copy(frozen) is frozen
    thawed = deepcopy(frozen)
    assert isinstance(thawed, dict)
    assert thawed == _SAMPLE
    assert isinstance(thawed["flags"], list)
    thawed["nested"]["depth"] = 3
    assert frozen["nested"]["depth"] == 2


def test_thaw_round_trips_to_plain_json() -> None:
    frozen = freeze_json_container(_SAMPLE)
    thawed = thaw_json_container(frozen)
    assert thawed == _SAMPLE
    assert type(thawed) is dict
    assert type(thawed["flags"]) is list
    assert type(thawed["nested"]) is dict


def test_pickle_round_trip_preserves_frozen_types() -> None:
    frozen = freeze_json_container(_SAMPLE)
    restored = pickle.loads(pickle.dumps(frozen))
    assert isinstance(restored, FrozenJsonObject)
    assert isinstance(restored["flags"], FrozenJsonArray)
    assert restored == _SAMPLE


def test_module_aliases_share_one_implementation() -> None:
    from graphblocks import client, webhooks, worker

    assert client._FrozenJsonObject is FrozenJsonObject
    assert client._FrozenJsonArray is FrozenJsonArray
    assert webhooks._FrozenJsonObject is FrozenJsonObject
    assert webhooks._FrozenJsonArray is FrozenJsonArray
    assert worker._FrozenWorkerJsonObject is FrozenJsonObject
    assert worker._FrozenWorkerJsonArray is FrozenJsonArray
