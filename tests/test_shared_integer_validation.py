from __future__ import annotations

from collections.abc import Callable

import pytest

from graphblocks._validation import validate_optional_bounded_non_negative_int
from graphblocks.integrations import pubsub, sqs


_MAX_SIGNED_64 = (1 << 63) - 1
OPTIONAL_TIMESTAMP_VALIDATORS = (
    (sqs._optional_non_negative_int, sqs.SqsAdapterError),
    (pubsub._optional_non_negative_int, pubsub.PubsubAdapterError),
)


@pytest.mark.parametrize(("validator", "error_type"), OPTIONAL_TIMESTAMP_VALIDATORS)
def test_shared_optional_integer_validator_preserves_adapter_contract(
    validator: Callable[[str, object | None], int | None],
    error_type: type[ValueError],
) -> None:
    assert validator("timestamp", None) is None
    assert validator("timestamp", 0) == 0
    assert validator("timestamp", _MAX_SIGNED_64) == _MAX_SIGNED_64

    invalid_cases = (
        (True, "timestamp must be an integer"),
        ("1", "timestamp must be an integer"),
        (-1, "timestamp must be non-negative"),
        (
            _MAX_SIGNED_64 + 1,
            "timestamp must not exceed signed 64-bit range",
        ),
    )
    for value, message in invalid_cases:
        with pytest.raises(error_type) as raised:
            validator("timestamp", value)

        assert type(raised.value) is error_type
        assert str(raised.value) == message


def test_shared_optional_integer_validator_defaults_to_value_error() -> None:
    with pytest.raises(ValueError) as raised:
        validate_optional_bounded_non_negative_int(
            "attempt",
            -1,
            maximum=10,
            range_name="test",
        )

    assert type(raised.value) is ValueError
    assert str(raised.value) == "attempt must be non-negative"
