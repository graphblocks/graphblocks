from __future__ import annotations

from decimal import Decimal
import importlib


def test_canonical_numeric_tokens_do_not_rescan_output_per_value(monkeypatch) -> None:
    canonical = importlib.import_module("graphblocks.canonical")
    original_dumps = canonical.json.dumps

    class TrackingString(str):
        replace_calls = 0

        def replace(
            self,
            old: str,
            new: str,
            count: int = -1,
        ) -> TrackingString:
            type(self).replace_calls += 1
            return type(self)(super().replace(old, new, count))

    def tracking_dumps(*args: object, **kwargs: object) -> TrackingString:
        return TrackingString(original_dumps(*args, **kwargs))

    monkeypatch.setattr(canonical.json, "dumps", tracking_dumps)
    values = [Decimal("1.25") for _ in range(128)]
    values.extend(10**400 + index for index in range(128))

    encoded = canonical.canonical_dumps(values)

    assert encoded.startswith("[1.25,1.25,")
    assert TrackingString.replace_calls == 0


def test_canonical_numeric_tokens_preserve_placeholder_shaped_strings() -> None:
    canonical = importlib.import_module("graphblocks.canonical")
    decimal_placeholder = "\x00graphblocks-decimal-0\x00"
    integer_placeholder = "\x00graphblocks-integer-1\x00"
    value = {
        "decimal": Decimal("1.25"),
        "integer": 10**400,
        "literalDecimal": decimal_placeholder,
        "literalInteger": integer_placeholder,
        "embedded": f'before "{decimal_placeholder}" after',
    }

    encoded = canonical.canonical_dumps(value)

    assert canonical.canonical_loads(encoded) == value
