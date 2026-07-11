from __future__ import annotations

import pytest

from graphblocks.durable import (
    LateEventError,
    SourceCursor,
    SourceEvent,
    Watermark,
    WindowAccumulator,
    WindowPolicy,
)


def _event(offset: int, event_time_unix_ms: int) -> SourceEvent:
    return SourceEvent(
        SourceCursor("orders", 0, offset),
        {"offset": offset},
        event_time_unix_ms=event_time_unix_ms,
    )


def _offsets(pane: object) -> list[int]:
    return [event.cursor.offset for event in pane.events]


def test_accumulating_window_emits_on_time_and_final_replacement() -> None:
    windows = WindowAccumulator(
        WindowPolicy.tumbling_event_time(
            size_ms=100,
            allowed_lateness_ms=50,
            accumulation_mode="accumulating",
        )
    )
    windows.ingest(_event(900, 90))
    windows.ingest(_event(100, 10))

    on_time = windows.advance_watermark(Watermark.event_time(100))

    assert len(on_time) == 1
    assert _offsets(on_time[0]) == [100, 900]
    assert on_time[0].revision == 0
    assert not on_time[0].is_final
    assert windows.advance_watermark(Watermark.event_time(100)) == []
    assert windows.advance_watermark(Watermark.event_time(90)) == []

    windows.ingest(_event(500, 10))
    assert windows.advance_watermark(Watermark.event_time(149)) == []

    final = windows.advance_watermark(Watermark.event_time(150))

    assert len(final) == 1
    assert _offsets(final[0]) == [100, 500, 900]
    assert final[0].revision == 1
    assert final[0].is_final
    with pytest.raises(LateEventError):
        windows.ingest(_event(999, 99))


@pytest.mark.parametrize(
    ("allowed_lateness_ms", "watermark_unix_ms"),
    ((50, 150), (0, 100)),
)
def test_accumulating_window_direct_final_watermark_coalesces_panes(
    allowed_lateness_ms: int,
    watermark_unix_ms: int,
) -> None:
    windows = WindowAccumulator(
        WindowPolicy.tumbling_event_time(
            size_ms=100,
            allowed_lateness_ms=allowed_lateness_ms,
            accumulation_mode="accumulating",
        )
    )
    windows.ingest(_event(900, 90))
    windows.ingest(_event(100, 10))

    panes = windows.advance_watermark(Watermark.event_time(watermark_unix_ms))

    assert len(panes) == 1
    assert _offsets(panes[0]) == [100, 900]
    assert panes[0].revision == 0
    assert panes[0].is_final


def test_discarding_window_keeps_single_final_snapshot_and_deadline_admission() -> None:
    windows = WindowAccumulator(
        WindowPolicy.tumbling_event_time(
            size_ms=100,
            allowed_lateness_ms=50,
            accumulation_mode="discarding",
        )
    )
    windows.ingest(_event(900, 90))
    windows.ingest(_event(100, 10))

    assert windows.advance_watermark(Watermark.event_time(100)) == []
    windows.ingest(_event(500, 10))

    panes = windows.advance_watermark(Watermark.event_time(150))

    assert len(panes) == 1
    assert _offsets(panes[0]) == [100, 500, 900]
    assert panes[0].revision == 0
    assert panes[0].is_final
