from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, Thread

import pytest

from graphblocks.policy import PrincipalRef
from graphblocks.runtime import CancellationToken, RuntimeRegistry
from graphblocks.server import (
    GraphBlocksServerApp,
    ServerAuthDecision,
    ServerAuthRequest,
    ServerRequest,
    ServerResponse,
)


def _background_request(run_id: str) -> ServerRequest:
    return ServerRequest(
        method="POST",
        path="/runs",
        headers={},
        query={},
        cookies={},
        body=json.dumps(
            {
                "graph": {
                    "apiVersion": "graphblocks.ai/v1alpha3",
                    "kind": "Graph",
                    "metadata": {"name": f"lifecycle-{run_id}"},
                    "spec": {
                        "nodes": {
                            "wait": {
                                "block": "test.lifecycle@1",
                                "outputs": {"value": "$output.value"},
                            }
                        }
                    },
                },
                "runId": run_id,
                "responseMode": "background",
                "occurredAt": "2026-08-08T00:00:00Z",
            }
        ).encode("utf-8"),
    )


def _app(
    executor: ThreadPoolExecutor,
    block: Callable[
        [dict[str, object], dict[str, object], dict[str, object]],
        dict[str, object],
    ],
    *,
    owns_executor: bool = False,
) -> GraphBlocksServerApp:
    registry = RuntimeRegistry(allow_untyped=True)
    registry.register("test.lifecycle@1", block)
    return GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        registry=registry,
        defer_accepted_runs=True,
        allow_process_local_accepted_runs_dev=True,
        accepted_run_executor=executor,
        owns_accepted_run_executor=owns_executor,
    )


def test_server_lifecycle_gracefully_drains_and_preserves_external_executor() -> None:
    started = Event()
    release = Event()

    def wait_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=5)
        return {"value": "done"}

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        app = _app(executor, wait_block)
        assert app.start() is app
        assert app.lifecycle_state == "running"
        assert (
            app.handle(_background_request("run-lifecycle-graceful")).status_code == 202
        )
        assert started.wait(timeout=5)

        assert app.drain(timeout=0) is False
        assert app.lifecycle_state == "draining"
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            app.start()
        rejected = app.handle(_background_request("run-after-drain"))
        assert rejected.status_code == 503
        assert json.loads(rejected.body.decode("utf-8")) == {
            "ok": False,
            "errorCode": "server.lifecycle.unavailable",
            "message": "The server is not accepting new requests.",
            "lifecycleState": "draining",
        }
        with pytest.raises(RuntimeError, match="not accepting accepted-run work"):
            app.advance_accepted_run("run-after-drain")

        release.set()
        assert app.drain(timeout=5) is True
        assert app.close(timeout=0) is True
        assert app.close(timeout=0) is True
        assert app.drain(timeout=0) is True
        assert app.lifecycle_state == "closed"
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            app.start()

        assert executor.submit(lambda: "external-still-open").result(timeout=5) == (
            "external-still-open"
        )
        health = app.handle(
            ServerRequest(
                method="GET",
                path="/health",
                headers={},
                query={},
                cookies={},
            )
        )
        assert health.status_code == 200
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_server_close_timeout_force_cancels_process_local_run() -> None:
    started = Event()
    cancellation_seen = Event()

    def cancellation_aware_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        token = context["cancellation_token"]
        assert isinstance(token, CancellationToken)
        started.set()
        while not token.cancelled:
            cancellation_seen.wait(timeout=0.01)
        cancellation_seen.set()
        return {"value": "cancelled"}

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        app = _app(executor, cancellation_aware_block)
        assert (
            app.handle(_background_request("run-lifecycle-forced")).status_code == 202
        )
        assert started.wait(timeout=5)

        assert app.close(timeout=0) is False
        assert app.close(timeout=5) is False
        assert app.lifecycle_state == "closed"
        assert cancellation_seen.wait(timeout=5)
        completion = app.wait_for_accepted_run(
            "run-lifecycle-forced",
            timeout=5,
        )
        assert completion["status"] == "cancelled"
        assert executor.submit(lambda: "external-still-open").result(timeout=5) == (
            "external-still-open"
        )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_server_context_manager_closes_only_owned_executor() -> None:
    class RecordingThreadPoolExecutor(ThreadPoolExecutor):
        shutdown_calls = 0

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_calls += 1
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def immediate_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        return {"value": "done"}

    executor = RecordingThreadPoolExecutor(max_workers=1)
    app = _app(executor, immediate_block, owns_executor=True)

    with app as entered:
        assert entered is app
        assert app.lifecycle_state == "running"

    assert app.lifecycle_state == "closed"
    assert executor.shutdown_calls == 1
    assert app.close() is True
    assert executor.shutdown_calls == 1
    with pytest.raises(RuntimeError, match="cannot schedule new futures"):
        executor.submit(lambda: None)


def test_server_drain_without_deadline_waits_for_existing_work() -> None:
    started = Event()
    release = Event()
    drained = Event()

    def wait_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=5)
        return {"value": "done"}

    executor = ThreadPoolExecutor(max_workers=1)
    app = _app(executor, wait_block)
    drain_result: list[bool] = []
    drain_thread = Thread(
        target=lambda: (drain_result.append(app.drain()), drained.set()),
    )
    try:
        assert app.handle(_background_request("run-no-deadline")).status_code == 202
        assert started.wait(timeout=5)
        drain_thread.start()
        assert not drained.wait(timeout=0.05)
        release.set()
        assert drained.wait(timeout=5)
        drain_thread.join(timeout=5)
        assert drain_result == [True]
    finally:
        release.set()
        drain_thread.join(timeout=5)
        app.close(timeout=5)
        executor.shutdown(wait=True, cancel_futures=True)


def test_server_drain_allows_already_admitted_request_to_schedule_work() -> None:
    authentication_started = Event()
    release_authentication = Event()
    execution_started = Event()
    release_execution = Event()

    class BlockingAuthHook:
        def authorize(self, request: ServerAuthRequest) -> ServerAuthDecision:
            authentication_started.set()
            assert release_authentication.wait(timeout=5)
            return ServerAuthDecision(
                True,
                principal=PrincipalRef("lifecycle-user"),
            )

    def wait_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        execution_started.set()
        assert release_execution.wait(timeout=5)
        return {"value": "done"}

    executor = ThreadPoolExecutor(max_workers=1)
    app = _app(executor, wait_block)
    app.auth_hook = BlockingAuthHook()
    responses: list[ServerResponse] = []
    request_thread = Thread(
        target=lambda: responses.append(
            app.handle(_background_request("run-admitted-before-drain"))
        ),
    )
    try:
        request_thread.start()
        assert authentication_started.wait(timeout=5)
        assert app.drain(timeout=0) is False
        release_authentication.set()
        request_thread.join(timeout=5)
        assert [response.status_code for response in responses] == [202]
        assert execution_started.wait(timeout=5)
        assert app.drain(timeout=0) is False
        release_execution.set()
        assert app.drain(timeout=5) is True
    finally:
        release_authentication.set()
        release_execution.set()
        request_thread.join(timeout=5)
        app.close(timeout=5)
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize(
    "timeout",
    (
        True,
        -1,
        float("nan"),
        float("inf"),
        pytest.param(10**10_000, id="float-overflow"),
    ),
)
def test_server_lifecycle_rejects_invalid_timeouts(timeout: object) -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)

    with pytest.raises(
        ValueError,
        match="server lifecycle timeout must be a finite non-negative number",
    ):
        app.drain(timeout=timeout)
    assert app.lifecycle_state == "running"


def test_server_lifecycle_rejects_invalid_executor_ownership_flag() -> None:
    with pytest.raises(
        ValueError,
        match="server owns_accepted_run_executor must be a boolean",
    ):
        GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
            owns_accepted_run_executor=1,  # type: ignore[arg-type]
        )


def test_server_lifecycle_defensive_invariants_fail_closed() -> None:
    app = GraphBlocksServerApp(allow_unauthenticated_dev=True)

    with pytest.raises(RuntimeError, match="counter underflow"):
        app._finish_server_request()
    with pytest.raises(RuntimeError, match="executor is unavailable"):
        app._submit_accepted_run_task(lambda: None)
    with pytest.raises(TypeError, match="lifecycle admission must be a boolean"):
        app.advance_accepted_run(
            "invalid-lifecycle-admission",
            _lifecycle_admitted=1,  # type: ignore[arg-type]
        )

    assert app.drain(timeout=0) is True
    with pytest.raises(RuntimeError, match="not accepting accepted-run work"):
        app._submit_accepted_run_task(lambda: None)


def test_server_executor_ownership_requires_an_executor() -> None:
    with pytest.raises(
        ValueError,
        match="server-owned accepted run executor requires accepted_run_executor",
    ):
        GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
            owns_accepted_run_executor=True,
        )
