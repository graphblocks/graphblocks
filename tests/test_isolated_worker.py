from __future__ import annotations

from multiprocessing import active_children
import os
from pathlib import Path
from textwrap import dedent
import time

import pytest

from graphblocks.isolated_worker import (
    ProcessWorkerDeadlineExceeded,
    ProcessWorkerExecutor,
    ProcessWorkerFailed,
    ProcessWorkerPolicy,
    ProcessWorkerRequestTooLarge,
    ProcessWorkerResponseTooLarge,
    ProcessWorkerTarget,
)
from graphblocks.worker import (
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerStaleLeaseEpochError,
)


def _write_handler_module(tmp_path: Path) -> str:
    module_name = "isolated_worker_fixture"
    (tmp_path / f"{module_name}.py").write_text(
        dedent(
            """
            import os
            from pathlib import Path
            import time

            from graphblocks.worker import WorkerInvokeResult


            def succeed(request):
                return WorkerInvokeResult(
                    invocation_id=request.invocation_id,
                    node_attempt_id=request.node_attempt_id,
                    lease_epoch=request.lease_epoch,
                    outputs={"pid": os.getpid(), "value": request.inputs["value"]},
                )


            def spin_forever(request):
                del request
                while True:
                    pass


            def publish_late_effect(request):
                time.sleep(request.config["delaySeconds"])
                Path(request.inputs["sentinelPath"]).write_text(
                    "late",
                    encoding="utf-8",
                )
                return WorkerInvokeResult(
                    invocation_id=request.invocation_id,
                    node_attempt_id=request.node_attempt_id,
                    lease_epoch=request.lease_epoch,
                    outputs={},
                )


            def return_stale_result(request):
                return WorkerInvokeResult(
                    invocation_id=request.invocation_id,
                    node_attempt_id=request.node_attempt_id,
                    lease_epoch=request.lease_epoch - 1,
                    outputs={"stale": True},
                )


            def fail(request):
                del request
                raise ValueError("provider failed")


            def return_large_result(request):
                return WorkerInvokeResult(
                    invocation_id=request.invocation_id,
                    node_attempt_id=request.node_attempt_id,
                    lease_epoch=request.lease_epoch,
                    outputs={"value": "x" * 10000},
                )
            """
        ),
        encoding="utf-8",
    )
    return module_name


def _request(
    *,
    inputs: dict[str, object] | None = None,
    config: dict[str, object] | None = None,
) -> WorkerInvokeRequest:
    return WorkerInvokeRequest(
        invocation_id="invoke-1",
        run_id="run-1",
        node_id="node-1",
        node_attempt_id="attempt-1",
        lease_epoch=7,
        block="test.block@1",
        context=WorkerInvocationContext("release-1", "revision-1"),
        inputs={} if inputs is None else inputs,
        config={} if config is None else config,
    )


def test_process_worker_executes_in_fresh_process_and_validates_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_handler_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget(module_name, "succeed"),
        ProcessWorkerPolicy(timeout_seconds=5),
    )

    result = executor.invoke(_request(inputs={"value": "ok"}))

    assert result.outputs["value"] == "ok"
    assert result.outputs["pid"] != os.getpid()


def test_process_worker_reaps_infinite_loop_at_hard_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_handler_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget(module_name, "spin_forever"),
        ProcessWorkerPolicy(
            timeout_seconds=0.5,
            termination_grace_seconds=0.2,
        ),
    )
    started = time.monotonic()

    with pytest.raises(ProcessWorkerDeadlineExceeded) as error:
        executor.invoke(_request())

    assert time.monotonic() - started < 3
    assert error.value.worker_pid > 0
    assert error.value.exitcode is not None
    assert all(child.pid != error.value.worker_pid for child in active_children())


def test_process_worker_cannot_publish_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_handler_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sentinel = tmp_path / "late-effect.txt"
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget(module_name, "publish_late_effect"),
        ProcessWorkerPolicy(
            timeout_seconds=0.3,
            termination_grace_seconds=0.2,
        ),
    )

    with pytest.raises(ProcessWorkerDeadlineExceeded):
        executor.invoke(
            _request(
                inputs={"sentinelPath": str(sentinel)},
                config={"delaySeconds": 1},
            )
        )

    time.sleep(1.2)
    assert not sentinel.exists()


def test_process_worker_rejects_stale_lease_result_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_handler_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget(module_name, "return_stale_result"),
        ProcessWorkerPolicy(timeout_seconds=5),
    )

    with pytest.raises(WorkerStaleLeaseEpochError) as error:
        executor.invoke(_request())

    assert error.value.expected == 7
    assert error.value.actual == 6


def test_process_worker_reports_bounded_child_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_handler_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget(module_name, "fail"),
        ProcessWorkerPolicy(timeout_seconds=5),
    )

    with pytest.raises(ProcessWorkerFailed) as error:
        executor.invoke(_request())

    assert error.value.error_type == "ValueError"
    assert error.value.error_message == "provider failed"
    assert error.value.exitcode == 0


def test_process_worker_rejects_oversized_request_before_starting() -> None:
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget("example.worker", "invoke"),
        ProcessWorkerPolicy(
            timeout_seconds=1,
            max_request_bytes=32,
        ),
    )

    with pytest.raises(ProcessWorkerRequestTooLarge):
        executor.invoke(_request(inputs={"value": "x" * 100}))


def test_process_worker_rejects_oversized_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = _write_handler_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    executor = ProcessWorkerExecutor(
        ProcessWorkerTarget(module_name, "return_large_result"),
        ProcessWorkerPolicy(
            timeout_seconds=5,
            max_result_bytes=512,
        ),
    )

    with pytest.raises(ProcessWorkerResponseTooLarge):
        executor.invoke(_request())


@pytest.mark.parametrize(
    ("policy", "message"),
    (
        ({"timeout_seconds": 0}, "timeout_seconds must be positive"),
        ({"timeout_seconds": True}, "timeout_seconds must be a finite number"),
        ({"timeout_seconds": float("nan")}, "timeout_seconds must be a finite number"),
        ({"timeout_seconds": 10**10000}, "timeout_seconds must be a finite number"),
        (
            {"timeout_seconds": 1, "termination_grace_seconds": -1},
            "termination_grace_seconds must not be negative",
        ),
        (
            {"timeout_seconds": 1, "max_request_bytes": 0},
            "max_request_bytes must be positive",
        ),
        (
            {"timeout_seconds": 1, "max_result_bytes": True},
            "max_result_bytes must be an integer",
        ),
    ),
)
def test_process_worker_policy_rejects_unbounded_values(
    policy: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessWorkerPolicy(**policy)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("module", "function", "message"),
    (
        ("", "invoke", "module must not be empty"),
        ("example-worker", "invoke", "module must be a dotted Python identifier"),
        ("example.worker", "<locals>", "function must be a dotted Python identifier"),
    ),
)
def test_process_worker_target_requires_importable_identifiers(
    module: str,
    function: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProcessWorkerTarget(module, function)
