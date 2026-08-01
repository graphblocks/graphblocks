from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol
from urllib.parse import quote

from .canonical import canonical_dumps, canonical_hash, canonical_loads
from .compiler import Plan, compile_graph, compile_graph_reference
from .durable_worker import (
    DEFAULT_DURABLE_WORKER_TARGET,
    DurableWorkerOutcome,
    build_durable_worker_request,
    decode_durable_worker_result,
)
from .durable_registry import (
    durable_intent_registry,
    is_durable_worker_compatible_registry,
)
from .isolated_worker import (
    ProcessWorkerExecutor,
    ProcessWorkerPolicy,
    ProcessWorkerRequestTooLarge,
    ProcessWorkerResponseTooLarge,
    ProcessWorkerTarget,
)
from .isolated_worker_server import AcceptedRunWorkerAuthorityValidator
from .plugins import BlockCatalog
from .runtime import (
    RuntimeRegistry,
)
from .server import (
    ServerAuthDecision,
    ServerAuthHook,
    ServerAuthRequest,
    ServerEndpoint,
    ServerRequest,
    ServerRequestHead,
    ServerResponse,
    ServerRouteManifest,
    ServerRouteNotFoundError,
    _read_bounded_server_request_body,
    _ServerRequestBodyLengthMismatchError,
    _ServerRequestBodyTooLargeError,
)
from .server_storage import (
    CHECKPOINT_FORMAT_VERSION,
    AcceptedRunAdmission,
    AcceptedRunCancelCommand,
    AcceptedRunCallbackCommit,
    AcceptedRunClaimRequest,
    AcceptedRunConflictError,
    AcceptedRunControlAcceptance,
    AcceptedRunControlAction,
    AcceptedRunEffectIntent,
    AcceptedRunEffectKind,
    AcceptedRunEventIntent,
    AcceptedRunEventPage,
    AcceptedRunExpireCommand,
    AcceptedRunIdConflictError,
    AcceptedRunLeaseExpiredError,
    AcceptedRunNotFoundError,
    AcceptedRunQueueClaimRequest,
    AcceptedRunRepository,
    AcceptedRunSnapshot,
    AcceptedRunStateControlCommand,
    AcceptedRunStorageError,
    AcceptedRunTerminalCommit,
    AcceptedRunWaitingCommit,
    AcceptedRunWorkItem,
    AdmissionIdentity,
    AdmissionResult,
    accepted_run_system_clock,
    CallbackAcceptance,
    CallbackIssuanceIdentity,
    CallbackSubmissionIdentity,
    encode_runtime_checkpoint,
    assert_current_claim,
)
from .worker import WorkerInvokeRequest, WorkerInvokeResult


DURABLE_GRAPH_FORMAT_VERSION = "graphblocks.ai/Graph@v1"
DURABLE_RUNTIME_FORMAT_VERSION = "graphblocks.runtime@v1"
_MAX_SQLITE_UNIX_MS = (1 << 63) - 1
DEFAULT_DURABLE_SERVER_MAX_REQUEST_BODY_BYTES = 1_048_576
DEFAULT_DURABLE_WORKER_MAX_REQUEST_BYTES = 4_194_304
DEFAULT_DURABLE_WORKER_PUBLICATION_MARGIN_SECONDS = 0.1
DEFAULT_DURABLE_WORKER_TIMEOUT_SECONDS = 25.0


class DurableAcceptedRunIntegrityError(AcceptedRunStorageError):
    """Raised when durable execution material cannot be reconstructed."""


class DurableGraphCompiler(Protocol):
    """One compiler authority selected for the durable service lifetime."""

    def __call__(
        self,
        document: dict[str, object],
        block_catalog: BlockCatalog | None = None,
        *,
        allow_unknown_blocks: bool = False,
    ) -> Plan: ...


@dataclass(slots=True)
class DurableAcceptedRunService:
    """Preview service whose accepted-run authority is exclusively durable."""

    repository: AcceptedRunRepository
    lease_owner_id: str
    lease_duration_ms: int = 30_000
    registry: RuntimeRegistry = field(default_factory=durable_intent_registry)
    compiler: DurableGraphCompiler = field(
        default=compile_graph,
        repr=False,
    )
    clock: Callable[[], int] = field(
        default=accepted_run_system_clock,
        repr=False,
    )
    worker_target: ProcessWorkerTarget = DEFAULT_DURABLE_WORKER_TARGET
    worker_policy: ProcessWorkerPolicy | None = None
    allow_unsafe_custom_worker_dev: bool = field(default=False, repr=False)
    _unsafe_custom_worker_dev_enabled: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    _configured_worker_target: ProcessWorkerTarget | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _default_worker_registry: RuntimeRegistry | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _default_worker_handlers: tuple[tuple[str, object], ...] = field(
        default=(),
        init=False,
        repr=False,
    )
    _default_worker_block_catalog: BlockCatalog | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _default_worker_allow_untyped: bool | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.allow_unsafe_custom_worker_dev) is not bool:
            raise ValueError(
                "durable accepted-run service "
                "allow_unsafe_custom_worker_dev must be a boolean"
            )
        if type(self.worker_target) is not ProcessWorkerTarget:
            raise ValueError(
                "durable accepted-run service worker_target must be an exact "
                "ProcessWorkerTarget"
            )
        self._unsafe_custom_worker_dev_enabled = self.allow_unsafe_custom_worker_dev
        self._configured_worker_target = self.worker_target
        required_repository_operations = (
            "accept_run",
            "get_run",
            "read_events",
            "get_checkpoint",
            "claim_run",
            "claim_work",
            "claim_next_work",
            "commit_waiting",
            "accept_callback_and_queue_resume",
            "cancel_run",
            "expire_run",
            "pause_run",
            "resume_run",
            "commit_terminal",
        )
        if any(
            not callable(getattr(self.repository, operation, None))
            for operation in required_repository_operations
        ):
            raise ValueError(
                "durable accepted-run service repository must implement the "
                "complete accepted-run authority"
            )
        if (
            type(self.lease_owner_id) is not str
            or not self.lease_owner_id
            or self.lease_owner_id != self.lease_owner_id.strip()
        ):
            raise ValueError(
                "durable accepted-run service lease_owner_id must be an "
                "exact non-empty string"
            )
        if (
            isinstance(self.lease_duration_ms, bool)
            or not isinstance(self.lease_duration_ms, int)
            or self.lease_duration_ms < 1
            or self.lease_duration_ms > _MAX_SQLITE_UNIX_MS
        ):
            raise ValueError(
                "durable accepted-run service lease_duration_ms must be a "
                "positive SQLite integer"
            )
        worker_policy = self.worker_policy
        if worker_policy is None:
            lease_seconds = self.lease_duration_ms / 1_000
            available_seconds = (
                lease_seconds - DEFAULT_DURABLE_WORKER_PUBLICATION_MARGIN_SECONDS
            )
            if available_seconds <= 0:
                raise ValueError(
                    "durable accepted-run lease must leave time for worker "
                    "termination and fenced publication"
                )
            worker_policy = ProcessWorkerPolicy(
                timeout_seconds=min(
                    DEFAULT_DURABLE_WORKER_TIMEOUT_SECONDS,
                    available_seconds * 0.85,
                ),
                termination_grace_seconds=min(
                    1.0,
                    available_seconds * 0.1,
                ),
                max_request_bytes=DEFAULT_DURABLE_WORKER_MAX_REQUEST_BYTES,
            )
            self.worker_policy = worker_policy
        self._validated_worker_policy()

    def _validate_clock_authority(self) -> None:
        if not callable(self.clock):
            raise ValueError("durable accepted-run service clock must be callable")
        repository_clock = getattr(
            self.repository,
            "transaction_clock",
            None,
        )
        if repository_clock is None:
            return
        if not callable(repository_clock):
            raise ValueError(
                "durable accepted-run repository transaction_clock must be callable"
            )
        if repository_clock is not self.clock:
            raise ValueError(
                "durable accepted-run service and repository must share "
                "the exact clock authority"
            )

    def _validated_worker_policy(
        self,
    ) -> tuple[ProcessWorkerTarget, ProcessWorkerPolicy]:
        self._validate_clock_authority()
        if type(self.allow_unsafe_custom_worker_dev) is not bool:
            raise ValueError(
                "durable accepted-run service "
                "allow_unsafe_custom_worker_dev must be a boolean"
            )
        if (
            self.allow_unsafe_custom_worker_dev
            is not self._unsafe_custom_worker_dev_enabled
        ):
            raise ValueError(
                "durable accepted-run service unsafe custom worker "
                "configuration changed after construction"
            )
        worker_target = self.worker_target
        configured_worker_target = self._configured_worker_target
        if type(worker_target) is not ProcessWorkerTarget:
            raise ValueError(
                "durable accepted-run service worker_target must be an exact "
                "ProcessWorkerTarget"
            )
        if type(configured_worker_target) is not ProcessWorkerTarget:
            raise DurableAcceptedRunIntegrityError(
                "durable accepted-run service configured worker target is invalid"
            )
        if (
            worker_target.module != configured_worker_target.module
            or worker_target.function != configured_worker_target.function
        ):
            raise ValueError(
                "durable accepted-run service worker_target changed after construction"
            )
        default_worker_target = (
            configured_worker_target.module == DEFAULT_DURABLE_WORKER_TARGET.module
            and configured_worker_target.function
            == DEFAULT_DURABLE_WORKER_TARGET.function
        )
        if not default_worker_target and not self._unsafe_custom_worker_dev_enabled:
            raise ValueError(
                "durable accepted-run service custom worker_target requires "
                "explicit allow_unsafe_custom_worker_dev=True"
            )
        worker_policy = self.worker_policy
        if not isinstance(worker_policy, ProcessWorkerPolicy):
            raise ValueError(
                "durable accepted-run service worker_policy must be a "
                "ProcessWorkerPolicy"
            )
        if (
            worker_policy.timeout_seconds
            + worker_policy.termination_grace_seconds
            + DEFAULT_DURABLE_WORKER_PUBLICATION_MARGIN_SECONDS
            >= self.lease_duration_ms / 1_000
        ):
            raise ValueError(
                "durable accepted-run worker deadline, termination grace, "
                "and publication margin must fit inside the run lease"
            )
        if not isinstance(self.registry, RuntimeRegistry):
            raise ValueError(
                "durable accepted-run service registry must be a RuntimeRegistry"
            )
        if not callable(self.compiler):
            raise ValueError("durable accepted-run service compiler must be callable")
        if default_worker_target:
            if (
                self.compiler is not compile_graph
                and self.compiler is not compile_graph_reference
            ):
                raise ValueError(
                    "durable accepted-run service custom compiler requires "
                    "a matching custom process worker target"
                )
            if not is_durable_worker_compatible_registry(self.registry):
                raise ValueError(
                    "durable accepted-run service custom registry is not "
                    "compatible with the default durable worker; use "
                    "durable_intent_registry() or a matching unsafe custom "
                    "process worker target"
                )
            current_handlers = tuple(sorted(self.registry.blocks.items()))
            if self._default_worker_registry is None:
                self._default_worker_registry = self.registry
                self._default_worker_handlers = current_handlers
                self._default_worker_block_catalog = self.registry.block_catalog
                self._default_worker_allow_untyped = self.registry.allow_untyped
            elif (
                self.registry is not self._default_worker_registry
                or self.registry.block_catalog is not self._default_worker_block_catalog
                or self.registry.allow_untyped != self._default_worker_allow_untyped
                or len(current_handlers) != len(self._default_worker_handlers)
                or any(
                    current_block_id != expected_block_id
                    or current_handler is not expected_handler
                    for (
                        current_block_id,
                        current_handler,
                    ), (
                        expected_block_id,
                        expected_handler,
                    ) in zip(
                        current_handlers,
                        self._default_worker_handlers,
                        strict=True,
                    )
                )
            ):
                raise ValueError(
                    "durable accepted-run service default worker registry "
                    "changed after configuration"
                )
        return configured_worker_target, worker_policy

    def _now_unix_ms(self) -> int:
        self._validate_clock_authority()
        now_unix_ms = self.clock()
        if (
            isinstance(now_unix_ms, bool)
            or not isinstance(now_unix_ms, int)
            or now_unix_ms < 0
            or now_unix_ms > _MAX_SQLITE_UNIX_MS
        ):
            raise ValueError(
                "durable accepted-run service clock must return a "
                "non-negative SQLite integer"
            )
        return now_unix_ms

    def admit_run(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        idempotency_key: str,
        graph: Mapping[str, object],
        inputs: Mapping[str, object],
        invocation: Mapping[str, object],
        created_at_unix_ms: int | None = None,
    ) -> AdmissionResult:
        self._validated_worker_policy()
        for field_name, value in (
            ("graph", graph),
            ("inputs", inputs),
            ("invocation", invocation),
        ):
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"durable accepted-run admission {field_name} must be a mapping"
                )
        graph_json = canonical_dumps(dict(graph))
        inputs_json = canonical_dumps(dict(inputs))
        invocation_json = canonical_dumps(dict(invocation))
        decoded_graph = canonical_loads(graph_json)
        decoded_inputs = canonical_loads(inputs_json)
        decoded_invocation = canonical_loads(invocation_json)
        if (
            not isinstance(decoded_graph, dict)
            or not isinstance(decoded_inputs, dict)
            or not isinstance(decoded_invocation, dict)
        ):
            raise ValueError(
                "durable accepted-run admission values must encode JSON objects"
            )
        block_catalog = self.registry.compilation_catalog()
        plan = self.compiler(
            decoded_graph,
            block_catalog=block_catalog,
            allow_unknown_blocks=self.registry.allow_untyped,
        )
        plan_errors = [
            diagnostic
            for diagnostic in plan.diagnostics.diagnostics
            if diagnostic.severity == "error"
        ]
        if plan_errors:
            raise ValueError(
                "; ".join(
                    f"{diagnostic.code} {diagnostic.path}: {diagnostic.message}"
                    for diagnostic in plan_errors
                )
            )
        normalized_graph_json = canonical_dumps(plan.normalized)
        if canonical_hash(canonical_loads(normalized_graph_json)) != plan.graph_hash:
            raise DurableAcceptedRunIntegrityError(
                "durable accepted-run compiler plan hash does not match its "
                "normalized graph"
            )
        created_at = (
            self._now_unix_ms() if created_at_unix_ms is None else created_at_unix_ms
        )
        request_value = {
            "graph": decoded_graph,
            "inputs": decoded_inputs,
            "invocation": decoded_invocation,
            "ownerPrincipalId": owner_principal_id,
            "runId": run_id,
            "tenantId": tenant_id,
        }
        request_digest = canonical_hash(request_value)
        ticket_value = {
            "requestDigest": request_digest,
            "runId": run_id,
            "state": "accepted",
        }
        accepted_event_value = {
            "runId": run_id,
            "tenantId": tenant_id,
            "state": "ready_initial",
        }
        return self.repository.accept_run(
            AcceptedRunAdmission(
                run_id=run_id,
                identity=AdmissionIdentity(
                    tenant_id=tenant_id,
                    owner_principal_id=owner_principal_id,
                    admission_scope="POST:/runs",
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                ),
                graph_json=normalized_graph_json,
                graph_hash=plan.graph_hash,
                inputs_json=inputs_json,
                invocation_json=invocation_json,
                ticket_json=canonical_dumps(ticket_value),
                graph_format_version=DURABLE_GRAPH_FORMAT_VERSION,
                runtime_format_version=DURABLE_RUNTIME_FORMAT_VERSION,
                checkpoint_format_version=CHECKPOINT_FORMAT_VERSION,
                created_at_unix_ms=created_at,
                accepted_event=AcceptedRunEventIntent(
                    kind="run_accepted",
                    payload_json=canonical_dumps(accepted_event_value),
                    payload_digest=canonical_hash(accepted_event_value),
                    created_at_unix_ms=created_at,
                ),
            )
        )

    def get_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> AcceptedRunSnapshot | None:
        return self.repository.get_run(
            tenant_id=tenant_id,
            run_id=run_id,
        )

    def read_events(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int,
        limit: int,
    ) -> AcceptedRunEventPage:
        return self.repository.read_events(
            tenant_id=tenant_id,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def accept_callback(
        self,
        command: AcceptedRunCallbackCommit,
    ) -> CallbackAcceptance:
        return self.repository.accept_callback_and_queue_resume(command)

    def submit_callback(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        operation_id: str,
        expected_state_version: int,
        checkpoint_digest: str,
        operation_attempt_id: str,
        callback_idempotency_key: str,
        lease_generation: int,
        fencing_token: int,
        payload: Mapping[str, object],
        receipt: Mapping[str, object],
        received_at_unix_ms: int | None = None,
    ) -> CallbackAcceptance:
        if not isinstance(payload, Mapping):
            raise ValueError("durable accepted-run callback payload must be a mapping")
        if not isinstance(receipt, Mapping):
            raise ValueError("durable accepted-run callback receipt must be a mapping")
        payload_json = canonical_dumps(dict(payload))
        receipt_json = canonical_dumps(dict(receipt))
        decoded_payload = canonical_loads(payload_json)
        decoded_receipt = canonical_loads(receipt_json)
        if not isinstance(decoded_payload, dict):
            raise ValueError(
                "durable accepted-run callback payload must encode a JSON object"
            )
        if not isinstance(decoded_receipt, dict):
            raise ValueError(
                "durable accepted-run callback receipt must encode a JSON object"
            )
        received_at = (
            self._now_unix_ms() if received_at_unix_ms is None else received_at_unix_ms
        )
        issuance = CallbackIssuanceIdentity(
            run_id=run_id,
            checkpoint_digest=checkpoint_digest,
            operation_id=operation_id,
            operation_attempt_id=operation_attempt_id,
            callback_idempotency_key=callback_idempotency_key,
            lease_generation=lease_generation,
            fencing_token=fencing_token,
        )
        event_value = {
            "checkpointDigest": checkpoint_digest,
            "resumeState": "ready_resume",
            "runId": run_id,
        }
        return self.accept_callback(
            AcceptedRunCallbackCommit(
                tenant_id=tenant_id,
                owner_principal_id=owner_principal_id,
                expected_state_version=expected_state_version,
                submission=CallbackSubmissionIdentity(
                    issuance=issuance,
                    payload_digest=canonical_hash(decoded_payload),
                ),
                payload_json=payload_json,
                receipt_json=receipt_json,
                received_at_unix_ms=received_at,
                accepted_event=AcceptedRunEventIntent(
                    kind="external_callback_received",
                    payload_json=canonical_dumps(event_value),
                    payload_digest=canonical_hash(event_value),
                    created_at_unix_ms=received_at,
                ),
            )
        )

    def cancel_run(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        expected_state_version: int,
        idempotency_key: str,
        reason: str,
        requested_at_unix_ms: int | None = None,
    ) -> AcceptedRunControlAcceptance:
        return self._apply_terminal_control(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            run_id=run_id,
            action=AcceptedRunControlAction.CANCEL,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            reason=reason,
            requested_at_unix_ms=requested_at_unix_ms,
        )

    def expire_run(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        expected_state_version: int,
        idempotency_key: str,
        reason: str,
        requested_at_unix_ms: int | None = None,
    ) -> AcceptedRunControlAcceptance:
        return self._apply_terminal_control(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            run_id=run_id,
            action=AcceptedRunControlAction.EXPIRE,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            reason=reason,
            requested_at_unix_ms=requested_at_unix_ms,
        )

    def _apply_terminal_control(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        action: AcceptedRunControlAction,
        expected_state_version: int,
        idempotency_key: str,
        reason: str,
        requested_at_unix_ms: int | None,
    ) -> AcceptedRunControlAcceptance:
        if action not in {
            AcceptedRunControlAction.CANCEL,
            AcceptedRunControlAction.EXPIRE,
        }:
            raise ValueError(
                "durable accepted-run terminal control action must be cancel or expire"
            )
        control_name = {
            AcceptedRunControlAction.CANCEL: "cancellation",
            AcceptedRunControlAction.EXPIRE: "expiration",
        }[action]
        if type(reason) is not str or not reason or reason != reason.strip():
            raise ValueError(
                f"durable accepted-run {control_name} reason must be an exact "
                "non-empty string"
            )
        requested_at = (
            self._now_unix_ms()
            if requested_at_unix_ms is None
            else requested_at_unix_ms
        )
        terminal_status = {
            AcceptedRunControlAction.CANCEL: "cancelled",
            AcceptedRunControlAction.EXPIRE: "expired",
        }[action]
        request_value = {
            "action": action.value,
            "expectedStateVersion": expected_state_version,
            "ownerPrincipalId": owner_principal_id,
            "reason": reason,
            "runId": run_id,
            "tenantId": tenant_id,
        }
        request_digest = canonical_hash(request_value)
        result_value = {
            "reason": reason,
            "requestId": idempotency_key,
            "status": terminal_status,
        }
        result_digest = canonical_hash(result_value)
        event_value = {
            "reason": reason,
            "requestId": idempotency_key,
            "runId": run_id,
            "state": terminal_status,
        }
        completion_value = {
            "result": result_value,
            "resultDigest": result_digest,
            "runId": run_id,
            "tenantId": tenant_id,
        }
        completion_digest = canonical_hash(completion_value)
        completion_identity_digest = canonical_hash(
            {
                "runId": run_id,
                "tenantId": tenant_id,
            }
        )
        completion_effect = AcceptedRunEffectIntent(
            effect_id=(
                f"effect-completion:{completion_digest.removeprefix('sha256:')}"
            ),
            kind=AcceptedRunEffectKind.COMPLETION,
            idempotency_key=(
                f"completion:{completion_identity_digest.removeprefix('sha256:')}"
            ),
            payload_json=canonical_dumps(completion_value),
            payload_digest=completion_digest,
        )
        if action is AcceptedRunControlAction.CANCEL:
            return self.repository.cancel_run(
                AcceptedRunCancelCommand(
                    tenant_id=tenant_id,
                    owner_principal_id=owner_principal_id,
                    run_id=run_id,
                    expected_state_version=expected_state_version,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    requested_at_unix_ms=requested_at,
                    result_json=canonical_dumps(result_value),
                    result_digest=result_digest,
                    cancelled_event=AcceptedRunEventIntent(
                        kind="run_cancelled",
                        payload_json=canonical_dumps(event_value),
                        payload_digest=canonical_hash(event_value),
                        created_at_unix_ms=requested_at,
                    ),
                    completion_effect=completion_effect,
                )
            )
        return self.repository.expire_run(
            AcceptedRunExpireCommand(
                tenant_id=tenant_id,
                owner_principal_id=owner_principal_id,
                run_id=run_id,
                expected_state_version=expected_state_version,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                requested_at_unix_ms=requested_at,
                result_json=canonical_dumps(result_value),
                result_digest=result_digest,
                expired_event=AcceptedRunEventIntent(
                    kind="run_expired",
                    payload_json=canonical_dumps(event_value),
                    payload_digest=canonical_hash(event_value),
                    created_at_unix_ms=requested_at,
                ),
                completion_effect=completion_effect,
            )
        )

    def pause_run(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        expected_state_version: int,
        idempotency_key: str,
        reason: str,
        requested_at_unix_ms: int | None = None,
    ) -> AcceptedRunControlAcceptance:
        return self._apply_state_control(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            run_id=run_id,
            action=AcceptedRunControlAction.PAUSE,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            reason=reason,
            requested_at_unix_ms=requested_at_unix_ms,
        )

    def resume_run(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        expected_state_version: int,
        idempotency_key: str,
        reason: str,
        requested_at_unix_ms: int | None = None,
    ) -> AcceptedRunControlAcceptance:
        return self._apply_state_control(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            run_id=run_id,
            action=AcceptedRunControlAction.RESUME,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            reason=reason,
            requested_at_unix_ms=requested_at_unix_ms,
        )

    def _apply_state_control(
        self,
        *,
        tenant_id: str,
        owner_principal_id: str,
        run_id: str,
        action: AcceptedRunControlAction,
        expected_state_version: int,
        idempotency_key: str,
        reason: str,
        requested_at_unix_ms: int | None,
    ) -> AcceptedRunControlAcceptance:
        if action not in {
            AcceptedRunControlAction.PAUSE,
            AcceptedRunControlAction.RESUME,
        }:
            raise ValueError(
                "durable accepted-run state control action must be pause or resume"
            )
        if type(reason) is not str or not reason or reason != reason.strip():
            raise ValueError(
                "durable accepted-run state control reason must be an exact "
                "non-empty string"
            )
        requested_at = (
            self._now_unix_ms()
            if requested_at_unix_ms is None
            else requested_at_unix_ms
        )
        request_value = {
            "action": action.value,
            "expectedStateVersion": expected_state_version,
            "ownerPrincipalId": owner_principal_id,
            "reason": reason,
            "runId": run_id,
            "tenantId": tenant_id,
        }
        event_value = {
            "action": action.value,
            "reason": reason,
            "requestId": idempotency_key,
            "runId": run_id,
        }
        command = AcceptedRunStateControlCommand(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            run_id=run_id,
            action=action,
            expected_state_version=expected_state_version,
            idempotency_key=idempotency_key,
            request_digest=canonical_hash(request_value),
            requested_at_unix_ms=requested_at,
            control_event=AcceptedRunEventIntent(
                kind={
                    AcceptedRunControlAction.PAUSE: "run_paused",
                    AcceptedRunControlAction.RESUME: "run_resumed",
                }[action],
                payload_json=canonical_dumps(event_value),
                payload_digest=canonical_hash(event_value),
                created_at_unix_ms=requested_at,
            ),
        )
        if action is AcceptedRunControlAction.PAUSE:
            return self.repository.pause_run(command)
        return self.repository.resume_run(command)

    def advance_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> AcceptedRunSnapshot:
        worker_target, worker_policy = self._validated_worker_policy()
        claimed_at_unix_ms = self._now_unix_ms()
        work = self.repository.claim_work(
            AcceptedRunClaimRequest(
                tenant_id=tenant_id,
                run_id=run_id,
                lease_owner_id=self.lease_owner_id,
                now_unix_ms=claimed_at_unix_ms,
                lease_duration_ms=self.lease_duration_ms,
            )
        )
        if work is None:
            snapshot = self.repository.get_run(
                tenant_id=tenant_id,
                run_id=run_id,
            )
            if snapshot is None:
                raise AcceptedRunNotFoundError(tenant_id, run_id)
            return snapshot
        return self._advance_work(work, worker_target, worker_policy)

    def advance_next_run(
        self,
        *,
        tenant_id: str | None = None,
    ) -> AcceptedRunSnapshot | None:
        worker_target, worker_policy = self._validated_worker_policy()
        claimed_at_unix_ms = self._now_unix_ms()
        work = self.repository.claim_next_work(
            AcceptedRunQueueClaimRequest(
                tenant_id=tenant_id,
                lease_owner_id=self.lease_owner_id,
                now_unix_ms=claimed_at_unix_ms,
                lease_duration_ms=self.lease_duration_ms,
            )
        )
        if work is None:
            return None
        return self._advance_work(work, worker_target, worker_policy)

    def _advance_work(
        self,
        work: AcceptedRunWorkItem,
        worker_target: ProcessWorkerTarget,
        worker_policy: ProcessWorkerPolicy,
    ) -> AcceptedRunSnapshot:
        graph = canonical_loads(work.envelope.graph_json)
        inputs = canonical_loads(work.envelope.inputs_json)
        invocation = canonical_loads(work.envelope.invocation_json)
        if (
            not isinstance(graph, dict)
            or not isinstance(inputs, dict)
            or not isinstance(invocation, dict)
        ):
            raise DurableAcceptedRunIntegrityError(
                "durable accepted-run execution envelope must contain JSON objects"
            )
        plan = self.compiler(
            graph,
            block_catalog=self.registry.compilation_catalog(),
            allow_unknown_blocks=self.registry.allow_untyped,
        )
        if (
            any(
                diagnostic.severity == "error"
                for diagnostic in plan.diagnostics.diagnostics
            )
            or plan.graph_hash != work.envelope.graph_hash
        ):
            raise DurableAcceptedRunIntegrityError(
                "durable accepted-run graph does not match its admitted plan"
            )

        worker_request = build_durable_worker_request(
            work,
            graph=graph,
            inputs=inputs,
        )
        current_snapshot = self.repository.get_run(
            tenant_id=work.claim.tenant_id,
            run_id=work.claim.run_id,
        )
        if current_snapshot is not None and (
            current_snapshot.owner_principal_id
            != work.envelope.identity.owner_principal_id
            or current_snapshot.state_version != work.state_version
        ):
            raise DurableAcceptedRunIntegrityError(
                "durable accepted-run worker authority changed before execution"
            )
        assert_current_claim(
            current=(None if current_snapshot is None else current_snapshot.claim),
            provided=work.claim,
        )
        worker_started_at_unix_ms = self._now_unix_ms()
        remaining_lease_seconds = (
            work.claim.lease_expires_at_unix_ms - worker_started_at_unix_ms
        ) / 1_000
        max_execution_seconds = (
            remaining_lease_seconds
            - worker_policy.termination_grace_seconds
            - DEFAULT_DURABLE_WORKER_PUBLICATION_MARGIN_SECONDS
        )
        if max_execution_seconds <= 0:
            raise AcceptedRunLeaseExpiredError(
                work.claim,
                "isolated worker deadline allocation",
            )
        execution_policy = replace(
            worker_policy,
            timeout_seconds=min(
                worker_policy.timeout_seconds,
                max_execution_seconds,
            ),
        )
        live_authority_validator = AcceptedRunWorkerAuthorityValidator(
            repository=self.repository,
            claim=work.claim,
            clock=self._now_unix_ms,
        )

        def validate_worker_authority(
            request: WorkerInvokeRequest,
            worker_result: WorkerInvokeResult,
        ) -> None:
            decode_durable_worker_result(request, worker_result)
            live_authority_validator(request, worker_result)

        try:
            worker_result = ProcessWorkerExecutor(
                target=worker_target,
                policy=execution_policy,
                authority_validator=validate_worker_authority,
            ).invoke(worker_request)
        except ProcessWorkerRequestTooLarge as error:
            result = DurableWorkerOutcome(
                run_id=work.claim.run_id,
                status="failed",
                outputs={
                    "error": {
                        "actualBytes": error.actual_bytes,
                        "code": "worker_request_too_large",
                        "maxBytes": error.max_bytes,
                    }
                },
                checkpoint=None,
            )
        except ProcessWorkerResponseTooLarge as error:
            result = DurableWorkerOutcome(
                run_id=work.claim.run_id,
                status="failed",
                outputs={
                    "error": {
                        "code": "worker_response_too_large",
                        "maxBytes": error.max_bytes,
                    }
                },
                checkpoint=None,
            )
        else:
            result = decode_durable_worker_result(worker_request, worker_result)
        committed_at_unix_ms = self._now_unix_ms()
        if result.status == "waiting_callback":
            if result.checkpoint is None:
                raise DurableAcceptedRunIntegrityError(
                    "waiting durable accepted run has no runtime checkpoint"
                )
            stored_checkpoint = encode_runtime_checkpoint(result.checkpoint)
            operation = canonical_loads(canonical_dumps(result.checkpoint.operation))
            if not isinstance(operation, dict):
                raise DurableAcceptedRunIntegrityError(
                    "durable accepted-run checkpoint operation must be an object"
                )
            operation_id = operation.get("operation_id")
            operation_attempt_id = operation.get("attempt_id")
            if (
                type(operation_id) is not str
                or not operation_id
                or operation_id != operation_id.strip()
                or type(operation_attempt_id) is not str
                or not operation_attempt_id
                or operation_attempt_id != operation_attempt_id.strip()
            ):
                raise DurableAcceptedRunIntegrityError(
                    "durable accepted-run checkpoint operation identity is invalid"
                )
            callback_identity_digest = canonical_hash(
                {
                    "checkpointDigest": stored_checkpoint.checkpoint_digest,
                    "runId": work.claim.run_id,
                    "tenantId": work.claim.tenant_id,
                }
            )
            callback_issuance = CallbackIssuanceIdentity(
                run_id=work.claim.run_id,
                checkpoint_digest=stored_checkpoint.checkpoint_digest,
                operation_id=operation_id,
                operation_attempt_id=operation_attempt_id,
                callback_idempotency_key=(
                    f"callback:{callback_identity_digest.removeprefix('sha256:')}"
                ),
                lease_generation=work.claim.lease_generation,
                fencing_token=work.claim.fencing_token,
            )
            issuance_value = {
                "callbackIdempotencyKey": (callback_issuance.callback_idempotency_key),
                "checkpointDigest": callback_issuance.checkpoint_digest,
                "expectedStateVersion": work.state_version + 1,
                "fencingToken": callback_issuance.fencing_token,
                "leaseGeneration": callback_issuance.lease_generation,
                "operationAttemptId": (callback_issuance.operation_attempt_id),
                "operationId": callback_issuance.operation_id,
                "runId": callback_issuance.run_id,
            }
            dispatch_value = {
                "callbackIssuance": issuance_value,
                "invocation": invocation,
                "operation": operation,
                "runId": work.claim.run_id,
                "tenantId": work.claim.tenant_id,
            }
            dispatch_digest = canonical_hash(dispatch_value)
            waiting_event_value = {
                "checkpointDigest": stored_checkpoint.checkpoint_digest,
                "runId": work.claim.run_id,
                "state": "waiting_callback",
            }
            return self.repository.commit_waiting(
                AcceptedRunWaitingCommit(
                    claim=work.claim,
                    expected_state_version=work.state_version,
                    checkpoint=stored_checkpoint,
                    callback_issuance=callback_issuance,
                    waiting_event=AcceptedRunEventIntent(
                        kind="run_waiting_callback",
                        payload_json=canonical_dumps(waiting_event_value),
                        payload_digest=canonical_hash(waiting_event_value),
                        created_at_unix_ms=committed_at_unix_ms,
                    ),
                    dispatch_effect=AcceptedRunEffectIntent(
                        effect_id=(
                            "effect-operation-dispatch:"
                            f"{dispatch_digest.removeprefix('sha256:')}"
                        ),
                        kind=AcceptedRunEffectKind.OPERATION_DISPATCH,
                        idempotency_key=(
                            "operation-dispatch:"
                            f"{callback_identity_digest.removeprefix('sha256:')}"
                        ),
                        payload_json=canonical_dumps(dispatch_value),
                        payload_digest=dispatch_digest,
                    ),
                )
            )

        terminal_value = {
            "outputs": canonical_loads(canonical_dumps(dict(result.outputs))),
            "status": result.status,
        }
        result_digest = canonical_hash(terminal_value)
        terminal_event_value = {
            "resultDigest": result_digest,
            "runId": work.claim.run_id,
            "state": result.status,
        }
        completion_value = {
            "invocation": invocation,
            "result": terminal_value,
            "resultDigest": result_digest,
            "runId": work.claim.run_id,
            "tenantId": work.claim.tenant_id,
        }
        completion_digest = canonical_hash(completion_value)
        completion_identity_digest = canonical_hash(
            {
                "runId": work.claim.run_id,
                "tenantId": work.claim.tenant_id,
            }
        )
        return self.repository.commit_terminal(
            AcceptedRunTerminalCommit(
                claim=work.claim,
                expected_state_version=work.state_version,
                terminal_status=result.status,
                result_json=canonical_dumps(terminal_value),
                result_digest=result_digest,
                terminal_event=AcceptedRunEventIntent(
                    kind={
                        "cancelled": "run_cancelled",
                        "failed": "run_failed",
                        "succeeded": "run_succeeded",
                    }[result.status],
                    payload_json=canonical_dumps(terminal_event_value),
                    payload_digest=canonical_hash(terminal_event_value),
                    created_at_unix_ms=committed_at_unix_ms,
                ),
                completion_effect=AcceptedRunEffectIntent(
                    effect_id=(
                        f"effect-completion:{completion_digest.removeprefix('sha256:')}"
                    ),
                    kind=AcceptedRunEffectKind.COMPLETION,
                    idempotency_key=(
                        "completion:"
                        f"{completion_identity_digest.removeprefix('sha256:')}"
                    ),
                    payload_json=canonical_dumps(completion_value),
                    payload_digest=completion_digest,
                ),
            )
        )


def durable_server_route_manifest() -> ServerRouteManifest:
    """Return the intentionally narrow route set for the durable preview."""

    return ServerRouteManifest(
        (
            ServerEndpoint(
                "GET",
                "/health",
                "http",
                "health",
                auth_required=False,
            ),
            ServerEndpoint(
                "POST",
                "/runs",
                "http",
                "admit_run",
                auth_required=True,
            ),
            ServerEndpoint(
                "GET",
                "/runs/{run_id}",
                "http",
                "get_run",
                auth_required=True,
            ),
            ServerEndpoint(
                "GET",
                "/runs/{run_id}/events",
                "http",
                "read_run_events",
                auth_required=True,
            ),
            ServerEndpoint(
                "POST",
                "/runs/{run_id}/callbacks/{operation_id}",
                "http",
                "submit_run_callback",
                auth_required=True,
            ),
            ServerEndpoint(
                "POST",
                "/runs/{run_id}/cancel",
                "http",
                "cancel_run",
                auth_required=True,
            ),
            ServerEndpoint(
                "POST",
                "/runs/{run_id}/expire",
                "http",
                "expire_run",
                auth_required=True,
            ),
            ServerEndpoint(
                "POST",
                "/runs/{run_id}/pause",
                "http",
                "pause_run",
                auth_required=True,
            ),
            ServerEndpoint(
                "POST",
                "/runs/{run_id}/resume",
                "http",
                "resume_run",
                auth_required=True,
            ),
        )
    )


@dataclass(slots=True)
class DurableAcceptedRunServerApp:
    """Fail-closed HTTP adapter for the repository-authoritative preview."""

    service: DurableAcceptedRunService
    auth_hook: ServerAuthHook
    route_manifest: ServerRouteManifest = field(
        default_factory=durable_server_route_manifest
    )
    max_request_body_bytes: int = DEFAULT_DURABLE_SERVER_MAX_REQUEST_BODY_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.service, DurableAcceptedRunService):
            raise ValueError(
                "durable accepted-run server requires a DurableAcceptedRunService"
            )
        if not callable(getattr(self.auth_hook, "authorize", None)):
            raise ValueError("durable accepted-run server requires an auth_hook")
        if not isinstance(self.route_manifest, ServerRouteManifest):
            raise ValueError(
                "durable accepted-run server route_manifest must be a "
                "ServerRouteManifest"
            )
        if (
            isinstance(self.max_request_body_bytes, bool)
            or not isinstance(self.max_request_body_bytes, int)
            or self.max_request_body_bytes < 1
        ):
            raise ValueError(
                "durable accepted-run server max_request_body_bytes must be "
                "a positive integer"
            )

    def _request_body_too_large_response(self) -> ServerResponse:
        return ServerResponse.json(
            413,
            {
                "ok": False,
                "error": "RequestBodyTooLarge",
                "maxBytes": self.max_request_body_bytes,
            },
        )

    def handle_stream(
        self,
        request: ServerRequestHead,
        read_body: Callable[[int], bytes],
        *,
        abort_body: Callable[[], None],
    ) -> ServerResponse:
        """Read a bounded wire body before routing or authentication."""

        if not callable(abort_body):
            raise TypeError(
                "durable streaming server request abort callback must be callable"
            )
        try:
            body = _read_bounded_server_request_body(
                request,
                read_body,
                max_body_bytes=self.max_request_body_bytes,
            )
        except _ServerRequestBodyTooLargeError:
            abort_body()
            return self._request_body_too_large_response()
        except _ServerRequestBodyLengthMismatchError as error:
            abort_body()
            return ServerResponse.json(
                400,
                {
                    "ok": False,
                    "error": str(error),
                },
            )
        except Exception:
            abort_body()
            raise
        return self.handle(
            ServerRequest(
                method=request.method,
                path=request.path,
                headers=dict(request.headers),
                query=dict(request.query),
                cookies=dict(request.cookies),
                body=body,
                requested_at=request.requested_at,
            )
        )

    def handle(self, request: ServerRequest) -> ServerResponse:
        if not isinstance(request, ServerRequest):
            raise TypeError(
                "durable accepted-run server request must be a ServerRequest"
            )
        if len(request.body) > self.max_request_body_bytes:
            return self._request_body_too_large_response()
        try:
            route = self.route_manifest.match(
                request.method,
                request.path,
                transport="http",
            )
        except ServerRouteNotFoundError:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": "NotFound",
                },
            )
        if route.endpoint.operation == "health":
            return ServerResponse.json(
                200,
                {
                    "ok": True,
                    "profile": "durable-preview",
                    "status": "healthy",
                },
            )

        try:
            auth_decision = self.auth_hook.authorize(
                ServerAuthRequest(
                    route=route.endpoint,
                    headers=request.headers,
                    query=request.query,
                    cookies=request.cookies,
                    requested_at=request.requested_at,
                )
            )
        except Exception:
            return ServerResponse.json(
                503,
                {
                    "ok": False,
                    "error": "AuthenticationUnavailable",
                },
            )
        if not isinstance(auth_decision, ServerAuthDecision):
            return ServerResponse.json(
                503,
                {
                    "ok": False,
                    "error": "AuthenticationUnavailable",
                },
            )
        if not auth_decision.allowed:
            return ServerResponse.json(
                401,
                {
                    "ok": False,
                    "error": "Unauthorized",
                    "reasonCodes": list(auth_decision.reason_codes),
                },
            )
        principal = auth_decision.principal
        if principal is None or principal.tenant_id is None:
            return ServerResponse.json(
                403,
                {
                    "ok": False,
                    "error": "TenantPrincipalRequired",
                },
            )

        try:
            if route.endpoint.operation == "admit_run":
                payload = canonical_loads(request.body)
                if not isinstance(payload, dict):
                    raise ValueError("durable run request body must be a JSON object")
                allowed_fields = {
                    "graph",
                    "inputs",
                    "invocation",
                    "requestId",
                    "responseMode",
                    "runId",
                }
                unknown_fields = sorted(set(payload) - allowed_fields)
                if unknown_fields:
                    raise ValueError(
                        "durable run request contains unknown fields: "
                        + ", ".join(unknown_fields)
                    )
                graph = payload.get("graph")
                inputs = payload.get("inputs", {})
                invocation = payload.get("invocation", {})
                if not isinstance(graph, dict):
                    raise ValueError("durable run request graph must be a JSON object")
                if not isinstance(inputs, dict):
                    raise ValueError("durable run request inputs must be a JSON object")
                if not isinstance(invocation, dict):
                    raise ValueError(
                        "durable run request invocation must be a JSON object"
                    )
                run_id = payload.get("runId")
                request_id = payload.get("requestId", run_id)
                response_mode = payload.get("responseMode", "accepted")
                for field_name, value in (
                    ("runId", run_id),
                    ("requestId", request_id),
                    ("responseMode", response_mode),
                ):
                    if type(value) is not str or not value or value != value.strip():
                        raise ValueError(
                            f"durable run request {field_name} must be an "
                            "exact non-empty string"
                        )
                assert isinstance(run_id, str)
                assert isinstance(request_id, str)
                assert isinstance(response_mode, str)
                if response_mode not in {"accepted", "background"}:
                    raise ValueError(
                        "durable run request responseMode must be accepted "
                        "or background"
                    )
                admission = self.service.admit_run(
                    tenant_id=principal.tenant_id,
                    owner_principal_id=principal.principal_id,
                    run_id=run_id,
                    idempotency_key=request_id,
                    graph=graph,
                    inputs=inputs,
                    invocation=invocation,
                )
                snapshot = self.service.get_run(
                    tenant_id=principal.tenant_id,
                    run_id=run_id,
                )
                if (
                    snapshot is None
                    or snapshot.owner_principal_id != principal.principal_id
                ):
                    raise DurableAcceptedRunIntegrityError(
                        "durable admission did not resolve to its owner"
                    )
                encoded_run_id = quote(run_id, safe="")
                return ServerResponse.json(
                    202,
                    {
                        "duplicate": admission.replayed,
                        "events": f"/runs/{encoded_run_id}/events",
                        "ok": True,
                        "runId": run_id,
                        "state": snapshot.phase.value,
                        "status": response_mode,
                    },
                )

            run_id = route.path_params["run_id"]
            snapshot = self.service.get_run(
                tenant_id=principal.tenant_id,
                run_id=run_id,
            )
            if (
                snapshot is None
                or snapshot.owner_principal_id != principal.principal_id
            ):
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": "NotFound",
                    },
                )

            if route.endpoint.operation == "get_run":
                response_payload: dict[str, object] = {
                    "eventHighWatermark": snapshot.event_high_watermark,
                    "eventLowWatermark": snapshot.event_low_watermark,
                    "ok": True,
                    "runId": snapshot.run_id,
                    "state": snapshot.phase.value,
                    "stateVersion": snapshot.state_version,
                }
                if snapshot.checkpoint_digest is not None:
                    response_payload["checkpointDigest"] = snapshot.checkpoint_digest
                if snapshot.paused_from_phase is not None:
                    response_payload["resumeState"] = snapshot.paused_from_phase.value
                if snapshot.terminal_status is not None:
                    response_payload["terminalStatus"] = snapshot.terminal_status
                if snapshot.terminal_result_json is not None:
                    response_payload["result"] = canonical_loads(
                        snapshot.terminal_result_json
                    )
                return ServerResponse.json(200, response_payload)

            if route.endpoint.operation == "submit_run_callback":
                payload = canonical_loads(request.body)
                if not isinstance(payload, dict):
                    raise ValueError(
                        "durable callback request body must be a JSON object"
                    )
                expected_fields = {
                    "callbackIdempotencyKey",
                    "checkpointDigest",
                    "expectedStateVersion",
                    "fencingToken",
                    "leaseGeneration",
                    "operationAttemptId",
                    "payload",
                    "receipt",
                }
                if set(payload) != expected_fields:
                    raise ValueError(
                        "durable callback request fields must match version 1"
                    )
                callback_payload = payload["payload"]
                receipt = payload["receipt"]
                if not isinstance(callback_payload, dict):
                    raise ValueError(
                        "durable callback request payload must be a JSON object"
                    )
                if not isinstance(receipt, dict):
                    raise ValueError(
                        "durable callback request receipt must be a JSON object"
                    )
                callback_acceptance = self.service.submit_callback(
                    tenant_id=principal.tenant_id,
                    owner_principal_id=principal.principal_id,
                    run_id=run_id,
                    operation_id=route.path_params["operation_id"],
                    expected_state_version=payload["expectedStateVersion"],
                    checkpoint_digest=payload["checkpointDigest"],
                    operation_attempt_id=payload["operationAttemptId"],
                    callback_idempotency_key=payload["callbackIdempotencyKey"],
                    lease_generation=payload["leaseGeneration"],
                    fencing_token=payload["fencingToken"],
                    payload=callback_payload,
                    receipt=receipt,
                )
                return ServerResponse.json(
                    202,
                    {
                        "acceptedEventSequence": (
                            callback_acceptance.accepted_event_sequence
                        ),
                        "ok": True,
                        "runId": run_id,
                        "stateVersion": callback_acceptance.state_version,
                        "status": "accepted",
                    },
                )

            if route.endpoint.operation in {"cancel_run", "expire_run"}:
                control_name = (
                    "cancellation"
                    if route.endpoint.operation == "cancel_run"
                    else "expiration"
                )
                payload = canonical_loads(request.body)
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"durable {control_name} request body must be a JSON object"
                    )
                expected_fields = {
                    "expectedStateVersion",
                    "reason",
                    "requestId",
                }
                if set(payload) != expected_fields:
                    raise ValueError(
                        f"durable {control_name} request fields must match version 1"
                    )
                request_id = payload["requestId"]
                reason = payload["reason"]
                for field_name, value in (
                    ("requestId", request_id),
                    ("reason", reason),
                ):
                    if type(value) is not str or not value or value != value.strip():
                        raise ValueError(
                            f"durable {control_name} request {field_name} "
                            "must be an exact non-empty string"
                        )
                assert isinstance(request_id, str)
                assert isinstance(reason, str)
                terminal_status = (
                    "cancelled"
                    if route.endpoint.operation == "cancel_run"
                    else "expired"
                )
                if route.endpoint.operation == "cancel_run":
                    control_acceptance = self.service.cancel_run(
                        tenant_id=principal.tenant_id,
                        owner_principal_id=principal.principal_id,
                        run_id=run_id,
                        expected_state_version=payload["expectedStateVersion"],
                        idempotency_key=request_id,
                        reason=reason,
                    )
                else:
                    control_acceptance = self.service.expire_run(
                        tenant_id=principal.tenant_id,
                        owner_principal_id=principal.principal_id,
                        run_id=run_id,
                        expected_state_version=payload["expectedStateVersion"],
                        idempotency_key=request_id,
                        reason=reason,
                    )
                return ServerResponse.json(
                    202,
                    {
                        "acceptedEventSequence": (
                            control_acceptance.accepted_event_sequence
                        ),
                        "duplicate": control_acceptance.replayed,
                        "ok": True,
                        "runId": run_id,
                        "state": "terminal",
                        "stateVersion": control_acceptance.state_version,
                        "terminalStatus": terminal_status,
                    },
                )

            if route.endpoint.operation in {"pause_run", "resume_run"}:
                payload = canonical_loads(request.body)
                if not isinstance(payload, dict):
                    raise ValueError(
                        "durable state control request body must be a JSON object"
                    )
                expected_fields = {
                    "expectedStateVersion",
                    "reason",
                    "requestId",
                }
                if set(payload) != expected_fields:
                    raise ValueError(
                        "durable state control request fields must match version 1"
                    )
                request_id = payload["requestId"]
                reason = payload["reason"]
                for field_name, value in (
                    ("requestId", request_id),
                    ("reason", reason),
                ):
                    if type(value) is not str or not value or value != value.strip():
                        raise ValueError(
                            f"durable state control request {field_name} must "
                            "be an exact non-empty string"
                        )
                assert isinstance(request_id, str)
                assert isinstance(reason, str)
                if route.endpoint.operation == "pause_run":
                    state_acceptance = self.service.pause_run(
                        tenant_id=principal.tenant_id,
                        owner_principal_id=principal.principal_id,
                        run_id=run_id,
                        expected_state_version=payload["expectedStateVersion"],
                        idempotency_key=request_id,
                        reason=reason,
                    )
                else:
                    state_acceptance = self.service.resume_run(
                        tenant_id=principal.tenant_id,
                        owner_principal_id=principal.principal_id,
                        run_id=run_id,
                        expected_state_version=payload["expectedStateVersion"],
                        idempotency_key=request_id,
                        reason=reason,
                    )
                return ServerResponse.json(
                    202,
                    {
                        "acceptedEventSequence": (
                            state_acceptance.accepted_event_sequence
                        ),
                        "action": state_acceptance.action.value,
                        "duplicate": state_acceptance.replayed,
                        "ok": True,
                        "runId": run_id,
                        "state": state_acceptance.resulting_phase.value,
                        "stateVersion": state_acceptance.state_version,
                    },
                )

            if route.endpoint.operation == "read_run_events":
                raw_after = request.query.get("after", "0")
                raw_limit = request.query.get("limit", "100")
                if (
                    not raw_after
                    or not raw_after.isascii()
                    or not raw_after.isdigit()
                    or not raw_limit
                    or not raw_limit.isascii()
                    or not raw_limit.isdigit()
                ):
                    raise ValueError(
                        "durable event cursor and limit must be decimal integers"
                    )
                page = self.service.read_events(
                    tenant_id=principal.tenant_id,
                    run_id=run_id,
                    after_sequence=int(raw_after),
                    limit=int(raw_limit),
                )
                return ServerResponse.json(
                    200,
                    {
                        "events": [
                            {
                                "kind": event.kind,
                                "payload": canonical_loads(event.payload_json),
                                "sequence": event.sequence,
                            }
                            for event in page.events
                        ],
                        "highWatermark": page.high_watermark,
                        "lowWatermark": page.low_watermark,
                        "nextAfter": page.next_after_sequence,
                        "ok": True,
                        "runId": run_id,
                    },
                )
        except AcceptedRunIdConflictError as error:
            existing = self.service.get_run(
                tenant_id=principal.tenant_id,
                run_id=error.run_id,
            )
            if (
                existing is not None
                and existing.owner_principal_id != principal.principal_id
            ):
                return ServerResponse.json(
                    404,
                    {
                        "ok": False,
                        "error": "NotFound",
                    },
                )
            return ServerResponse.json(
                409,
                {
                    "ok": False,
                    "error": str(error),
                },
            )
        except AcceptedRunNotFoundError:
            return ServerResponse.json(
                404,
                {
                    "ok": False,
                    "error": "NotFound",
                },
            )
        except AcceptedRunConflictError as error:
            return ServerResponse.json(
                409,
                {
                    "ok": False,
                    "error": str(error),
                },
            )
        except AcceptedRunStorageError as error:
            return ServerResponse.json(
                503,
                {
                    "ok": False,
                    "error": str(error),
                },
            )
        except (TypeError, ValueError) as error:
            return ServerResponse.json(
                400,
                {
                    "ok": False,
                    "error": str(error),
                },
            )
        except RuntimeError as error:
            return ServerResponse.json(
                503,
                {
                    "ok": False,
                    "error": str(error),
                },
            )

        return ServerResponse.json(
            404,
            {
                "ok": False,
                "error": "NotFound",
            },
        )
