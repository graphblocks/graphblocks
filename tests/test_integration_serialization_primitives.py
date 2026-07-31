from __future__ import annotations

import math

import pytest

from graphblocks.integrations import gitops, kubernetes, oci, terraform
from graphblocks.integrations._serialization import canonical_json_dumps


ADAPTER_SERIALIZERS = (
    gitops._canonical_dumps,
    kubernetes._canonical_dumps,
    oci._canonical_dumps,
    terraform._canonical_dumps,
)


@pytest.mark.parametrize("serializer", ADAPTER_SERIALIZERS)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"z": 1, "a": 2}, '{"a":2,"z":1}'),
        (
            {"unicode": "한글", "items": [True, None]},
            '{"items":[true,null],"unicode":"한글"}',
        ),
        (("tuple", 1), '["tuple",1]'),
        ({"fraction": 1.25}, '{"fraction":1.25}'),
    ],
)
def test_deployment_adapter_serializers_share_exact_json_contract(
    serializer,
    value: object,
    expected: str,
) -> None:
    assert serializer is canonical_json_dumps
    assert serializer(value) == expected


@pytest.mark.parametrize("serializer", ADAPTER_SERIALIZERS)
def test_deployment_adapter_serializers_reject_non_finite_numbers(
    serializer,
) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        serializer({"value": math.nan})


@pytest.mark.parametrize("serializer", ADAPTER_SERIALIZERS)
def test_deployment_adapter_serializers_reject_non_json_values(
    serializer,
) -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        serializer({"value": {1, 2}})
