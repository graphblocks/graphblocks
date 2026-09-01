from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from graphblocks._datetime import parse_utc_iso_datetime


def test_parse_utc_iso_datetime_accepts_canonical_forms() -> None:
    assert parse_utc_iso_datetime("2026-07-10T00:00:00Z") == datetime(
        2026, 7, 10, tzinfo=timezone.utc
    )
    assert parse_utc_iso_datetime("2026-07-10T00:00:00.250Z") == datetime(
        2026, 7, 10, 0, 0, 0, 250_000, tzinfo=timezone.utc
    )
    assert parse_utc_iso_datetime("2026-07-10T02:30:00+02:30") == datetime(
        2026, 7, 10, tzinfo=timezone.utc
    )
    assert parse_utc_iso_datetime("2026-07-09T21:30:00-02:30") == datetime(
        2026, 7, 10, tzinfo=timezone.utc
    )
    parsed = parse_utc_iso_datetime("2026-07-10T12:00:00+23:59")
    assert parsed.tzinfo == timezone.utc
    assert parsed == datetime(2026, 7, 10, 12, 0, tzinfo=timezone(timedelta(hours=23, minutes=59)))


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        " 2026-07-10T00:00:00Z",
        "2026-07-10T00:00:00Z ",
        # forms the drifted per-module parsers disagreed on
        "2026-07-10 00:00:00+00:00",  # space separator
        "2026-07-10T00:00+00:00",  # missing seconds
        "2026-07-10T00:00:00",  # naive
        "20260710T000000Z",  # basic format
        "2026-07-10T00:00:00+0000",  # offset without colon
        "2026-07-10T00:00:00+24:00",  # offset hours out of range
        "2026-07-10T00:00:00+00:60",  # offset minutes out of range
        "2026-07-10T00:00:00.Z",  # empty fraction
        "2026-07-10T00:00:00.25",  # fraction without timezone
        "2026-07-10T00:00:00.2eZ",  # non-digit fraction
        "2026-07-10",  # date only
        "2026-13-10T00:00:00Z",  # invalid month
    ],
)
def test_parse_utc_iso_datetime_rejects_non_canonical_forms(value: object) -> None:
    with pytest.raises(ValueError, match="must be an ISO datetime"):
        parse_utc_iso_datetime(value)
