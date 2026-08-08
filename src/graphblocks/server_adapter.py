from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import math
from threading import Lock
from time import monotonic
import unicodedata

from .server import GraphBlocksServerApp, ServerRequestHead, ServerResponse


DEFAULT_MAX_SERVER_HEADER_COUNT = 100
DEFAULT_MAX_SERVER_HEADER_BYTES = 32 * 1024
DEFAULT_MAX_SERVER_ADAPTER_BODY_BYTES = 1024 * 1024
DEFAULT_MAX_SERVER_CONCURRENT_REQUESTS = 128
DEFAULT_MAX_SERVER_REQUESTS_PER_TENANT_WINDOW = 600
DEFAULT_SERVER_TENANT_RATE_WINDOW_SECONDS = 60.0
DEFAULT_MAX_SERVER_TENANT_RATE_BUCKETS = 10_000
DEFAULT_SERVER_BODY_IDLE_TIMEOUT_SECONDS = 15.0
DEFAULT_SERVER_REQUEST_TOTAL_TIMEOUT_SECONDS = 60.0
MAX_SERVER_ADAPTER_TENANT_ID_BYTES = 4_096


@dataclass(frozen=True, slots=True)
class ServerLimits:
    """Resource contract that every network-facing server adapter must enforce."""

    max_header_count: int = DEFAULT_MAX_SERVER_HEADER_COUNT
    max_header_bytes: int = DEFAULT_MAX_SERVER_HEADER_BYTES
    max_request_body_bytes: int = DEFAULT_MAX_SERVER_ADAPTER_BODY_BYTES
    max_concurrent_requests: int = DEFAULT_MAX_SERVER_CONCURRENT_REQUESTS
    max_requests_per_tenant_window: int = DEFAULT_MAX_SERVER_REQUESTS_PER_TENANT_WINDOW
    tenant_rate_window_seconds: float = DEFAULT_SERVER_TENANT_RATE_WINDOW_SECONDS
    max_tenant_rate_buckets: int = DEFAULT_MAX_SERVER_TENANT_RATE_BUCKETS
    body_idle_timeout_seconds: float = DEFAULT_SERVER_BODY_IDLE_TIMEOUT_SECONDS
    request_total_timeout_seconds: float = DEFAULT_SERVER_REQUEST_TOTAL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        owner = "server limits"
        for field_name in (
            "max_header_count",
            "max_header_bytes",
            "max_request_body_bytes",
            "max_concurrent_requests",
            "max_requests_per_tenant_window",
            "max_tenant_rate_buckets",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{owner} {field_name} must be a positive integer")
            object.__setattr__(
                self,
                field_name,
                value,
            )
        for field_name in (
            "tenant_rate_window_seconds",
            "body_idle_timeout_seconds",
            "request_total_timeout_seconds",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"{owner} {field_name} must be finite positive seconds"
                )
            seconds = float(value)
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(
                    f"{owner} {field_name} must be finite positive seconds"
                )
            object.__setattr__(
                self,
                field_name,
                seconds,
            )
        if self.body_idle_timeout_seconds > self.request_total_timeout_seconds:
            raise ValueError(
                "server limits body_idle_timeout_seconds must not exceed "
                "request_total_timeout_seconds"
            )


class _ServerAdapterBodyTooLargeError(ValueError):
    def __init__(self, body_size_bytes: int) -> None:
        super().__init__("server adapter request body exceeds max body bytes")
        self.body_size_bytes = body_size_bytes


class _ServerAdapterDeadlineExceededError(TimeoutError):
    def __init__(self, deadline_kind: str) -> None:
        super().__init__(f"server adapter {deadline_kind} deadline exceeded")
        self.deadline_kind = deadline_kind


@dataclass(slots=True)
class ServerAdapterIngress:
    """Reference ingress boundary for framework and HTTP-server adapters.

    Raw header pairs are accepted deliberately: adapters must apply limits and
    reject ambiguous duplicates before normalizing them into a mapping.
    """

    app: GraphBlocksServerApp
    limits: ServerLimits = field(default_factory=ServerLimits)
    clock: Callable[[], float] = field(default=monotonic, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _active_requests: int = field(default=0, init=False, repr=False)
    _rate_events_by_tenant: dict[str, deque[float]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.app, GraphBlocksServerApp):
            raise ValueError("server adapter ingress app must be GraphBlocksServerApp")
        if not isinstance(self.limits, ServerLimits):
            raise ValueError("server adapter ingress limits must be ServerLimits")
        if not callable(self.clock):
            raise ValueError("server adapter ingress clock must be callable")

    @property
    def active_requests(self) -> int:
        with self._lock:
            return self._active_requests

    @property
    def retained_tenant_rate_buckets(self) -> int:
        with self._lock:
            return len(self._rate_events_by_tenant)

    def _response(
        self,
        status_code: int,
        error_code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ServerResponse:
        payload: dict[str, object] = {
            "ok": False,
            "errorCode": error_code,
            "message": message,
        }
        if details is not None:
            payload.update(details)
        return ServerResponse.json(status_code, payload, headers=headers)

    def _header_limit_response(self) -> ServerResponse:
        return self._response(
            431,
            "server.adapter.headers_too_large",
            "The request headers exceed the server adapter limit.",
            details={
                "maxHeaderCount": self.limits.max_header_count,
                "maxHeaderBytes": self.limits.max_header_bytes,
            },
        )

    def _body_limit_response(
        self,
        body_size_bytes: int,
        *,
        exact_size: bool,
    ) -> ServerResponse:
        size_key = "bodySizeBytes" if exact_size else "bodySizeBytesAtLeast"
        return self._response(
            413,
            "server.adapter.body_too_large",
            "The request body exceeds the server adapter limit.",
            details={
                size_key: body_size_bytes,
                "maxBodyBytes": self.limits.max_request_body_bytes,
            },
        )

    def _normalize_headers(
        self,
        headers: Sequence[tuple[str, str]],
    ) -> dict[str, str] | None:
        if isinstance(headers, (str, bytes, bytearray)) or not isinstance(
            headers,
            Sequence,
        ):
            raise ValueError(
                "server adapter headers must be a sequence of name/value pairs"
            )
        if len(headers) > self.limits.max_header_count:
            return None
        normalized: dict[str, str] = {}
        retained_bytes = 0
        for entry in headers:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise ValueError("server adapter headers must contain name/value pairs")
            name, value = entry
            if type(name) is not str or type(value) is not str:
                raise ValueError(
                    "server adapter header names and values must be exact strings"
                )
            normalized_name = name.lower()
            if normalized_name in normalized:
                raise ValueError(
                    "server adapter headers must not contain duplicate names"
                )
            retained_bytes += len(name.encode("utf-8"))
            retained_bytes += len(value.encode("utf-8")) + 4
            if retained_bytes > self.limits.max_header_bytes:
                return None
            normalized[normalized_name] = value
        return normalized

    def _tenant_rate_key(self, tenant_id: str | None) -> str:
        if tenant_id is None:
            return "<unauthenticated>"
        if type(tenant_id) is not str:
            raise ValueError("server adapter tenant_id must be an exact string or null")
        if tenant_id != tenant_id.strip() or not tenant_id:
            raise ValueError(
                "server adapter tenant_id must be non-empty without surrounding whitespace"
            )
        if unicodedata.normalize("NFC", tenant_id) != tenant_id:
            raise ValueError("server adapter tenant_id must use NFC normalization")
        if len(tenant_id.encode("utf-8")) > MAX_SERVER_ADAPTER_TENANT_ID_BYTES:
            raise ValueError("server adapter tenant_id exceeds its byte limit")
        if any(
            not character.isascii() or not 0x21 <= ord(character) <= 0x7E
            for character in tenant_id
        ):
            raise ValueError(
                "server adapter tenant_id must contain only printable ASCII"
            )
        return tenant_id

    def _discard_expired_rate_buckets(self, window_start: float) -> None:
        for tenant_key, events in tuple(self._rate_events_by_tenant.items()):
            while events and events[0] <= window_start:
                events.popleft()
            if not events:
                self._rate_events_by_tenant.pop(tenant_key, None)

    def _admit(self, tenant_key: str, now: float) -> tuple[str, float] | None:
        window_start = now - self.limits.tenant_rate_window_seconds
        with self._lock:
            if self._active_requests >= self.limits.max_concurrent_requests:
                return ("concurrency", 1.0)
            events = self._rate_events_by_tenant.get(tenant_key)
            if events is None:
                if (
                    len(self._rate_events_by_tenant)
                    >= self.limits.max_tenant_rate_buckets
                ):
                    self._discard_expired_rate_buckets(window_start)
                if (
                    len(self._rate_events_by_tenant)
                    >= self.limits.max_tenant_rate_buckets
                ):
                    return ("rate_capacity", self.limits.tenant_rate_window_seconds)
                events = deque()
                self._rate_events_by_tenant[tenant_key] = events
            while events and events[0] <= window_start:
                events.popleft()
            if len(events) >= self.limits.max_requests_per_tenant_window:
                retry_after = max(
                    0.001,
                    events[0] + self.limits.tenant_rate_window_seconds - now,
                )
                return ("rate", retry_after)
            events.append(now)
            self._active_requests += 1
            return None

    def _release(self) -> None:
        with self._lock:
            if self._active_requests < 1:
                raise RuntimeError("server adapter active request count underflow")
            self._active_requests -= 1

    def handle(
        self,
        *,
        method: str,
        path: str,
        headers: Sequence[tuple[str, str]],
        query: Mapping[str, str],
        cookies: Mapping[str, str],
        read_body: Callable[[int], bytes],
        abort_body: Callable[[], None],
        tenant_id: str | None = None,
        requested_at: str = "",
    ) -> ServerResponse:
        """Apply adapter limits before delegating to the reference server app."""

        if not callable(read_body):
            raise ValueError("server adapter body reader must be callable")
        if not callable(abort_body):
            raise ValueError("server adapter abort callback must be callable")
        try:
            normalized_headers = self._normalize_headers(headers)
        except (UnicodeError, ValueError):
            abort_body()
            return self._response(
                400,
                "server.adapter.invalid_headers",
                "The request headers are invalid.",
            )
        if normalized_headers is None:
            abort_body()
            return self._header_limit_response()

        content_length = normalized_headers.get("content-length")
        if content_length is not None and "transfer-encoding" in normalized_headers:
            abort_body()
            return self._response(
                400,
                "server.adapter.invalid_framing",
                "The request body framing is invalid.",
            )
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdecimal():
                abort_body()
                return self._response(
                    400,
                    "server.adapter.invalid_framing",
                    "The request body framing is invalid.",
                )
            declared_body_bytes = int(content_length)
            if declared_body_bytes > self.limits.max_request_body_bytes:
                abort_body()
                return self._body_limit_response(
                    declared_body_bytes,
                    exact_size=True,
                )

        try:
            request_head = ServerRequestHead(
                method=method,
                path=path,
                headers=normalized_headers,
                query=query,
                cookies=cookies,
                requested_at=requested_at,
            )
            tenant_key = self._tenant_rate_key(tenant_id)
        except (TypeError, ValueError):
            abort_body()
            return self._response(
                400,
                "server.adapter.invalid_request_head",
                "The request metadata is invalid.",
            )

        started_at = self.clock()
        if not math.isfinite(started_at):
            raise ValueError("server adapter clock must return a finite number")
        admission_error = self._admit(tenant_key, started_at)
        if admission_error is not None:
            abort_body()
            kind, retry_after = admission_error
            retry_after_header = str(max(1, math.ceil(retry_after)))
            if kind == "concurrency":
                return self._response(
                    503,
                    "server.adapter.concurrency_exhausted",
                    "The server adapter concurrency limit is exhausted.",
                    headers={"retry-after": retry_after_header},
                )
            if kind == "rate_capacity":
                return self._response(
                    503,
                    "server.adapter.rate_capacity_exhausted",
                    "The server adapter rate-limit capacity is exhausted.",
                    headers={"retry-after": retry_after_header},
                )
            return self._response(
                429,
                "server.adapter.tenant_rate_exceeded",
                "The tenant request rate limit is exceeded.",
                headers={"retry-after": retry_after_header},
            )

        last_progress_at = started_at
        body_size_bytes = 0

        def read_with_limits(max_bytes: int) -> bytes:
            nonlocal body_size_bytes, last_progress_at
            before_read = self.clock()
            if not math.isfinite(before_read):
                raise ValueError("server adapter clock must return a finite number")
            if before_read - started_at >= self.limits.request_total_timeout_seconds:
                raise _ServerAdapterDeadlineExceededError("total")
            if before_read - last_progress_at >= self.limits.body_idle_timeout_seconds:
                raise _ServerAdapterDeadlineExceededError("idle")
            remaining_probe_bytes = (
                self.limits.max_request_body_bytes - body_size_bytes + 1
            )
            chunk = read_body(min(max_bytes, remaining_probe_bytes))
            after_read = self.clock()
            if not math.isfinite(after_read) or after_read < before_read:
                raise ValueError(
                    "server adapter clock must be finite and non-decreasing"
                )
            if after_read - started_at > self.limits.request_total_timeout_seconds:
                raise _ServerAdapterDeadlineExceededError("total")
            if after_read - before_read > self.limits.body_idle_timeout_seconds:
                raise _ServerAdapterDeadlineExceededError("idle")
            if type(chunk) is bytes:
                body_size_bytes += len(chunk)
                if body_size_bytes > self.limits.max_request_body_bytes:
                    raise _ServerAdapterBodyTooLargeError(body_size_bytes)
                if chunk:
                    last_progress_at = after_read
            return chunk

        try:
            response = self.app.handle_stream(
                request_head,
                read_with_limits,
                abort_body=abort_body,
            )
            completed_at = self.clock()
            if not math.isfinite(completed_at) or completed_at < started_at:
                raise ValueError(
                    "server adapter clock must be finite and non-decreasing"
                )
            if completed_at - started_at > self.limits.request_total_timeout_seconds:
                return self._response(
                    504,
                    "server.adapter.total_deadline_exceeded",
                    "The request exceeded the server adapter total deadline.",
                )
            return response
        except _ServerAdapterBodyTooLargeError as error:
            return self._body_limit_response(
                error.body_size_bytes,
                exact_size=False,
            )
        except _ServerAdapterDeadlineExceededError as error:
            return self._response(
                408,
                f"server.adapter.{error.deadline_kind}_deadline_exceeded",
                "The request body exceeded the server adapter deadline.",
            )
        except ValueError:
            return self._response(
                400,
                "server.adapter.invalid_request",
                "The request is invalid.",
            )
        finally:
            self._release()


__all__ = [
    "DEFAULT_MAX_SERVER_ADAPTER_BODY_BYTES",
    "DEFAULT_MAX_SERVER_CONCURRENT_REQUESTS",
    "DEFAULT_MAX_SERVER_HEADER_BYTES",
    "DEFAULT_MAX_SERVER_HEADER_COUNT",
    "DEFAULT_MAX_SERVER_REQUESTS_PER_TENANT_WINDOW",
    "DEFAULT_MAX_SERVER_TENANT_RATE_BUCKETS",
    "DEFAULT_SERVER_BODY_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_SERVER_REQUEST_TOTAL_TIMEOUT_SECONDS",
    "DEFAULT_SERVER_TENANT_RATE_WINDOW_SECONDS",
    "ServerAdapterIngress",
    "ServerLimits",
]
