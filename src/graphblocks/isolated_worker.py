from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
import math
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import time
from typing import cast

from ._canonical_reference import canonical_dumps, canonical_loads
from .worker import (
    WorkerInvokeRequest,
    WorkerInvokeResult,
    validate_worker_result,
)


DEFAULT_PROCESS_WORKER_MAX_REQUEST_BYTES = 1_048_576
DEFAULT_PROCESS_WORKER_MAX_RESULT_BYTES = 1_048_576
DEFAULT_PROCESS_WORKER_TERMINATION_GRACE_SECONDS = 1.0
MAX_PROCESS_WORKER_PAYLOAD_BYTES = 67_108_864
MAX_PROCESS_WORKER_TIMEOUT_SECONDS = 86_400.0
MAX_PROCESS_WORKER_TERMINATION_GRACE_SECONDS = 60.0
_MAX_PROCESS_WORKER_ERROR_BYTES = 512
ProcessWorkerAuthorityValidator = Callable[
    [WorkerInvokeRequest, WorkerInvokeResult],
    None,
]


class ProcessWorkerError(RuntimeError):
    """Base error for isolated worker execution failures."""


class ProcessWorkerDeadlineExceeded(ProcessWorkerError):
    def __init__(
        self,
        timeout_seconds: float,
        worker_pid: int | None,
        exitcode: int | None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.worker_pid = worker_pid
        self.exitcode = exitcode
        if worker_pid is None:
            message = (
                "isolated worker exceeded "
                f"{timeout_seconds:g} second deadline before process start"
            )
        else:
            message = (
                "isolated worker exceeded "
                f"{timeout_seconds:g} second deadline and was reaped"
            )
        super().__init__(message)


class ProcessWorkerFailed(ProcessWorkerError):
    def __init__(
        self,
        error_type: str,
        error_message: str,
        worker_pid: int,
        exitcode: int | None,
    ) -> None:
        self.error_type = error_type
        self.error_message = error_message
        self.worker_pid = worker_pid
        self.exitcode = exitcode
        super().__init__(f"isolated worker failed with {error_type}: {error_message}")


class ProcessWorkerProtocolError(ProcessWorkerError):
    """Raised when an isolated worker returns an invalid response envelope."""


class ProcessWorkerRequestTooLarge(ProcessWorkerError):
    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"isolated worker request uses {actual_bytes} bytes; maximum is {max_bytes}"
        )


class ProcessWorkerResponseTooLarge(ProcessWorkerError):
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__(
            f"isolated worker response exceeds maximum of {max_bytes} bytes"
        )


def _validate_dotted_identifier(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip() or any(
        not component.isidentifier() for component in value.split(".")
    ):
        raise ValueError(f"{owner} {field_name} must be a dotted Python identifier")
    return value


def _validate_finite_seconds(
    field_name: str,
    value: object,
    *,
    positive: bool,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"process worker {field_name} must be a finite number")
    try:
        seconds = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(
            f"process worker {field_name} must be a finite number"
        ) from error
    if not math.isfinite(seconds):
        raise ValueError(f"process worker {field_name} must be a finite number")
    if positive and seconds <= 0:
        raise ValueError(f"process worker {field_name} must be positive")
    if not positive and seconds < 0:
        raise ValueError(f"process worker {field_name} must not be negative")
    if seconds > maximum:
        raise ValueError(f"process worker {field_name} must not exceed {maximum:g}")
    return seconds


def _validate_byte_limit(field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"process worker {field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"process worker {field_name} must be positive")
    if value > MAX_PROCESS_WORKER_PAYLOAD_BYTES:
        raise ValueError(
            "process worker "
            f"{field_name} must not exceed {MAX_PROCESS_WORKER_PAYLOAD_BYTES}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProcessWorkerTarget:
    module: str
    function: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "module",
            _validate_dotted_identifier("process worker target", "module", self.module),
        )
        object.__setattr__(
            self,
            "function",
            _validate_dotted_identifier(
                "process worker target",
                "function",
                self.function,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProcessWorkerPolicy:
    timeout_seconds: float
    termination_grace_seconds: float = DEFAULT_PROCESS_WORKER_TERMINATION_GRACE_SECONDS
    max_request_bytes: int = DEFAULT_PROCESS_WORKER_MAX_REQUEST_BYTES
    max_result_bytes: int = DEFAULT_PROCESS_WORKER_MAX_RESULT_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timeout_seconds",
            _validate_finite_seconds(
                "timeout_seconds",
                self.timeout_seconds,
                positive=True,
                maximum=MAX_PROCESS_WORKER_TIMEOUT_SECONDS,
            ),
        )
        object.__setattr__(
            self,
            "termination_grace_seconds",
            _validate_finite_seconds(
                "termination_grace_seconds",
                self.termination_grace_seconds,
                positive=False,
                maximum=MAX_PROCESS_WORKER_TERMINATION_GRACE_SECONDS,
            ),
        )
        object.__setattr__(
            self,
            "max_request_bytes",
            _validate_byte_limit("max_request_bytes", self.max_request_bytes),
        )
        object.__setattr__(
            self,
            "max_result_bytes",
            _validate_byte_limit("max_result_bytes", self.max_result_bytes),
        )


def _resolve_process_worker_handler(
    target: ProcessWorkerTarget,
) -> Callable[[WorkerInvokeRequest], WorkerInvokeResult]:
    value: object = import_module(target.module)
    for component in target.function.split("."):
        value = getattr(value, component)
    if not callable(value):
        raise TypeError(
            f"isolated worker target {target.module}.{target.function} must be callable"
        )
    return cast(Callable[[WorkerInvokeRequest], WorkerInvokeResult], value)


def _bounded_error_message(error: BaseException) -> str:
    try:
        message = str(error)
    except BaseException:
        message = type(error).__name__
    encoded = message.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_PROCESS_WORKER_ERROR_BYTES:
        encoded = encoded[:_MAX_PROCESS_WORKER_ERROR_BYTES]
    return encoded.decode("utf-8", errors="ignore") or type(error).__name__


def _encode_process_worker_envelope(envelope: Mapping[str, object]) -> bytes:
    return canonical_dumps(dict(envelope)).encode("utf-8")


def _process_worker_main(
    sender: Connection,
    target: ProcessWorkerTarget,
    request_json: str,
    max_result_bytes: int,
) -> None:
    try:
        try:
            request_wire = canonical_loads(request_json)
            if not isinstance(request_wire, dict):
                raise TypeError("isolated worker request must decode to an object")
            request = WorkerInvokeRequest.from_wire(request_wire)
            handler = _resolve_process_worker_handler(target)
            result = handler(request)
            if not isinstance(result, WorkerInvokeResult):
                raise TypeError("isolated worker target must return WorkerInvokeResult")
            envelope: Mapping[str, object] = {
                "kind": "result",
                "result": result.to_wire(),
            }
        except BaseException as error:
            envelope = {
                "kind": "error",
                "errorType": type(error).__name__,
                "message": _bounded_error_message(error),
            }
        encoded = _encode_process_worker_envelope(envelope)
        if len(encoded) > max_result_bytes:
            encoded = _encode_process_worker_envelope(
                {
                    "kind": "error",
                    "errorType": "ProcessWorkerResponseTooLarge",
                    "message": (
                        "isolated worker response exceeds "
                        f"maximum of {max_result_bytes} bytes"
                    ),
                }
            )
        sender.send_bytes(encoded)
    finally:
        sender.close()


def _reap_process(process: BaseProcess, grace_seconds: float) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(grace_seconds)
    if process.is_alive():
        process.kill()
        process.join()


def _decode_process_worker_response(
    encoded: bytes,
    *,
    request: WorkerInvokeRequest,
    worker_pid: int,
    exitcode: int | None,
    max_result_bytes: int,
) -> WorkerInvokeResult:
    try:
        envelope = canonical_loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise ProcessWorkerProtocolError(
            "isolated worker response must be canonical JSON"
        ) from error
    if not isinstance(envelope, dict):
        raise ProcessWorkerProtocolError("isolated worker response must be an object")
    kind = envelope.get("kind")
    if kind == "error":
        if set(envelope) != {"kind", "errorType", "message"}:
            raise ProcessWorkerProtocolError(
                "isolated worker error response has invalid fields"
            )
        error_type = envelope.get("errorType")
        error_message = envelope.get("message")
        if not isinstance(error_type, str) or not error_type:
            raise ProcessWorkerProtocolError(
                "isolated worker error type must be a non-empty string"
            )
        if not isinstance(error_message, str) or not error_message:
            raise ProcessWorkerProtocolError(
                "isolated worker error message must be a non-empty string"
            )
        if error_type == "ProcessWorkerResponseTooLarge":
            raise ProcessWorkerResponseTooLarge(max_result_bytes)
        raise ProcessWorkerFailed(
            error_type,
            error_message,
            worker_pid,
            exitcode,
        )
    if kind != "result" or set(envelope) != {"kind", "result"}:
        raise ProcessWorkerProtocolError("isolated worker response has invalid fields")
    result_wire = envelope.get("result")
    if not isinstance(result_wire, dict):
        raise ProcessWorkerProtocolError("isolated worker result must be an object")
    try:
        result = WorkerInvokeResult.from_wire(result_wire)
    except (TypeError, ValueError) as error:
        raise ProcessWorkerProtocolError("isolated worker result is invalid") from error
    validate_worker_result(request, result)
    return result


@dataclass(frozen=True, slots=True)
class ProcessWorkerExecutor:
    target: ProcessWorkerTarget
    policy: ProcessWorkerPolicy
    authority_validator: ProcessWorkerAuthorityValidator

    def __post_init__(self) -> None:
        if not isinstance(self.target, ProcessWorkerTarget):
            raise TypeError("process worker target must be ProcessWorkerTarget")
        if not isinstance(self.policy, ProcessWorkerPolicy):
            raise TypeError("process worker policy must be ProcessWorkerPolicy")
        if not callable(self.authority_validator):
            raise TypeError("process worker authority_validator must be callable")

    def invoke(self, request: WorkerInvokeRequest) -> WorkerInvokeResult:
        if not isinstance(request, WorkerInvokeRequest):
            raise TypeError("process worker request must be WorkerInvokeRequest")
        deadline = time.monotonic() + self.policy.timeout_seconds
        request_json = canonical_dumps(request.to_wire())
        request_bytes = len(request_json.encode("utf-8"))
        if request_bytes > self.policy.max_request_bytes:
            raise ProcessWorkerRequestTooLarge(
                request_bytes,
                self.policy.max_request_bytes,
            )

        context = get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_process_worker_main,
            args=(
                sender,
                self.target,
                request_json,
                self.policy.max_result_bytes,
            ),
            daemon=True,
        )
        started = False
        try:
            if time.monotonic() >= deadline:
                raise ProcessWorkerDeadlineExceeded(
                    self.policy.timeout_seconds,
                    None,
                    None,
                )
            process.start()
            started = True
            worker_pid = process.pid
            if worker_pid is None:
                raise ProcessWorkerError("isolated worker did not receive a process id")
            sender.close()

            remaining = max(0.0, deadline - time.monotonic())
            if not receiver.poll(remaining):
                _reap_process(
                    process,
                    self.policy.termination_grace_seconds,
                )
                raise ProcessWorkerDeadlineExceeded(
                    self.policy.timeout_seconds,
                    worker_pid,
                    process.exitcode,
                )
            try:
                encoded = receiver.recv_bytes(self.policy.max_result_bytes)
            except OSError as error:
                _reap_process(
                    process,
                    self.policy.termination_grace_seconds,
                )
                raise ProcessWorkerResponseTooLarge(
                    self.policy.max_result_bytes
                ) from error
            except EOFError as error:
                _reap_process(
                    process,
                    self.policy.termination_grace_seconds,
                )
                raise ProcessWorkerFailed(
                    "WorkerExited",
                    "worker exited without a response",
                    worker_pid,
                    process.exitcode,
                ) from error

            process.join(max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                _reap_process(
                    process,
                    self.policy.termination_grace_seconds,
                )
                raise ProcessWorkerDeadlineExceeded(
                    self.policy.timeout_seconds,
                    worker_pid,
                    process.exitcode,
                )
            if time.monotonic() > deadline:
                _reap_process(
                    process,
                    self.policy.termination_grace_seconds,
                )
                raise ProcessWorkerDeadlineExceeded(
                    self.policy.timeout_seconds,
                    worker_pid,
                    process.exitcode,
                )
            if process.exitcode != 0:
                raise ProcessWorkerFailed(
                    "WorkerExited",
                    f"worker exited with status {process.exitcode}",
                    worker_pid,
                    process.exitcode,
                )
            result = _decode_process_worker_response(
                encoded,
                request=request,
                worker_pid=worker_pid,
                exitcode=process.exitcode,
                max_result_bytes=self.policy.max_result_bytes,
            )
            authority_validator = cast(
                Callable[[WorkerInvokeRequest, WorkerInvokeResult], object],
                self.authority_validator,
            )
            authority_result = authority_validator(request, result)
            if authority_result is not None:
                raise ProcessWorkerProtocolError(
                    "process worker authority_validator must return None"
                )
            if time.monotonic() > deadline:
                raise ProcessWorkerDeadlineExceeded(
                    self.policy.timeout_seconds,
                    worker_pid,
                    process.exitcode,
                )
            return result
        finally:
            receiver.close()
            sender.close()
            if started:
                if process.is_alive():
                    _reap_process(
                        process,
                        self.policy.termination_grace_seconds,
                    )
                process.close()


__all__ = [
    "DEFAULT_PROCESS_WORKER_MAX_REQUEST_BYTES",
    "DEFAULT_PROCESS_WORKER_MAX_RESULT_BYTES",
    "DEFAULT_PROCESS_WORKER_TERMINATION_GRACE_SECONDS",
    "MAX_PROCESS_WORKER_PAYLOAD_BYTES",
    "MAX_PROCESS_WORKER_TERMINATION_GRACE_SECONDS",
    "MAX_PROCESS_WORKER_TIMEOUT_SECONDS",
    "ProcessWorkerAuthorityValidator",
    "ProcessWorkerDeadlineExceeded",
    "ProcessWorkerError",
    "ProcessWorkerExecutor",
    "ProcessWorkerFailed",
    "ProcessWorkerPolicy",
    "ProcessWorkerProtocolError",
    "ProcessWorkerRequestTooLarge",
    "ProcessWorkerResponseTooLarge",
    "ProcessWorkerTarget",
]
