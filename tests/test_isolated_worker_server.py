from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from graphblocks.isolated_worker import ProcessWorkerProtocolError
from graphblocks.isolated_worker_server import (
    AcceptedRunWorkerAuthorityValidator,
)
from graphblocks.server_storage import (
    AcceptedRunClaim,
    AcceptedRunLeaseExpiredError,
    AcceptedRunPhase,
    AcceptedRunRepository,
    AcceptedRunSnapshot,
    StaleAcceptedRunClaimError,
)
from graphblocks.worker import (
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerStaleLeaseEpochError,
)


class _RunReader:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str]] = []

    def get_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> object:
        self.calls.append((tenant_id, run_id))
        return self.snapshot


def _claim(
    *,
    owner: str = "worker-1",
    generation: int = 1,
    fencing_token: int = 7,
    expires_at: int = 2_000,
) -> AcceptedRunClaim:
    return AcceptedRunClaim(
        tenant_id="tenant-1",
        run_id="run-1",
        lease_owner_id=owner,
        lease_generation=generation,
        fencing_token=fencing_token,
        lease_expires_at_unix_ms=expires_at,
    )


def _snapshot(claim: AcceptedRunClaim) -> AcceptedRunSnapshot:
    return AcceptedRunSnapshot(
        run_id=claim.run_id,
        tenant_id=claim.tenant_id,
        owner_principal_id="principal-1",
        phase=AcceptedRunPhase.RUNNING,
        state_version=2,
        event_low_watermark=1,
        event_high_watermark=2,
        claim=claim,
    )


def _request(
    *,
    run_id: str = "run-1",
    lease_epoch: int = 7,
) -> WorkerInvokeRequest:
    return WorkerInvokeRequest(
        invocation_id="invoke-1",
        run_id=run_id,
        node_id="node-1",
        node_attempt_id="attempt-1",
        lease_epoch=lease_epoch,
        block="test.block@1",
        context=WorkerInvocationContext("release-1", "revision-1"),
        inputs={},
        config={},
    )


def _result(*, lease_epoch: int = 7) -> WorkerInvokeResult:
    return WorkerInvokeResult(
        invocation_id="invoke-1",
        node_attempt_id="attempt-1",
        lease_epoch=lease_epoch,
        outputs={"value": "ok"},
    )


def _repository(reader: _RunReader) -> AcceptedRunRepository:
    return cast(AcceptedRunRepository, reader)


def test_validator_accepts_only_the_current_unexpired_claim() -> None:
    claim = _claim()
    reader = _RunReader(_snapshot(claim))
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(reader),
        claim=claim,
        clock=lambda: 1_500,
    )

    validator(_request(), _result())
    assert reader.calls == [("tenant-1", "run-1")]


def test_validator_rejects_a_replaced_claim() -> None:
    claim = _claim()
    replacement = _claim(
        owner="worker-2",
        generation=2,
        fencing_token=8,
        expires_at=3_000,
    )
    reader = _RunReader(_snapshot(replacement))
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(reader),
        claim=claim,
        clock=lambda: 1_500,
    )

    with pytest.raises(StaleAcceptedRunClaimError) as error:
        validator(_request(), _result())

    assert error.value.current == replacement
    assert error.value.provided == claim


def test_validator_rejects_a_missing_run_as_stale_authority() -> None:
    claim = _claim()
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(None)),
        claim=claim,
        clock=lambda: 1_500,
    )

    with pytest.raises(StaleAcceptedRunClaimError) as error:
        validator(_request(), _result())

    assert error.value.current is None


def test_validator_rejects_authority_at_the_lease_expiry_boundary() -> None:
    claim = _claim()
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(_snapshot(claim))),
        claim=claim,
        clock=lambda: claim.lease_expires_at_unix_ms,
    )

    with pytest.raises(AcceptedRunLeaseExpiredError) as error:
        validator(_request(), _result())

    assert error.value.claim == claim
    assert error.value.operation == "isolated worker result validation"


def test_validator_rejects_a_request_for_another_run() -> None:
    claim = _claim()
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(_snapshot(claim))),
        claim=claim,
        clock=lambda: 1_500,
    )

    with pytest.raises(ProcessWorkerProtocolError, match="run_id"):
        validator(_request(run_id="run-2"), _result())


def test_validator_maps_worker_lease_epoch_to_the_run_fencing_token() -> None:
    claim = _claim()
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(_snapshot(claim))),
        claim=claim,
        clock=lambda: 1_500,
    )

    with pytest.raises(WorkerStaleLeaseEpochError) as error:
        validator(_request(lease_epoch=6), _result(lease_epoch=6))

    assert error.value.expected == claim.fencing_token
    assert error.value.actual == 6


def test_validator_rejects_malformed_repository_results() -> None:
    claim = _claim()
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(object())),
        claim=claim,
        clock=lambda: 1_500,
    )

    with pytest.raises(ProcessWorkerProtocolError, match="repository"):
        validator(_request(), _result())


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("run_id", "run-2"),
        ("tenant_id", "tenant-2"),
    ],
)
def test_validator_rejects_a_corrupt_snapshot_identity(
    field_name: str,
    value: str,
) -> None:
    claim = _claim()
    snapshot = _snapshot(claim)
    object.__setattr__(snapshot, field_name, value)
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(snapshot)),
        claim=claim,
        clock=lambda: 1_500,
    )

    with pytest.raises(ProcessWorkerProtocolError, match="repository"):
        validator(_request(), _result())


@pytest.mark.parametrize("now", [True, -1, 1 << 63])
def test_validator_rejects_invalid_clock_values(now: object) -> None:
    claim = _claim()
    validator = AcceptedRunWorkerAuthorityValidator(
        repository=_repository(_RunReader(_snapshot(claim))),
        claim=claim,
        clock=cast("Callable[[], int]", lambda: now),
    )

    with pytest.raises((TypeError, ValueError), match="clock"):
        validator(_request(), _result())


def test_validator_rejects_invalid_dependencies() -> None:
    claim = _claim()

    with pytest.raises(TypeError, match="repository"):
        AcceptedRunWorkerAuthorityValidator(
            repository=cast("AcceptedRunRepository", object()),
            claim=claim,
            clock=lambda: 1_500,
        )
    with pytest.raises(TypeError, match="claim"):
        AcceptedRunWorkerAuthorityValidator(
            repository=_repository(_RunReader(_snapshot(claim))),
            claim=cast("AcceptedRunClaim", object()),
            clock=lambda: 1_500,
        )
    with pytest.raises(TypeError, match="clock"):
        AcceptedRunWorkerAuthorityValidator(
            repository=_repository(_RunReader(_snapshot(claim))),
            claim=claim,
            clock=cast("Callable[[], int]", object()),
        )
