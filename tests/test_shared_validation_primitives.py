from __future__ import annotations

import pytest

from graphblocks import conversation, run_store, workspace
from graphblocks._validation import (
    validate_non_empty_string,
    validate_optional_non_empty_string,
)


STRING_VALIDATORS = (
    conversation._validate_non_empty_string,
    run_store._validate_non_empty_string,
    workspace._validate_non_empty_string,
)
OPTIONAL_STRING_VALIDATORS = (
    conversation._validate_optional_non_empty_string,
    run_store._validate_optional_non_empty_string,
    workspace._validate_optional_non_empty_string,
)


@pytest.mark.parametrize("validator", STRING_VALIDATORS)
@pytest.mark.parametrize("value", ["한글-value", "inner\nnewline"])
def test_shared_string_validation_accepts_exact_unicode_scalars(
    validator,
    value: str,
) -> None:
    assert validator is validate_non_empty_string
    assert validator("contract", "field", value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (1, "must be a string"),
        ("", "must not be empty"),
        (" \t", "must not be empty"),
        (" padded", "must not contain surrounding whitespace"),
        ("\ud800", "must contain only Unicode scalar values"),
        ("\udfff", "must contain only Unicode scalar values"),
    ],
)
@pytest.mark.parametrize("validator", STRING_VALIDATORS)
def test_shared_string_validation_rejects_the_same_adversarial_corpus(
    validator,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        validator("contract", "field", value)

    assert str(raised.value) == f"contract field {message}"


@pytest.mark.parametrize("validator", OPTIONAL_STRING_VALIDATORS)
def test_shared_optional_string_validation_preserves_none(validator) -> None:
    assert validator is validate_optional_non_empty_string
    assert validator("contract", "field", None) is None
    assert validator("contract", "field", "value") == "value"
