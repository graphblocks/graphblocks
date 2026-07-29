from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .isolated_worker import ProcessWorkerProtocolError
from .server_storage import (
    AcceptedRunClaim,
    AcceptedRunLeaseExpiredError,
    AcceptedRunRepository,
    AcceptedRunSnapshot,
    assert_current_claim,
)
from .worker import (
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerStaleLeaseEpochError,
    validate_worker_result,
)

_MAX_UNIX_MILLISECONDS = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class AcceptedRunWorkerAuthorityValidator:
    """Validate a worker result against one live durable accepted-run claim."""

    repository: AcceptedRunRepository
    claim: AcceptedRunClaim
    clock: Callable[[], int]

    def __post_init__(self) -> None:
        if not callable(getattr(self.repository, "get_run", None)):
            raise TypeError(
                "accepted run worker authority repository must provide get_run"
            )
        if not isinstance(self.claim, AcceptedRunClaim):
            raise TypeError(
                "accepted run worker authority claim must be an AcceptedRunClaim"
            )
        if not callable(self.clock):
            raise TypeError("accepted run worker authority clock must be callable")

    def __call__(
        self,
        request: WorkerInvokeRequest,
        result: WorkerInvokeResult,
    ) -> None:
        validate_worker_result(request, result)
        if request.run_id != self.claim.run_id:
            raise ProcessWorkerProtocolError(
                "isolated worker request run_id does not match accepted run claim"
            )
        if request.lease_epoch != self.claim.fencing_token:
            raise WorkerStaleLeaseEpochError(
                self.claim.fencing_token,
                request.lease_epoch,
            )

        snapshot = self.repository.get_run(
            tenant_id=self.claim.tenant_id,
            run_id=self.claim.run_id,
        )
        if snapshot is not None and not isinstance(snapshot, AcceptedRunSnapshot):
            raise ProcessWorkerProtocolError(
                "accepted run repository returned an invalid snapshot"
            )
        if snapshot is not None and (
            snapshot.tenant_id != self.claim.tenant_id
            or snapshot.run_id != self.claim.run_id
        ):
            raise ProcessWorkerProtocolError(
                "accepted run repository returned a snapshot for another resource"
            )
        assert_current_claim(
            current=None if snapshot is None else snapshot.claim,
            provided=self.claim,
        )

        now_unix_ms = self.clock()
        if type(now_unix_ms) is not int:
            raise TypeError(
                "accepted run worker authority clock must return an integer"
            )
        if not 0 <= now_unix_ms <= _MAX_UNIX_MILLISECONDS:
            raise ValueError(
                "accepted run worker authority clock value is out of range"
            )
        if now_unix_ms >= self.claim.lease_expires_at_unix_ms:
            raise AcceptedRunLeaseExpiredError(
                self.claim,
                "isolated worker result validation",
            )


__all__ = ["AcceptedRunWorkerAuthorityValidator"]
