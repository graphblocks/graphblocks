from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import secrets
import sqlite3

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash, canonical_loads
from graphblocks.provider_effects import (
    ProviderCancellation,
    ProviderCapabilitySnapshot,
    ProviderDeduplication,
    ProviderEffectContractError,
    ProviderEffectIdentityConflictError,
    ProviderEffectIntent,
    ProviderEffectKind,
    ProviderEffectState,
    ProviderRunAuthoritySnapshot,
    ProviderStatusLookup,
)
from graphblocks.server_storage import (
    AcceptedRunAdmission,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunEventIntent,
    AcceptedRunLeaseExpiredError,
    AdmissionIdentity,
    StaleAcceptedRunClaimError,
)
from graphblocks.sqlite_provider_effects import (
    PROVIDER_EFFECT_CLAIM_FORMAT_VERSION,
    PROVIDER_EFFECT_CLAIM_RELEASE_FORMAT_VERSION,
    PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
    ProviderEffectClaim,
    ProviderEffectClaimRelease,
    ProviderEffectClaimRequest,
    ProviderEffectWorkItem,
    SQLiteProviderEffectCorruptionError,
    SQLiteProviderEffectRepository,
    StaleProviderEffectClaimError,
    StoredProviderEffect,
    StoredProviderEffectEvent,
)
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_ADAPTER_RELEASE_DIGEST = "sha256:" + ("a" * 64)
_ORIGIN_AUTHORITY_DIGEST = "sha256:" + ("b" * 64)
_CLAIM_AUTHORITY_DIGEST = "sha256:" + ("0" * 64)
_CAPABILITY_AUTHORITY_DIGEST = "sha256:" + ("c" * 64)
_VERIFIER_RELEASE_DIGEST = "sha256:" + ("d" * 64)
_VERIFICATION_AUTHORITY_DIGEST = "sha256:" + ("e" * 64)


def _admission(
    *,
    tenant_id: str = "tenant-1",
    owner_principal_id: str = "principal-1",
    run_id: str = "run-1",
) -> AcceptedRunAdmission:
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "provider-effect-origin"},
        "spec": {"edges": [], "nodes": {}},
    }
    inputs = {"request": {"value": "hello"}}
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    request_identity = {
        "graph": graph,
        "inputs": inputs,
        "invocation": invocation,
        "ownerPrincipalId": owner_principal_id,
        "runId": run_id,
        "tenantId": tenant_id,
    }
    event_payload = {
        "runId": run_id,
        "state": "ready_initial",
        "tenantId": tenant_id,
    }
    return AcceptedRunAdmission(
        run_id=run_id,
        identity=AdmissionIdentity(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            admission_scope="POST:/runs",
            idempotency_key=f"admit:{tenant_id}:{run_id}",
            request_digest=canonical_hash(request_identity),
        ),
        graph_json=canonical_dumps(graph),
        graph_hash=canonical_hash(graph),
        inputs_json=canonical_dumps(inputs),
        invocation_json=canonical_dumps(invocation),
        ticket_json=canonical_dumps({"runId": run_id, "state": "accepted"}),
        graph_format_version="graphblocks.ai/Graph@v1",
        runtime_format_version="graphblocks.runtime@v1",
        checkpoint_format_version="graphblocks.runtime-checkpoint.v1",
        created_at_unix_ms=1_000,
        accepted_event=AcceptedRunEventIntent(
            kind="run_accepted",
            payload_json=canonical_dumps(event_payload),
            payload_digest=canonical_hash(event_payload),
            created_at_unix_ms=1_000,
        ),
    )


def _capability() -> ProviderCapabilitySnapshot:
    return ProviderCapabilitySnapshot(
        authority_digest=_CAPABILITY_AUTHORITY_DIGEST,
        adapter_id="payments.adapter",
        adapter_release_digest=_ADAPTER_RELEASE_DIGEST,
        target="payments.primary",
        operation="capture",
        reconciliation_verifier_id="payments.receipt-verifier",
        reconciliation_verifier_release_digest=_VERIFIER_RELEASE_DIGEST,
        reconciliation_verification_authority_digest=(_VERIFICATION_AUTHORITY_DIGEST),
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY,
        status_lookup=(ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID),
        cancellation=ProviderCancellation.NONE,
    )


def _seed_active_run(
    path: Path,
    *,
    tenant_id: str = "tenant-1",
    owner_principal_id: str = "principal-1",
    run_id: str = "run-1",
    lease_owner_id: str = "worker-1",
) -> tuple[AcceptedRunClaim, ProviderRunAuthoritySnapshot]:
    accepted_runs = SQLiteAcceptedRunRepository(path)
    accepted_runs.accept_run(
        _admission(
            tenant_id=tenant_id,
            owner_principal_id=owner_principal_id,
            run_id=run_id,
        )
    )
    claim = accepted_runs.claim_run(
        AcceptedRunClaimRequest(
            tenant_id=tenant_id,
            run_id=run_id,
            lease_owner_id=lease_owner_id,
            now_unix_ms=2_000,
            lease_duration_ms=1_000,
        )
    )
    assert claim is not None
    snapshot = accepted_runs.get_run(tenant_id=tenant_id, run_id=run_id)
    assert snapshot is not None
    return claim, ProviderRunAuthoritySnapshot(
        tenant_id=tenant_id,
        run_id=run_id,
        owner_principal_id=owner_principal_id,
        run_state_version=snapshot.state_version,
        lease_generation=claim.lease_generation,
        fencing_token=claim.fencing_token,
        checkpoint_digest=snapshot.checkpoint_digest,
    )


def _intent(
    capability: ProviderCapabilitySnapshot,
    run_authority: ProviderRunAuthoritySnapshot,
    *,
    effect_id: str = "effect-1",
    idempotency_key: str = "tenant-1:run-1:capture-1",
    amount: int = 1_200,
    owner_principal_id: str | None = None,
    run_state_version: int | None = None,
) -> ProviderEffectIntent:
    request = {"amount": amount, "currency": "KRW"}
    return ProviderEffectIntent(
        effect_id=effect_id,
        effect_kind=ProviderEffectKind.PROVIDER_MUTATION,
        tenant_id=run_authority.tenant_id,
        run_id=run_authority.run_id,
        owner_principal_id=(
            run_authority.owner_principal_id
            if owner_principal_id is None
            else owner_principal_id
        ),
        idempotency_key=idempotency_key,
        request_json=canonical_dumps(request),
        request_digest=canonical_hash(request),
        provider_target=capability.target,
        provider_operation=capability.operation,
        adapter_id=capability.adapter_id,
        adapter_release_digest=capability.adapter_release_digest,
        capability_snapshot_digest=capability.digest,
        provider_correlation_id="provider-correlation-1",
        origin_run_state_version=(
            run_authority.run_state_version
            if run_state_version is None
            else run_state_version
        ),
        origin_lease_generation=run_authority.lease_generation,
        origin_fencing_token=run_authority.fencing_token,
        origin_authority_digest=run_authority.digest,
        origin_checkpoint_digest=run_authority.checkpoint_digest,
        created_at_unix_ms=2_050,
    )


def _repository(
    path: Path,
    *,
    origin_authority_digest: str = _ORIGIN_AUTHORITY_DIGEST,
    claim_authority_digest: str = _CLAIM_AUTHORITY_DIGEST,
    clock: Callable[[], int] = lambda: 2_100,
    failpoint: Callable[[str], None] | None = None,
    attempt_id_factory: Callable[[], str] = (
        lambda: f"provider-send-attempt-{secrets.token_hex(8)}"
    ),
) -> SQLiteProviderEffectRepository:
    return SQLiteProviderEffectRepository(
        path,
        origin_authority_digest=origin_authority_digest,
        claim_authority_digest=claim_authority_digest,
        clock=clock,
        failpoint=failpoint,
        attempt_id_factory=attempt_id_factory,
    )


def _persist_pending_effect(
    path: Path,
    *,
    tenant_id: str = "tenant-1",
    owner_principal_id: str = "principal-1",
    run_id: str = "run-1",
    effect_id: str = "effect-1",
    idempotency_key: str = "tenant-1:run-1:capture-1",
) -> StoredProviderEffect:
    run_claim, run_authority = _seed_active_run(
        path,
        tenant_id=tenant_id,
        owner_principal_id=owner_principal_id,
        run_id=run_id,
    )
    capability = _capability()
    return _repository(path).persist_transferred_effect(
        claim=run_claim,
        intent=_intent(
            capability,
            run_authority,
            effect_id=effect_id,
            idempotency_key=idempotency_key,
        ),
        capability=capability,
    )


def _claim_request(
    *,
    tenant_id: str = "tenant-1",
    owner_principal_id: str = "principal-1",
    claim_owner_id: str = "provider-worker-1",
    lease_duration_ms: int = 100,
) -> ProviderEffectClaimRequest:
    return ProviderEffectClaimRequest(
        tenant_id=tenant_id,
        owner_principal_id=owner_principal_id,
        claim_owner_id=claim_owner_id,
        lease_duration_ms=lease_duration_ms,
    )


def test_sqlite_provider_effect_persists_exact_origin_transfer_and_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effects.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    intent = _intent(capability, run_authority)

    stored = _repository(path).persist_transferred_effect(
        claim=claim,
        intent=intent,
        capability=capability,
    )
    reopened = _repository(path, clock=lambda: 4_000)
    restored = reopened.get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    event_page = reopened.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
        after_sequence=0,
        limit=10,
    )

    assert stored == restored
    assert stored.state is ProviderEffectState.PENDING
    assert stored.origin_transfer.intent_digest == intent.digest
    assert stored.origin_transfer.run_authority_digest == run_authority.digest
    assert stored.origin_transfer.repository_authority_digest == (
        _ORIGIN_AUTHORITY_DIGEST
    )
    assert reopened.verify_transferred_origin(
        intent=intent,
        origin_transfer=stored.origin_transfer,
        admitted_at_unix_ms=4_000,
    )
    assert len(event_page.events) == 1
    event = event_page.events[0]
    assert event.sequence == 1
    assert event.kind == "origin_transferred"
    assert event.from_state is None
    assert event.to_state is ProviderEffectState.PENDING
    assert event_page.next_after_sequence is None
    payload = canonical_loads(event.payload_json)
    assert payload == {
        "capabilitySnapshotDigest": capability.digest,
        "effectId": intent.effect_id,
        "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
        "intentDigest": intent.digest,
        "originTransferDigest": stored.origin_transfer.digest,
        "state": "pending",
    }


def test_sqlite_provider_effect_exact_replay_survives_lease_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-replay.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    intent = _intent(capability, run_authority)
    stored = _repository(path).persist_transferred_effect(
        claim=claim,
        intent=intent,
        capability=capability,
    )

    replayed = _repository(path, clock=lambda: 9_000).persist_transferred_effect(
        claim=claim,
        intent=intent,
        capability=capability,
    )

    assert replayed == stored
    assert (
        len(
            _repository(path)
            .read_events(
                tenant_id="tenant-1",
                run_id="run-1",
                owner_principal_id="principal-1",
                effect_id="effect-1",
                after_sequence=0,
                limit=10,
            )
            .events
        )
        == 1
    )


def test_sqlite_provider_effect_rejects_repository_authority_substitution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-authority-substitution.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    intent = _intent(capability, run_authority)
    stored = _repository(path).persist_transferred_effect(
        claim=claim,
        intent=intent,
        capability=capability,
    )
    other_authority = _repository(
        path,
        origin_authority_digest="sha256:" + ("f" * 64),
    )

    assert not other_authority.verify_transferred_origin(
        intent=intent,
        origin_transfer=stored.origin_transfer,
        admitted_at_unix_ms=2_200,
    )
    with pytest.raises(
        ProviderEffectContractError,
        match="another origin authority",
    ):
        other_authority.persist_transferred_effect(
            claim=claim,
            intent=intent,
            capability=capability,
        )


def test_sqlite_provider_effect_rejects_divergent_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-conflict.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    intent = _intent(capability, run_authority)
    repository = _repository(path)
    repository.persist_transferred_effect(
        claim=claim,
        intent=intent,
        capability=capability,
    )

    with pytest.raises(
        ProviderEffectIdentityConflictError,
        match="changed immutable intent",
    ):
        repository.persist_transferred_effect(
            claim=claim,
            intent=_intent(capability, run_authority, amount=1_300),
            capability=capability,
        )


def test_sqlite_provider_effect_rejects_idempotency_alias(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-idempotency.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    repository = _repository(path)
    repository.persist_transferred_effect(
        claim=claim,
        intent=_intent(capability, run_authority),
        capability=capability,
    )

    with pytest.raises(
        ProviderEffectIdentityConflictError,
        match="belongs to another effect",
    ):
        repository.persist_transferred_effect(
            claim=claim,
            intent=_intent(capability, run_authority, effect_id="effect-2"),
            capability=capability,
        )


@pytest.mark.parametrize(
    "claim_change",
    (
        {"lease_owner_id": "stale-worker"},
        {"lease_generation": 99},
        {"fencing_token": 99},
        {"lease_expires_at_unix_ms": 9_000},
    ),
)
def test_sqlite_provider_effect_rejects_stale_run_claim(
    tmp_path: Path,
    claim_change: dict[str, object],
) -> None:
    path = tmp_path / "provider-effect-stale.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()

    with pytest.raises(StaleAcceptedRunClaimError):
        _repository(path).persist_transferred_effect(
            claim=replace(claim, **claim_change),
            intent=_intent(capability, run_authority),
            capability=capability,
        )


def test_sqlite_provider_effect_rechecks_expiry_after_write_lock_wait(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-expired-after-lock.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    now = [2_100]
    repository = _repository(path, clock=lambda: now[0])
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("PRAGMA foreign_keys = ON")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                repository.persist_transferred_effect,
                claim=claim,
                intent=_intent(capability, run_authority),
                capability=capability,
            )
            now[0] = claim.lease_expires_at_unix_ms
            blocker.rollback()
            with pytest.raises(AcceptedRunLeaseExpiredError):
                future.result()
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()


def test_sqlite_provider_effect_rechecks_expiry_before_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-expired-before-commit.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    times = iter((2_100, claim.lease_expires_at_unix_ms))

    with pytest.raises(AcceptedRunLeaseExpiredError):
        _repository(path, clock=lambda: next(times)).persist_transferred_effect(
            claim=claim,
            intent=_intent(capability, run_authority),
            capability=capability,
        )

    assert (
        _repository(path).get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )
        is None
    )


@pytest.mark.parametrize(
    "intent_change",
    (
        {"owner_principal_id": "principal-2"},
        {"run_state_version": 99},
    ),
)
def test_sqlite_provider_effect_rejects_mismatched_run_authority(
    tmp_path: Path,
    intent_change: dict[str, object],
) -> None:
    path = tmp_path / "provider-effect-authority-mismatch.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()

    with pytest.raises(ProviderEffectContractError):
        _repository(path).persist_transferred_effect(
            claim=claim,
            intent=_intent(capability, run_authority, **intent_change),
            capability=capability,
        )


@pytest.mark.parametrize(
    "failpoint_name",
    (
        "persist_transferred_effect.after_effect_insert",
        "persist_transferred_effect.after_event_insert",
    ),
)
def test_sqlite_provider_effect_rolls_back_origin_transaction(
    tmp_path: Path,
    failpoint_name: str,
) -> None:
    path = tmp_path / "provider-effect-rollback.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()

    def inject(name: str) -> None:
        if name == failpoint_name:
            raise RuntimeError("injected provider-effect failure")

    with pytest.raises(RuntimeError, match="injected provider-effect failure"):
        _repository(path, failpoint=inject).persist_transferred_effect(
            claim=claim,
            intent=_intent(capability, run_authority),
            capability=capability,
        )

    assert (
        _repository(path).get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )
        is None
    )
    assert (
        not _repository(path)
        .read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
            after_sequence=0,
            limit=10,
        )
        .events
    )


def test_sqlite_provider_effect_recovers_after_commit_response_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-response-loss.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    intent = _intent(capability, run_authority)

    def inject(name: str) -> None:
        if name == "persist_transferred_effect.after_commit":
            raise RuntimeError("response lost after commit")

    with pytest.raises(RuntimeError, match="response lost after commit"):
        _repository(path, failpoint=inject).persist_transferred_effect(
            claim=claim,
            intent=intent,
            capability=capability,
        )

    replayed = _repository(path, clock=lambda: 9_000).persist_transferred_effect(
        claim=claim,
        intent=intent,
        capability=capability,
    )
    assert replayed.intent == intent
    assert replayed.origin_transfer.intent_digest == intent.digest


def test_sqlite_provider_effect_concurrent_exact_replay_inserts_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-concurrent.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    intent = _intent(capability, run_authority)

    def persist() -> object:
        return _repository(path).persist_transferred_effect(
            claim=claim,
            intent=intent,
            capability=capability,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        records = tuple(executor.map(lambda _: persist(), range(2)))

    assert records[0] == records[1]
    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute("SELECT count(*) FROM provider_effects").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM provider_effect_events"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_sqlite_provider_effect_lookup_is_tenant_and_run_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-scope.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    repository = _repository(path)
    repository.persist_transferred_effect(
        claim=claim,
        intent=_intent(capability, run_authority),
        capability=capability,
    )

    assert (
        repository.get_effect(
            tenant_id="tenant-2",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )
        is None
    )
    assert (
        repository.get_effect(
            tenant_id="tenant-1",
            run_id="run-2",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )
        is None
    )
    assert (
        repository.get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-2",
            effect_id="effect-1",
        )
        is None
    )
    assert not repository.read_events(
        tenant_id="tenant-2",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
        after_sequence=0,
        limit=10,
    ).events
    assert not repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-2",
        effect_id="effect-1",
        after_sequence=0,
        limit=10,
    ).events


def test_sqlite_provider_effect_identity_is_scoped_by_internal_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-multi-tenant.sqlite3"
    first_claim, first_authority = _seed_active_run(path)
    second_claim, second_authority = _seed_active_run(
        path,
        tenant_id="tenant-2",
        owner_principal_id="principal-2",
        run_id="run-2",
        lease_owner_id="worker-2",
    )
    capability = _capability()
    repository = _repository(path)
    first = repository.persist_transferred_effect(
        claim=first_claim,
        intent=_intent(capability, first_authority, amount=1_200),
        capability=capability,
    )
    second = repository.persist_transferred_effect(
        claim=second_claim,
        intent=_intent(
            capability,
            second_authority,
            idempotency_key="tenant-2:run-2:capture-1",
            amount=2_400,
        ),
        capability=capability,
    )

    assert first.intent.effect_id == second.intent.effect_id == "effect-1"
    assert first.intent.tenant_id == "tenant-1"
    assert second.intent.tenant_id == "tenant-2"
    assert (
        repository.get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )
        == first
    )
    assert (
        repository.get_effect(
            tenant_id="tenant-2",
            run_id="run-2",
            owner_principal_id="principal-2",
            effect_id="effect-1",
        )
        == second
    )


@pytest.mark.parametrize(
    ("column", "replacement"),
    (
        ("intent_json", "{}"),
        ("intent_digest", "sha256:" + ("f" * 64)),
        ("capability_snapshot_json", "{}"),
        ("origin_transfer_json", "{}"),
    ),
)
def test_sqlite_provider_effect_fails_closed_on_persisted_identity_corruption(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    path = tmp_path / f"provider-effect-corrupt-{column}.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    repository = _repository(path)
    repository.persist_transferred_effect(
        claim=claim,
        intent=_intent(capability, run_authority),
        capability=capability,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"UPDATE provider_effects SET {column} = ? WHERE effect_id = 'effect-1'",
            (replacement,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteProviderEffectCorruptionError):
        repository.get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )


@pytest.mark.parametrize("mutation", ("delete", "kind", "payload"))
def test_sqlite_provider_effect_fails_closed_on_event_chain_corruption(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / f"provider-effect-event-corrupt-{mutation}.sqlite3"
    claim, run_authority = _seed_active_run(path)
    capability = _capability()
    repository = _repository(path)
    repository.persist_transferred_effect(
        claim=claim,
        intent=_intent(capability, run_authority),
        capability=capability,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if mutation == "delete":
            connection.execute("DELETE FROM provider_effect_events")
        elif mutation == "kind":
            connection.execute(
                "UPDATE provider_effect_events SET kind = 'forged_event'"
            )
        else:
            row = connection.execute(
                "SELECT payload_json FROM provider_effect_events"
            ).fetchone()
            assert row is not None
            payload = canonical_loads(row[0])
            assert type(payload) is dict
            payload["intentDigest"] = "sha256:" + ("f" * 64)
            connection.execute(
                """
                UPDATE provider_effect_events
                SET payload_json = ?, payload_digest = ?
                """,
                (canonical_dumps(payload), canonical_hash(payload)),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteProviderEffectCorruptionError):
        repository.get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )


def test_sqlite_provider_effect_claim_is_closed_durable_and_journaled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim.sqlite3"
    pending = _persist_pending_effect(path)
    repository = _repository(
        path,
        clock=lambda: 2_200,
        attempt_id_factory=lambda: "provider-send-attempt-1",
    )

    work_item = repository.claim_next_effect(_claim_request())

    assert work_item is not None
    claim = work_item.claim
    assert work_item.effect.state is ProviderEffectState.CLAIMED
    assert work_item.effect.intent == pending.intent
    assert claim.format_version == PROVIDER_EFFECT_CLAIM_FORMAT_VERSION
    assert ProviderEffectClaim.from_wire(claim.to_wire()) == claim
    assert claim.claim_authority_digest == _CLAIM_AUTHORITY_DIGEST
    assert claim.claim_owner_id == "provider-worker-1"
    assert claim.claim_generation == claim.claim_fencing_token == 1
    assert claim.claim_started_at_unix_ms == claim.admitted_at_unix_ms == 2_200
    assert claim.claim_expires_at_unix_ms == 2_300
    assert claim.send_attempt_id == "provider-send-attempt-1"
    assert claim.previous_send_attempt_digest is None
    restored = _repository(path, clock=lambda: 2_250).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored == work_item.effect
    events = repository.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
        after_sequence=0,
        limit=10,
    ).events
    assert tuple(event.kind for event in events) == (
        "origin_transferred",
        "send_claimed",
    )
    payload = canonical_loads(events[-1].payload_json)
    assert payload == {
        "claim": claim.to_wire(),
        "claimDigest": claim.digest,
        "effectId": "effect-1",
        "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
        "intentDigest": pending.intent.digest,
        "state": "claimed",
    }


def test_sqlite_provider_effect_claim_replays_active_owner_after_response_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim-response-loss.sqlite3"
    _persist_pending_effect(path)

    def inject(name: str) -> None:
        if name == "claim_next_effect.after_commit":
            raise RuntimeError("claim response lost after commit")

    with pytest.raises(RuntimeError, match="claim response lost after commit"):
        _repository(
            path,
            clock=lambda: 2_200,
            failpoint=inject,
            attempt_id_factory=lambda: "provider-send-attempt-1",
        ).claim_next_effect(_claim_request())

    def unexpected_attempt_id() -> str:
        raise AssertionError("response-loss replay must not issue another attempt")

    replayed = _repository(
        path,
        clock=lambda: 2_250,
        attempt_id_factory=unexpected_attempt_id,
    ).claim_next_effect(_claim_request(lease_duration_ms=50))
    assert replayed is not None
    assert replayed.claim.send_attempt_id == "provider-send-attempt-1"
    assert replayed.claim.claim_expires_at_unix_ms == 2_300
    assert replayed.effect.state_version == 2


def test_sqlite_provider_effect_rechecks_active_replay_expiry_before_return(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim-replay-expiry.sqlite3"
    _persist_pending_effect(path)
    first = _repository(
        path,
        clock=lambda: 2_200,
        attempt_id_factory=lambda: "provider-send-attempt-1",
    ).claim_next_effect(_claim_request())
    assert first is not None
    times = iter((2_299, 2_300, 2_300))

    reclaimed = _repository(
        path,
        clock=lambda: next(times),
        attempt_id_factory=lambda: "provider-send-attempt-2",
    ).claim_next_effect(_claim_request())

    assert reclaimed is not None
    assert reclaimed.claim.claim_generation == 2
    assert reclaimed.claim.claim_fencing_token == 2
    assert reclaimed.claim.send_attempt_id == "provider-send-attempt-2"
    assert reclaimed.claim.digest != first.claim.digest


def test_sqlite_provider_effect_rejects_replay_clock_before_active_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim-replay-clock-regression.sqlite3"
    _persist_pending_effect(path)
    first = _repository(path, clock=lambda: 2_200).claim_next_effect(_claim_request())
    assert first is not None

    with pytest.raises(ValueError, match="clock moved behind the active claim"):
        _repository(path, clock=lambda: 2_100).claim_next_effect(_claim_request())

    restored = _repository(path, clock=lambda: 2_250).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored == first.effect


def test_sqlite_provider_effect_claim_is_tenant_and_owner_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim-scope.sqlite3"
    _persist_pending_effect(path)
    _persist_pending_effect(
        path,
        tenant_id="tenant-2",
        owner_principal_id="principal-2",
        run_id="run-2",
        effect_id="effect-2",
        idempotency_key="tenant-2:run-2:capture-1",
    )
    repository = _repository(path, clock=lambda: 2_200)

    assert (
        repository.claim_next_effect(
            _claim_request(
                tenant_id="tenant-2",
                owner_principal_id="principal-1",
            )
        )
        is None
    )
    second = repository.claim_next_effect(
        _claim_request(
            tenant_id="tenant-2",
            owner_principal_id="principal-2",
        )
    )
    first = repository.claim_next_effect(_claim_request())

    assert second is not None
    assert second.effect.run_id == "run-2"
    assert second.effect.owner_principal_id == "principal-2"
    assert first is not None
    assert first.effect.run_id == "run-1"
    assert first.effect.owner_principal_id == "principal-1"


def test_sqlite_provider_effect_concurrent_claim_has_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-concurrent-claim.sqlite3"
    _persist_pending_effect(path)

    def claim(worker_number: int) -> ProviderEffectWorkItem | None:
        return _repository(
            path,
            clock=lambda: 2_200,
            attempt_id_factory=(lambda: f"provider-send-attempt-{worker_number}"),
        ).claim_next_effect(
            _claim_request(claim_owner_id=f"provider-worker-{worker_number}")
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim, (1, 2)))

    winners = tuple(result for result in results if result is not None)
    assert len(winners) == 1
    restored = _repository(path, clock=lambda: 2_250).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored is not None
    assert restored.state is ProviderEffectState.CLAIMED
    assert restored.claim == winners[0].claim


def test_sqlite_provider_effect_reclaims_only_expired_pre_send_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-expired-claim.sqlite3"
    _persist_pending_effect(path)
    first = _repository(
        path,
        clock=lambda: 2_200,
        attempt_id_factory=lambda: "provider-send-attempt-1",
    ).claim_next_effect(_claim_request())
    assert first is not None
    assert (
        _repository(path, clock=lambda: 2_299).claim_next_effect(
            _claim_request(claim_owner_id="provider-worker-2")
        )
        is None
    )

    reclaimed = _repository(
        path,
        clock=lambda: 2_300,
        attempt_id_factory=lambda: "provider-send-attempt-2",
    ).claim_next_effect(_claim_request(claim_owner_id="provider-worker-2"))

    assert reclaimed is not None
    assert reclaimed.claim.claim_generation == 2
    assert reclaimed.claim.claim_fencing_token == 2
    assert reclaimed.claim.send_attempt_id == "provider-send-attempt-2"
    assert reclaimed.claim.digest != first.claim.digest
    events = (
        _repository(path, clock=lambda: 2_350)
        .read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
            after_sequence=0,
            limit=10,
        )
        .events
    )
    assert events[-1].kind == "send_claim_reclaimed"
    with pytest.raises(StaleProviderEffectClaimError):
        _repository(path, clock=lambda: 2_350).release_claim_before_send(first.claim)


def test_sqlite_provider_effect_rejects_repository_authority_substitution_for_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim-authority-substitution.sqlite3"
    _persist_pending_effect(path)
    work_item = _repository(path, clock=lambda: 2_200).claim_next_effect(
        _claim_request()
    )
    assert work_item is not None

    with pytest.raises(ProviderEffectContractError, match="repository authority"):
        _repository(
            path,
            origin_authority_digest="sha256:" + ("f" * 64),
            clock=lambda: 2_250,
        ).claim_next_effect(_claim_request())
    with pytest.raises(ProviderEffectContractError, match="repository authority"):
        _repository(
            path,
            claim_authority_digest="sha256:" + ("f" * 64),
            clock=lambda: 2_250,
        ).claim_next_effect(_claim_request())
    with pytest.raises(StaleProviderEffectClaimError, match="claim authority"):
        _repository(
            path,
            claim_authority_digest="sha256:" + ("f" * 64),
            clock=lambda: 2_250,
        ).release_claim_before_send(work_item.claim)
    with pytest.raises(ProviderEffectContractError, match="origin authority"):
        _repository(
            path,
            origin_authority_digest="sha256:" + ("f" * 64),
            clock=lambda: 2_250,
        ).release_claim_before_send(work_item.claim)


def test_sqlite_provider_effect_rejects_duplicate_active_send_attempt_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-duplicate-send-attempt.sqlite3"
    _persist_pending_effect(path)
    _persist_pending_effect(
        path,
        run_id="run-2",
        effect_id="effect-2",
        idempotency_key="tenant-1:run-2:capture-1",
    )
    first = _repository(
        path,
        clock=lambda: 2_200,
        attempt_id_factory=lambda: "provider-send-attempt-shared",
    ).claim_next_effect(_claim_request())
    assert first is not None
    before = tuple(
        _repository(path, clock=lambda: 2_200).get_effect(
            tenant_id="tenant-1",
            run_id=run_id,
            owner_principal_id="principal-1",
            effect_id=effect_id,
        )
        for run_id, effect_id in (("run-1", "effect-1"), ("run-2", "effect-2"))
    )
    assert (
        sum(
            record is not None and record.state is ProviderEffectState.CLAIMED
            for record in before
        )
        == 1
    )
    assert (
        sum(
            record is not None and record.state is ProviderEffectState.PENDING
            for record in before
        )
        == 1
    )

    with pytest.raises(
        ProviderEffectIdentityConflictError,
        match="send attempt identity is already active",
    ):
        _repository(
            path,
            clock=lambda: 2_200,
            attempt_id_factory=lambda: "provider-send-attempt-shared",
        ).claim_next_effect(_claim_request(claim_owner_id="provider-worker-2"))

    after = tuple(
        _repository(path, clock=lambda: 2_200).get_effect(
            tenant_id="tenant-1",
            run_id=run_id,
            owner_principal_id="principal-1",
            effect_id=effect_id,
        )
        for run_id, effect_id in (("run-1", "effect-1"), ("run-2", "effect-2"))
    )
    assert after == before


def test_sqlite_provider_effect_rejects_duplicate_active_claim_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-duplicate-claim-owner.sqlite3"
    _persist_pending_effect(path)
    _persist_pending_effect(
        path,
        run_id="run-2",
        effect_id="effect-2",
        idempotency_key="tenant-1:run-2:capture-1",
    )
    first = _repository(path, clock=lambda: 2_200).claim_next_effect(
        _claim_request(claim_owner_id="provider-worker-1")
    )
    second = _repository(path, clock=lambda: 2_200).claim_next_effect(
        _claim_request(claim_owner_id="provider-worker-2")
    )
    assert first is not None
    assert second is not None
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE provider_effects
            SET claim_owner_id = 'provider-worker-1'
            WHERE claim_owner_id = 'provider-worker-2'
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteProviderEffectCorruptionError,
        match="multiple active claims",
    ):
        _repository(path, clock=lambda: 2_250).claim_next_effect(
            _claim_request(claim_owner_id="provider-worker-1")
        )


def test_sqlite_provider_effect_release_is_durable_replayable_and_fenced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-release.sqlite3"
    _persist_pending_effect(path)
    work_item = _repository(
        path,
        clock=lambda: 2_200,
        attempt_id_factory=lambda: "provider-send-attempt-1",
    ).claim_next_effect(_claim_request())
    assert work_item is not None

    released = _repository(path, clock=lambda: 2_400).release_claim_before_send(
        work_item.claim
    )

    assert released.state is ProviderEffectState.PENDING
    assert released.claim is None
    assert released.claim_generation == released.claim_fencing_token == 1
    release = released.last_pre_send_release
    assert release is not None
    assert release.format_version == PROVIDER_EFFECT_CLAIM_RELEASE_FORMAT_VERSION
    assert ProviderEffectClaimRelease.from_wire(release.to_wire()) == release
    assert release.claim_digest == work_item.claim.digest
    assert release.claim_generation == work_item.claim.claim_generation
    assert release.claim_fencing_token == work_item.claim.claim_fencing_token
    assert release.released_at_unix_ms == 2_400
    assert release.resulting_state_version == release.resulting_event_sequence == 3
    replayed = _repository(path, clock=lambda: -1).release_claim_before_send(
        work_item.claim
    )
    assert replayed == released
    events = (
        _repository(path, clock=lambda: 2_400)
        .read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
            after_sequence=0,
            limit=10,
        )
        .events
    )
    assert events[-1].kind == "send_claim_released"
    payload = canonical_loads(events[-1].payload_json)
    assert payload["release"] == release.to_wire()
    assert payload["releaseDigest"] == release.digest

    next_item = _repository(
        path,
        clock=lambda: 2_500,
        attempt_id_factory=lambda: "provider-send-attempt-2",
    ).claim_next_effect(_claim_request(claim_owner_id="provider-worker-2"))
    assert next_item is not None
    assert next_item.claim.claim_generation == 2
    with pytest.raises(StaleProviderEffectClaimError):
        _repository(path, clock=lambda: 2_550).release_claim_before_send(
            work_item.claim
        )


@pytest.mark.parametrize(
    "failpoint_name",
    (
        "claim_next_effect.after_effect_update",
        "claim_next_effect.after_event_insert",
    ),
)
def test_sqlite_provider_effect_rolls_back_claim_transaction(
    tmp_path: Path,
    failpoint_name: str,
) -> None:
    path = tmp_path / f"provider-effect-claim-rollback-{failpoint_name}.sqlite3"
    _persist_pending_effect(path)

    def inject(name: str) -> None:
        if name == failpoint_name:
            raise RuntimeError("injected provider-effect claim failure")

    with pytest.raises(RuntimeError, match="injected provider-effect claim failure"):
        _repository(
            path,
            clock=lambda: 2_200,
            failpoint=inject,
        ).claim_next_effect(_claim_request())

    restored = _repository(path).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored is not None
    assert restored.state is ProviderEffectState.PENDING
    assert restored.state_version == 1


def test_sqlite_provider_effect_rolls_back_claim_expired_before_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-claim-expired-before-commit.sqlite3"
    _persist_pending_effect(path)
    times = iter((2_200, 2_300))

    with pytest.raises(
        ProviderEffectContractError,
        match="claim expired before commit",
    ):
        _repository(path, clock=lambda: next(times)).claim_next_effect(
            _claim_request(lease_duration_ms=100)
        )

    restored = _repository(path).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored is not None
    assert restored.state is ProviderEffectState.PENDING
    assert restored.state_version == 1


@pytest.mark.parametrize(
    "failpoint_name",
    (
        "release_claim_before_send.after_effect_update",
        "release_claim_before_send.after_event_insert",
    ),
)
def test_sqlite_provider_effect_rolls_back_release_transaction(
    tmp_path: Path,
    failpoint_name: str,
) -> None:
    path = tmp_path / f"provider-effect-release-rollback-{failpoint_name}.sqlite3"
    _persist_pending_effect(path)
    work_item = _repository(path, clock=lambda: 2_200).claim_next_effect(
        _claim_request()
    )
    assert work_item is not None

    def inject(name: str) -> None:
        if name == failpoint_name:
            raise RuntimeError("injected provider-effect release failure")

    with pytest.raises(RuntimeError, match="injected provider-effect release failure"):
        _repository(
            path,
            clock=lambda: 2_250,
            failpoint=inject,
        ).release_claim_before_send(work_item.claim)

    restored = _repository(path, clock=lambda: 2_250).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored == work_item.effect


def test_sqlite_provider_effect_release_recovers_after_commit_response_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-release-response-loss.sqlite3"
    _persist_pending_effect(path)
    work_item = _repository(path, clock=lambda: 2_200).claim_next_effect(
        _claim_request()
    )
    assert work_item is not None

    def inject(name: str) -> None:
        if name == "release_claim_before_send.after_commit":
            raise RuntimeError("release response lost after commit")

    with pytest.raises(RuntimeError, match="release response lost after commit"):
        _repository(
            path,
            clock=lambda: 2_250,
            failpoint=inject,
        ).release_claim_before_send(work_item.claim)

    replayed = _repository(path, clock=lambda: -1).release_claim_before_send(
        work_item.claim
    )
    assert replayed.state is ProviderEffectState.PENDING
    assert replayed.last_pre_send_release is not None
    assert replayed.last_pre_send_release.claim_digest == work_item.claim.digest


@pytest.mark.parametrize(
    ("clock", "attempt_id_factory", "match"),
    (
        (lambda: -1, lambda: "provider-send-attempt-1", "non-negative"),
        (lambda: True, lambda: "provider-send-attempt-1", "non-negative"),
        (lambda: 2_200, lambda: "", "exact non-empty"),
    ),
)
def test_sqlite_provider_effect_rejects_invalid_claim_authority_inputs(
    tmp_path: Path,
    clock: Callable[[], int],
    attempt_id_factory: Callable[[], str],
    match: str,
) -> None:
    path = tmp_path / "provider-effect-invalid-claim-input.sqlite3"
    _persist_pending_effect(path)

    with pytest.raises(ValueError, match=match):
        _repository(
            path,
            clock=clock,
            attempt_id_factory=attempt_id_factory,
        ).claim_next_effect(_claim_request())

    restored = _repository(path).get_effect(
        tenant_id="tenant-1",
        run_id="run-1",
        owner_principal_id="principal-1",
        effect_id="effect-1",
    )
    assert restored is not None
    assert restored.state is ProviderEffectState.PENDING


def test_sqlite_provider_effect_never_auto_claims_send_started(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-send-started.sqlite3"
    _persist_pending_effect(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE provider_effects SET state = 'send_started'")
        connection.commit()
    finally:
        connection.close()

    assert (
        _repository(path, clock=lambda: 9_000).claim_next_effect(_claim_request())
        is None
    )


@pytest.mark.parametrize(
    ("column", "replacement"),
    (
        ("claim_json", "{}"),
        ("claim_digest", "sha256:" + ("f" * 64)),
    ),
)
def test_sqlite_provider_effect_fails_closed_on_claim_projection_corruption(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    path = tmp_path / f"provider-effect-claim-corrupt-{column}.sqlite3"
    _persist_pending_effect(path)
    work_item = _repository(path, clock=lambda: 2_200).claim_next_effect(
        _claim_request()
    )
    assert work_item is not None
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"UPDATE provider_effects SET {column} = ?",
            (replacement,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteProviderEffectCorruptionError):
        _repository(path, clock=lambda: 2_250).get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )


def test_sqlite_provider_effect_event_page_fails_closed_on_middle_gap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-event-middle-gap.sqlite3"
    _persist_pending_effect(path)
    first = _repository(path, clock=lambda: 2_200).claim_next_effect(_claim_request())
    assert first is not None
    _repository(path, clock=lambda: 2_250).release_claim_before_send(first.claim)
    second = _repository(path, clock=lambda: 2_300).claim_next_effect(
        _claim_request(claim_owner_id="provider-worker-2")
    )
    assert second is not None
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM provider_effect_events WHERE sequence = 2")
        connection.commit()
    finally:
        connection.close()

    assert (
        _repository(path, clock=lambda: 2_350).get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )
        == second.effect
    )
    with pytest.raises(SQLiteProviderEffectCorruptionError, match="not contiguous"):
        _repository(path, clock=lambda: 2_350).read_events(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
            after_sequence=0,
            limit=1,
        )


def test_sqlite_provider_effect_projection_rejects_event_above_watermark(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-effect-event-above-watermark.sqlite3"
    _persist_pending_effect(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO provider_effect_events (
              run_internal_id,
              effect_id,
              sequence,
              kind,
              from_state,
              to_state,
              payload_json,
              payload_digest,
              created_at_unix_ms
            )
            SELECT run_internal_id,
                   effect_id,
                   2,
                   kind,
                   from_state,
                   to_state,
                   payload_json,
                   payload_digest,
                   created_at_unix_ms
            FROM provider_effect_events
            WHERE sequence = 1
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteProviderEffectCorruptionError, match="watermark"):
        _repository(path).get_effect(
            tenant_id="tenant-1",
            run_id="run-1",
            owner_principal_id="principal-1",
            effect_id="effect-1",
        )


def test_sqlite_provider_effect_hot_transitions_decode_only_event_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider-effect-bounded-tail-validation.sqlite3"
    _persist_pending_effect(path)
    for cycle in range(10):
        claim_time = 2_200 + (cycle * 20)
        work_item = _repository(
            path, clock=lambda now=claim_time: now
        ).claim_next_effect(
            _claim_request(
                claim_owner_id=f"provider-worker-{cycle}",
                lease_duration_ms=10,
            )
        )
        assert work_item is not None
        _repository(
            path, clock=lambda now=claim_time + 1: now
        ).release_claim_before_send(work_item.claim)

    original = SQLiteProviderEffectRepository._event_from_row
    decoded_event_count = 0

    def counted_event_from_row(row: sqlite3.Row) -> StoredProviderEffectEvent:
        nonlocal decoded_event_count
        decoded_event_count += 1
        return original(row)

    monkeypatch.setattr(
        SQLiteProviderEffectRepository,
        "_event_from_row",
        staticmethod(counted_event_from_row),
    )
    work_item = _repository(path, clock=lambda: 2_500).claim_next_effect(
        _claim_request(claim_owner_id="provider-worker-final")
    )
    assert work_item is not None
    assert decoded_event_count == 2

    decoded_event_count = 0
    _repository(path, clock=lambda: 2_501).release_claim_before_send(work_item.claim)
    assert decoded_event_count == 2
