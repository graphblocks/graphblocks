from __future__ import annotations

from collections.abc import Callable

import pytest

from graphblocks.budget import _loads_strict_json as load_budget_json
from graphblocks.usage import _loads_strict_json as load_usage_json


@pytest.mark.parametrize(
    ("decoder", "message"),
    (
        pytest.param(
            load_budget_json,
            "budget ledger state_json must be valid strict JSON",
            id="budget",
        ),
        pytest.param(
            load_usage_json,
            "usage ledger amounts_json must be valid strict JSON",
            id="usage",
        ),
    ),
)
def test_ledger_json_decoders_enforce_portable_nesting_limit(
    decoder: Callable[[str, str], object],
    message: str,
) -> None:
    deeply_nested = ("[" * 65) + "0" + ("]" * 65)

    with pytest.raises(ValueError, match=message):
        decoder(
            "state_json" if decoder is load_budget_json else "amounts_json",
            deeply_nested,
        )
