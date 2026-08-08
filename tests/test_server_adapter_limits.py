from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
from threading import Event
from typing import Any, cast

import pytest

from graphblocks.server import GraphBlocksServerApp, ServerResponse
from graphblocks.server_adapter import ServerAdapterIngress, ServerLimits


class _BodyReader:
    def __init__(self, chunks: Iterable[bytes] = (b"",)) -> None:
        self._chunks = deque(chunks)
        self.calls: list[int] = []
        self.abort_count = 0

    def read(self, max_bytes: int) -> bytes:
        self.calls.append(max_bytes)
        return self._chunks.popleft() if self._chunks else b""

    def abort(self) -> None:
        self.abort_count += 1


@dataclass
class _HttpAdapterFixture:
    limits: ServerLimits = field(default_factory=ServerLimits)
    app: GraphBlocksServerApp = field(
        default_factory=lambda: GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
        )
    )
    clock: object | None = None

    def __post_init__(self) -> None:
        arguments: dict[str, object] = {
            "app": self.app,
            "limits": self.limits,
        }
        if self.clock is not None:
            arguments["clock"] = self.clock
        self.ingress = ServerAdapterIngress(**arguments)  # type: ignore[arg-type]

    def request(
        self,
        *,
        headers: list[tuple[str, str]] | None = None,
        reader: _BodyReader | None = None,
        tenant_id: str | None = "tenant-a",
    ) -> tuple[ServerResponse, _BodyReader]:
        body_reader = reader or _BodyReader()
        response = self.ingress.handle(
            method="GET",
            path="/health",
            headers=headers or [],
            query={},
            cookies={},
            read_body=body_reader.read,
            abort_body=body_reader.abort,
            tenant_id=tenant_id,
        )
        return response, body_reader


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values = deque(values)

    def __call__(self) -> float:
        return self._values.popleft()


def _payload(response: ServerResponse) -> dict[str, object]:
    payload = json.loads(response.body)
    assert isinstance(payload, dict)
    return payload


def test_server_limits_are_closed_positive_and_ordered() -> None:
    limits = ServerLimits()

    assert limits.max_header_count == 100
    assert limits.max_header_bytes == 32 * 1024
    assert limits.max_request_body_bytes == 1024 * 1024
    assert limits.max_concurrent_requests == 128
    assert limits.max_requests_per_tenant_window == 600
    assert limits.body_idle_timeout_seconds == 15.0
    assert limits.request_total_timeout_seconds == 60.0

    with pytest.raises(ValueError, match="max_header_count must be a positive"):
        ServerLimits(max_header_count=True)
    with pytest.raises(ValueError, match="max_header_bytes must be a positive"):
        ServerLimits(max_header_bytes=0)
    with pytest.raises(ValueError, match="finite positive seconds"):
        ServerLimits(tenant_rate_window_seconds=cast(Any, "60"))
    with pytest.raises(ValueError, match="finite positive seconds"):
        ServerLimits(body_idle_timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="must not exceed"):
        ServerLimits(
            body_idle_timeout_seconds=2,
            request_total_timeout_seconds=1,
        )


def test_adapter_ingress_rejects_invalid_dependencies() -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)

    with pytest.raises(ValueError, match="app must be"):
        ServerAdapterIngress(cast(Any, object()))
    with pytest.raises(ValueError, match="limits must be"):
        ServerAdapterIngress(app, limits=cast(Any, object()))
    with pytest.raises(ValueError, match="clock must be callable"):
        ServerAdapterIngress(app, clock=cast(Any, object()))


@pytest.mark.parametrize(
    "headers",
    (
        [("x-one", "1"), ("x-two", "2")],
        [("x-long", "a" * 128)],
    ),
    ids=("header-count", "header-bytes"),
)
def test_adapter_rejects_header_bombs_before_reading_body(
    headers: list[tuple[str, str]],
) -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(max_header_count=1, max_header_bytes=64),
    )

    response, reader = fixture.request(headers=headers)

    assert response.status_code == 431
    assert _payload(response)["errorCode"] == "server.adapter.headers_too_large"
    assert reader.calls == []
    assert reader.abort_count == 1


def test_adapter_rejects_duplicate_headers_before_normalization() -> None:
    fixture = _HttpAdapterFixture()

    response, reader = fixture.request(
        headers=[("Content-Length", "0"), ("content-length", "0")],
    )

    assert response.status_code == 400
    assert _payload(response)["errorCode"] == "server.adapter.invalid_headers"
    assert reader.calls == []
    assert reader.abort_count == 1


@pytest.mark.parametrize(
    "headers",
    (
        cast(Any, {"x-header": "value"}),
        cast(Any, [("x-header",)]),
        cast(Any, [(1, "value")]),
    ),
    ids=("mapping", "malformed-pair", "non-string"),
)
def test_adapter_rejects_nonconforming_raw_header_collections(
    headers: list[tuple[str, str]],
) -> None:
    fixture = _HttpAdapterFixture()

    response, reader = fixture.request(headers=headers)

    assert response.status_code == 400
    assert _payload(response)["errorCode"] == "server.adapter.invalid_headers"
    assert reader.calls == []
    assert reader.abort_count == 1


@pytest.mark.parametrize(
    "headers",
    (
        [("content-length", "1"), ("transfer-encoding", "chunked")],
        [("content-length", "١")],
    ),
    ids=("ambiguous-framing", "non-ascii-content-length"),
)
def test_adapter_rejects_ambiguous_framing_before_reading_body(
    headers: list[tuple[str, str]],
) -> None:
    fixture = _HttpAdapterFixture()

    response, reader = fixture.request(headers=headers)

    assert response.status_code == 400
    assert _payload(response)["errorCode"] == "server.adapter.invalid_framing"
    assert reader.calls == []
    assert reader.abort_count == 1


def test_adapter_rejects_declared_oversized_body_before_reading() -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(max_request_body_bytes=4),
    )

    response, reader = fixture.request(headers=[("content-length", "5")])

    assert response.status_code == 413
    assert _payload(response) == {
        "ok": False,
        "errorCode": "server.adapter.body_too_large",
        "message": "The request body exceeds the server adapter limit.",
        "bodySizeBytes": 5,
        "maxBodyBytes": 4,
    }
    assert reader.calls == []
    assert reader.abort_count == 1


def test_adapter_rejects_content_length_mismatch_after_bounded_read() -> None:
    fixture = _HttpAdapterFixture()
    reader = _BodyReader((b"x", b""))

    response, reader = fixture.request(
        headers=[("content-length", "2")],
        reader=reader,
    )

    assert response.status_code == 400
    assert _payload(response)["errorCode"] == "server.request.invalid_framing"
    assert reader.abort_count == 1


def test_adapter_caps_unbounded_chunked_body_during_streaming() -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(max_request_body_bytes=5),
    )
    reader = _BodyReader((b"abc", b"def", b""))

    response, reader = fixture.request(
        headers=[("transfer-encoding", "chunked")],
        reader=reader,
    )

    assert response.status_code == 413
    assert _payload(response)["bodySizeBytesAtLeast"] == 6
    assert reader.calls == [6, 3]
    assert reader.abort_count == 1


def test_adapter_enforces_body_idle_deadline() -> None:
    clock = _ManualClock()
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            body_idle_timeout_seconds=1,
            request_total_timeout_seconds=10,
        ),
        clock=clock,
    )
    reader = _BodyReader((b"{}",))

    def slow_read(max_bytes: int) -> bytes:
        clock.now += 1.1
        return reader.read(max_bytes)

    response = fixture.ingress.handle(
        method="GET",
        path="/health",
        headers=[("transfer-encoding", "chunked")],
        query={},
        cookies={},
        read_body=slow_read,
        abort_body=reader.abort,
        tenant_id="tenant-a",
    )

    assert response.status_code == 408
    assert _payload(response)["errorCode"] == ("server.adapter.idle_deadline_exceeded")
    assert reader.abort_count == 1


def test_adapter_enforces_cumulative_request_deadline() -> None:
    clock = _ManualClock()
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            body_idle_timeout_seconds=2,
            request_total_timeout_seconds=3,
        ),
        clock=clock,
    )
    reader = _BodyReader((b"a", b"b", b"c", b""))

    def cumulative_read(max_bytes: int) -> bytes:
        clock.now += 1.1
        return reader.read(max_bytes)

    response = fixture.ingress.handle(
        method="GET",
        path="/health",
        headers=[("transfer-encoding", "chunked")],
        query={},
        cookies={},
        read_body=cumulative_read,
        abort_body=reader.abort,
        tenant_id="tenant-a",
    )

    assert response.status_code == 408
    assert _payload(response)["errorCode"] == ("server.adapter.total_deadline_exceeded")
    assert reader.abort_count == 1


@pytest.mark.parametrize(
    "clock, expected_error_code",
    (
        (
            _SequenceClock((0.0, 4.0)),
            "server.adapter.total_deadline_exceeded",
        ),
        (
            _SequenceClock((0.0, 2.0)),
            "server.adapter.idle_deadline_exceeded",
        ),
    ),
)
def test_adapter_rejects_deadline_already_exhausted_before_read(
    clock: _SequenceClock,
    expected_error_code: str,
) -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            body_idle_timeout_seconds=1,
            request_total_timeout_seconds=3,
        ),
        clock=clock,
    )

    response, reader = fixture.request()

    assert response.status_code == 408
    assert _payload(response)["errorCode"] == expected_error_code
    assert reader.calls == []
    assert reader.abort_count == 1


def test_adapter_detects_total_deadline_after_handler_completion() -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            body_idle_timeout_seconds=2,
            request_total_timeout_seconds=3,
        ),
        clock=_SequenceClock((0.0, 0.0, 0.0, 4.0)),
    )

    response, reader = fixture.request()

    assert response.status_code == 504
    assert _payload(response)["errorCode"] == ("server.adapter.total_deadline_exceeded")
    assert reader.abort_count == 0


def test_adapter_maps_invalid_stream_reader_output_without_leaking_details() -> None:
    fixture = _HttpAdapterFixture()
    reader = _BodyReader()

    response = fixture.ingress.handle(
        method="GET",
        path="/health",
        headers=[],
        query={},
        cookies={},
        read_body=cast(Any, lambda _max_bytes: bytearray()),
        abort_body=reader.abort,
        tenant_id="tenant-a",
    )

    assert response.status_code == 400
    assert _payload(response)["errorCode"] == "server.adapter.invalid_request"
    assert reader.abort_count == 1


def test_adapter_enforces_concurrency_for_the_entire_request() -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            max_concurrent_requests=1,
            body_idle_timeout_seconds=10,
            request_total_timeout_seconds=20,
        ),
    )
    first_read_started = Event()
    release_first_read = Event()
    first_reader = _BodyReader()

    def blocking_read(max_bytes: int) -> bytes:
        first_read_started.set()
        assert release_first_read.wait(timeout=5)
        return first_reader.read(max_bytes)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            fixture.ingress.handle,
            method="GET",
            path="/health",
            headers=[],
            query={},
            cookies={},
            read_body=blocking_read,
            abort_body=first_reader.abort,
            tenant_id="tenant-a",
        )
        assert first_read_started.wait(timeout=5)
        second, second_reader = fixture.request(tenant_id="tenant-b")
        release_first_read.set()
        first_response = first.result(timeout=5)

    assert fixture.ingress.active_requests == 0
    assert first_response.status_code == 200
    assert second.status_code == 503
    assert _payload(second)["errorCode"] == ("server.adapter.concurrency_exhausted")
    assert second.headers["retry-after"] == "1"
    assert second_reader.calls == []
    assert second_reader.abort_count == 1


def test_adapter_rate_limit_is_tenant_scoped_and_windowed() -> None:
    clock = _ManualClock()
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            max_requests_per_tenant_window=2,
            tenant_rate_window_seconds=60,
        ),
        clock=clock,
    )

    assert fixture.request(tenant_id="tenant-a")[0].status_code == 200
    assert fixture.request(tenant_id="tenant-a")[0].status_code == 200
    limited, limited_reader = fixture.request(tenant_id="tenant-a")
    other_tenant, _ = fixture.request(tenant_id="tenant-b")
    clock.now = 60.1
    after_window, _ = fixture.request(tenant_id="tenant-a")

    assert limited.status_code == 429
    assert _payload(limited)["errorCode"] == ("server.adapter.tenant_rate_exceeded")
    assert limited.headers["retry-after"] == "60"
    assert limited_reader.calls == []
    assert limited_reader.abort_count == 1
    assert other_tenant.status_code == 200
    assert after_window.status_code == 200
    assert fixture.ingress.retained_tenant_rate_buckets == 2


def test_adapter_discards_expired_rate_buckets_before_capacity_rejection() -> None:
    clock = _ManualClock()
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(
            max_tenant_rate_buckets=1,
            tenant_rate_window_seconds=1,
        ),
        clock=clock,
    )

    assert fixture.request(tenant_id="tenant-a")[0].status_code == 200
    clock.now = 1.1
    response, _ = fixture.request(tenant_id="tenant-b")

    assert response.status_code == 200
    assert fixture.ingress.retained_tenant_rate_buckets == 1


def test_adapter_bounds_tenant_rate_bucket_cardinality() -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(max_tenant_rate_buckets=1),
    )

    assert fixture.request(tenant_id="tenant-a")[0].status_code == 200
    response, reader = fixture.request(tenant_id="tenant-b")

    assert response.status_code == 503
    assert _payload(response)["errorCode"] == ("server.adapter.rate_capacity_exhausted")
    assert fixture.ingress.retained_tenant_rate_buckets == 1
    assert reader.calls == []
    assert reader.abort_count == 1


@pytest.mark.parametrize(
    "tenant_id",
    (
        cast(Any, 1),
        " tenant-a",
        "cafe\u0301",
        "tenant-é",
        "a" * 4097,
    ),
    ids=("non-string", "whitespace", "non-nfc", "non-ascii", "oversized"),
)
def test_adapter_rejects_untrusted_tenant_rate_keys(tenant_id: str) -> None:
    fixture = _HttpAdapterFixture()

    response, reader = fixture.request(tenant_id=tenant_id)

    assert response.status_code == 400
    assert _payload(response)["errorCode"] == ("server.adapter.invalid_request_head")
    assert reader.calls == []
    assert reader.abort_count == 1


def test_adapter_and_app_body_limits_are_defense_in_depth() -> None:
    fixture = _HttpAdapterFixture(
        limits=ServerLimits(max_request_body_bytes=10),
        app=GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
            max_request_body_bytes=2,
        ),
    )
    reader = _BodyReader((b"abc", b""))

    response, reader = fixture.request(reader=reader)

    assert response.status_code == 413
    assert _payload(response)["error"] == "request body exceeds max body bytes"
    assert reader.abort_count == 1


def test_server_adapter_public_exports_are_resolvable() -> None:
    import graphblocks.server_adapter as adapter_module

    assert set(adapter_module.__all__) <= set(vars(adapter_module))
