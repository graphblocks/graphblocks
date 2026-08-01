from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.provider_effects import (
    PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION,
    PROVIDER_EFFECT_INTENT_FORMAT_VERSION,
    PROVIDER_EFFECT_SEND_ATTEMPT_FORMAT_VERSION,
    PROVIDER_RECONCILIATION_EVIDENCE_FORMAT_VERSION,
    PROVIDER_RUN_AUTHORITY_SNAPSHOT_FORMAT_VERSION,
    ProviderCancellation,
    ProviderCapabilityAuthorityVerifier,
    ProviderCapabilitySnapshot,
    ProviderDeduplication,
    ProviderEffectAdmission,
    ProviderEffectAdmissionError,
    ProviderEffectContractError,
    ProviderEffectDecodeError,
    ProviderEffectEvidenceError,
    ProviderEffectIdentityConflictError,
    ProviderEffectIntent,
    ProviderEffectKind,
    ProviderEffectSendAttempt,
    ProviderEffectState,
    ProviderEffectStateConflictError,
    ProviderEffectTransition,
    ProviderReconciliationEvidence,
    ProviderReconciliationMethod,
    ProviderReconciliationOutcome,
    ProviderReconciliationVerifierAuthority,
    ProviderRunAuthoritySnapshot,
    ProviderRunAuthorityVerifier,
    ProviderStatusLookup,
    admit_provider_effect_intent,
    apply_provider_reconciliation_evidence,
    assert_same_provider_effect_intent,
    begin_provider_effect_send,
    retry_same_provider_effect_intent,
    transition_provider_effect_state,
    validate_provider_reconciliation_evidence,
)


_DIGEST_A = "sha256:" + ("a" * 64)
_DIGEST_B = "sha256:" + ("b" * 64)
_DIGEST_C = "sha256:" + ("c" * 64)
_DIGEST_D = "sha256:" + ("d" * 64)
_DIGEST_E = "sha256:" + ("e" * 64)
_DIGEST_F = "sha256:" + ("f" * 64)
_DIGEST_G = "sha256:" + ("0" * 64)


class _StringSubclass(str):
    pass


class _IntegerSubclass(int):
    pass


def _capability(
    *,
    deduplication: ProviderDeduplication = ProviderDeduplication.NONE,
    status_lookup: ProviderStatusLookup = ProviderStatusLookup.NONE,
    cancellation: ProviderCancellation = ProviderCancellation.NONE,
) -> ProviderCapabilitySnapshot:
    return ProviderCapabilitySnapshot(
        authority_digest=_DIGEST_D,
        adapter_id="payments.adapter",
        adapter_release_digest=_DIGEST_A,
        target="payments.primary",
        operation="capture",
        reconciliation_verifier_id="payments.receipt-verifier",
        reconciliation_verifier_release_digest=_DIGEST_E,
        reconciliation_verification_authority_digest=_DIGEST_F,
        deduplication=deduplication,
        status_lookup=status_lookup,
        cancellation=cancellation,
    )


def _run_authority() -> ProviderRunAuthoritySnapshot:
    return ProviderRunAuthoritySnapshot(
        tenant_id="tenant-a",
        run_id="run-7",
        owner_principal_id="principal-a",
        run_state_version=3,
        lease_generation=4,
        fencing_token=5,
        checkpoint_digest=_DIGEST_C,
    )


def _intent(
    capability: ProviderCapabilitySnapshot,
    *,
    correlation_id: str | None = "provider-correlation-7",
) -> ProviderEffectIntent:
    request = {"amount": 1200, "currency": "KRW"}
    run_authority = _run_authority()
    return ProviderEffectIntent(
        effect_id="effect-7",
        effect_kind=ProviderEffectKind.PROVIDER_MUTATION,
        tenant_id=run_authority.tenant_id,
        run_id=run_authority.run_id,
        owner_principal_id=run_authority.owner_principal_id,
        idempotency_key="tenant-a:run-7:effect-7",
        request_json=canonical_dumps(request),
        request_digest=canonical_hash(request),
        provider_target=capability.target,
        provider_operation=capability.operation,
        adapter_id=capability.adapter_id,
        adapter_release_digest=capability.adapter_release_digest,
        capability_snapshot_digest=capability.digest,
        provider_correlation_id=correlation_id,
        origin_run_state_version=run_authority.run_state_version,
        origin_lease_generation=run_authority.lease_generation,
        origin_fencing_token=run_authority.fencing_token,
        origin_authority_digest=run_authority.digest,
        origin_checkpoint_digest=run_authority.checkpoint_digest,
        created_at_unix_ms=1_000,
    )


class _CapabilityAuthority:
    authority_digest = _DIGEST_D

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts

    def verify(self, capability: ProviderCapabilitySnapshot) -> bool:
        return self.accepts and capability.authority_digest == self.authority_digest


_CAPABILITY_AUTHORITY: ProviderCapabilityAuthorityVerifier = _CapabilityAuthority()


class _RunAuthorityVerifier:
    authority_digest = _DIGEST_B

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts

    def verify(
        self,
        *,
        intent: ProviderEffectIntent,
        run_authority: ProviderRunAuthoritySnapshot,
        admitted_at_unix_ms: int,
    ) -> bool:
        return (
            self.accepts
            and run_authority == _run_authority()
            and intent.origin_authority_digest == run_authority.digest
            and admitted_at_unix_ms >= intent.created_at_unix_ms
        )


_RUN_AUTHORITY_VERIFIER: ProviderRunAuthorityVerifier = _RunAuthorityVerifier()


class _ClaimAuthority:
    authority_digest = _DIGEST_G

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.consumed_admission_digests: set[str] = set()
        self.active_send_attempt_digest: str | None = None
        self.latest_send_attempt_digest: str | None = None

    def verify_claim(
        self,
        *,
        intent: ProviderEffectIntent,
        previous_send_attempt: ProviderEffectSendAttempt | None,
        send_attempt_id: str,
        claim_owner_id: str,
        claim_generation: int,
        claim_fencing_token: int,
        claim_expires_at_unix_ms: int,
        admitted_at_unix_ms: int,
    ) -> bool:
        del intent, send_attempt_id, claim_owner_id
        previous_digest = (
            None if previous_send_attempt is None else previous_send_attempt.digest
        )
        return (
            self.accepts
            and previous_digest == self.latest_send_attempt_digest
            and claim_generation > 0
            and claim_fencing_token > 0
            and claim_expires_at_unix_ms > admitted_at_unix_ms
        )

    def claim_send(
        self,
        *,
        admission: ProviderEffectAdmission,
        send_attempt: ProviderEffectSendAttempt,
    ) -> bool:
        if (
            not self.accepts
            or admission.digest in self.consumed_admission_digests
            or self.active_send_attempt_digest is not None
            or send_attempt.admission_digest != admission.digest
            or send_attempt.claim_authority_digest != self.authority_digest
        ):
            return False
        self.consumed_admission_digests.add(admission.digest)
        self.active_send_attempt_digest = send_attempt.digest
        self.latest_send_attempt_digest = send_attempt.digest
        return True

    def verify_active_send(
        self,
        *,
        current: ProviderEffectState,
        admission: ProviderEffectAdmission,
        send_attempt: ProviderEffectSendAttempt,
    ) -> bool:
        del admission
        return (
            self.accepts
            and current
            in {ProviderEffectState.SEND_STARTED, ProviderEffectState.RECONCILING}
            and self.active_send_attempt_digest == send_attempt.digest
        )

    def settle_active_send(
        self,
        *,
        current: ProviderEffectState,
        next_state: ProviderEffectState,
        admission: ProviderEffectAdmission,
        send_attempt: ProviderEffectSendAttempt,
        evidence_digest: str,
    ) -> bool:
        del current, admission, evidence_digest
        if not self.accepts or self.active_send_attempt_digest != send_attempt.digest:
            return False
        if next_state in {
            ProviderEffectState.CONFIRMED_COMMITTED,
            ProviderEffectState.CONFIRMED_NOT_COMMITTED,
            ProviderEffectState.CONFIRMED_CANCELLED,
        }:
            self.active_send_attempt_digest = None
        return True


def _admission(
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    *,
    previous_send_attempt: ProviderEffectSendAttempt | None = None,
    admitted_at_unix_ms: int = 1_010,
    claim_authority: _ClaimAuthority | None = None,
    verifier_authority: ProviderReconciliationVerifierAuthority | None = None,
) -> ProviderEffectAdmission:
    active_claim_authority = claim_authority or _ClaimAuthority()
    active_verifier_authority = verifier_authority or _VERIFIER_AUTHORITY
    retry_offset = 0 if previous_send_attempt is None else 1
    return admit_provider_effect_intent(
        intent,
        capability,
        _run_authority(),
        capability_authority=_CAPABILITY_AUTHORITY,
        verifier_authority=active_verifier_authority,
        run_authority_verifier=_RUN_AUTHORITY_VERIFIER,
        claim_authority=active_claim_authority,
        send_attempt_id=f"send-attempt-{retry_offset + 1}",
        claim_owner_id=f"dispatcher-{retry_offset + 1}",
        claim_generation=10 + retry_offset,
        claim_fencing_token=20 + retry_offset,
        claim_expires_at_unix_ms=admitted_at_unix_ms + 100,
        admitted_at_unix_ms=admitted_at_unix_ms,
        previous_send_attempt=previous_send_attempt,
    )


def _send_attempt(
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    *,
    previous_send_attempt: ProviderEffectSendAttempt | None = None,
    claim_authority: _ClaimAuthority | None = None,
) -> tuple[ProviderEffectAdmission, ProviderEffectSendAttempt, _ClaimAuthority]:
    active_claim_authority = claim_authority or _ClaimAuthority()
    retry_offset = 0 if previous_send_attempt is None else 1
    admission = _admission(
        intent,
        capability,
        previous_send_attempt=previous_send_attempt,
        admitted_at_unix_ms=1_010 + (retry_offset * 100),
        claim_authority=active_claim_authority,
    )
    state, attempt = begin_provider_effect_send(
        ProviderEffectState.CLAIMED,
        intent,
        capability,
        admission,
        active_claim_authority,
        started_at_unix_ms=1_020 + (retry_offset * 100),
        previous_send_attempt=previous_send_attempt,
    )
    assert state is ProviderEffectState.SEND_STARTED
    return admission, attempt, active_claim_authority


class _EvidenceVerifier:
    verifier_id = "payments.receipt-verifier"
    verifier_release_digest = _DIGEST_E
    verification_authority_digest = _DIGEST_F

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts

    def verify(
        self,
        *,
        intent: ProviderEffectIntent,
        capability: ProviderCapabilitySnapshot,
        send_attempt: ProviderEffectSendAttempt,
        evidence: ProviderReconciliationEvidence,
    ) -> bool:
        expected = canonical_dumps(
            {
                "adapterReleaseDigest": capability.adapter_release_digest,
                "effectId": intent.effect_id,
                "method": evidence.method.value,
                "outcome": evidence.outcome.value,
                "sendAttemptDigest": send_attempt.digest,
            }
        )
        return self.accepts and evidence.provider_evidence_json == expected


class _AlternateEvidenceVerifier(_EvidenceVerifier):
    verifier_id = "alternate.receipt-verifier"


class _CopycatEvidenceVerifier(_EvidenceVerifier):
    def verify(
        self,
        *,
        intent: ProviderEffectIntent,
        capability: ProviderCapabilitySnapshot,
        send_attempt: ProviderEffectSendAttempt,
        evidence: ProviderReconciliationEvidence,
    ) -> bool:
        del intent, capability, send_attempt, evidence
        return True


_VERIFIER = _EvidenceVerifier()


class _VerifierAuthority:
    authority_digest = _DIGEST_F

    def __init__(
        self,
        verifier: _EvidenceVerifier | None = _VERIFIER,
    ) -> None:
        self.verifier = verifier

    def resolve(
        self,
        *,
        capability: ProviderCapabilitySnapshot,
    ) -> _EvidenceVerifier | None:
        if (
            capability.reconciliation_verifier_id != _VERIFIER.verifier_id
            or capability.reconciliation_verifier_release_digest
            != _VERIFIER.verifier_release_digest
            or capability.reconciliation_verification_authority_digest
            != self.authority_digest
        ):
            return None
        return self.verifier


_VERIFIER_AUTHORITY: ProviderReconciliationVerifierAuthority = _VerifierAuthority()


def _evidence(
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    *,
    send_attempt: ProviderEffectSendAttempt | None = None,
    method: ProviderReconciliationMethod = (ProviderReconciliationMethod.STATUS_LOOKUP),
    outcome: ProviderReconciliationOutcome = (ProviderReconciliationOutcome.COMMITTED),
    correlation_id: str | None = "provider-correlation-7",
    observed_at_unix_ms: int = 1_100,
) -> ProviderReconciliationEvidence:
    if send_attempt is None:
        _, send_attempt, _ = _send_attempt(intent, capability)
    provider_evidence = {
        "adapterReleaseDigest": capability.adapter_release_digest,
        "effectId": intent.effect_id,
        "method": method.value,
        "outcome": outcome.value,
        "sendAttemptDigest": send_attempt.digest,
    }
    provider_evidence_json = canonical_dumps(provider_evidence)
    return ProviderReconciliationEvidence(
        effect_id=intent.effect_id,
        intent_digest=intent.digest,
        capability_snapshot_digest=capability.digest,
        send_attempt_digest=send_attempt.digest,
        method=method,
        outcome=outcome,
        provider_evidence_json=provider_evidence_json,
        provider_evidence_digest=canonical_hash(provider_evidence),
        verifier_id=_VERIFIER.verifier_id,
        verifier_release_digest=_VERIFIER.verifier_release_digest,
        verification_authority_digest=_VERIFIER.verification_authority_digest,
        observed_at_unix_ms=observed_at_unix_ms,
        provider_correlation_id=correlation_id,
    )


def test_provider_effect_contracts_are_closed_versioned_and_content_bound() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY,
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY,
        cancellation=ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY,
    )
    run_authority = _run_authority()
    intent = _intent(capability)
    admission, send_attempt, _ = _send_attempt(intent, capability)
    evidence = _evidence(intent, capability, send_attempt=send_attempt)

    assert capability.format_version == PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION
    assert (
        run_authority.format_version == PROVIDER_RUN_AUTHORITY_SNAPSHOT_FORMAT_VERSION
    )
    assert intent.format_version == PROVIDER_EFFECT_INTENT_FORMAT_VERSION
    assert send_attempt.format_version == PROVIDER_EFFECT_SEND_ATTEMPT_FORMAT_VERSION
    assert evidence.format_version == PROVIDER_RECONCILIATION_EVIDENCE_FORMAT_VERSION
    assert ProviderCapabilitySnapshot.from_wire(capability.to_wire()) == capability
    assert (
        ProviderRunAuthoritySnapshot.from_wire(run_authority.to_wire()) == run_authority
    )
    assert ProviderEffectIntent.from_wire(intent.to_wire()) == intent
    assert ProviderEffectSendAttempt.from_wire(send_attempt.to_wire()) == send_attempt
    assert ProviderReconciliationEvidence.from_wire(evidence.to_wire()) == evidence
    assert capability.digest == canonical_hash(capability.to_wire())
    assert run_authority.digest == canonical_hash(run_authority.to_wire())
    assert intent.digest == canonical_hash(intent.to_wire())
    assert admission.digest == canonical_hash(admission.to_wire())
    assert send_attempt.digest == canonical_hash(send_attempt.to_wire())
    assert evidence.digest == canonical_hash(evidence.to_wire())


def test_provider_effect_contracts_reject_primitive_subclasses() -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    evidence = _evidence(intent, capability)

    with pytest.raises(ProviderEffectContractError):
        replace(
            capability,
            format_version=_StringSubclass(PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION),
        )
    with pytest.raises(ProviderEffectContractError):
        replace(intent, origin_run_state_version=_IntegerSubclass(3))
    with pytest.raises(ProviderEffectContractError):
        replace(evidence, observed_at_unix_ms=_IntegerSubclass(1_100))


@pytest.mark.parametrize(
    ("decoder", "wire"),
    [
        (ProviderCapabilitySnapshot.from_wire, _capability().to_wire()),
        (ProviderRunAuthoritySnapshot.from_wire, _run_authority().to_wire()),
        (
            ProviderEffectIntent.from_wire,
            _intent(
                _capability(
                    deduplication=(ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY)
                )
            ).to_wire(),
        ),
        (
            ProviderEffectSendAttempt.from_wire,
            _send_attempt(
                _intent(
                    _capability(
                        deduplication=(ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY)
                    )
                ),
                _capability(
                    deduplication=(ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY)
                ),
            )[1].to_wire(),
        ),
        (
            ProviderReconciliationEvidence.from_wire,
            _evidence(
                _intent(
                    _capability(
                        status_lookup=(
                            ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
                        )
                    )
                ),
                _capability(
                    status_lookup=(ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY)
                ),
            ).to_wire(),
        ),
    ],
)
def test_provider_effect_decoders_reject_open_or_incomplete_objects(
    decoder: object,
    wire: dict[str, object],
) -> None:
    decode = decoder
    assert callable(decode)

    with pytest.raises(ProviderEffectDecodeError):
        decode(None)
    with pytest.raises(ProviderEffectDecodeError):
        decode(wire | {"unknown": True})
    incomplete = dict(wire)
    incomplete.pop(next(iter(incomplete)))
    with pytest.raises(ProviderEffectDecodeError):
        decode(incomplete)


def test_provider_effect_intent_decoder_rejects_open_nested_objects() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    wire = _intent(capability).to_wire()

    for nested_field in ("request", "provider", "originAuthority"):
        malformed = deepcopy(wire)
        nested = malformed[nested_field]
        assert type(nested) is dict
        nested["unknown"] = True
        with pytest.raises(ProviderEffectDecodeError):
            ProviderEffectIntent.from_wire(malformed)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda wire: wire.__setitem__("adapterId", 7),
        lambda wire: wire.__setitem__("deduplication", {"value": "none"}),
        lambda wire: wire.__setitem__("statusLookup", False),
        lambda wire: wire.__setitem__("formatVersion", None),
    ],
)
def test_provider_capability_decoder_rejects_primitive_coercion(mutate: object) -> None:
    wire = _capability().to_wire()
    apply_mutation = mutate
    assert callable(apply_mutation)
    apply_mutation(wire)

    with pytest.raises(ProviderEffectDecodeError):
        ProviderCapabilitySnapshot.from_wire(wire)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda wire: wire.__setitem__("effectId", False),
        lambda wire: wire["request"].__setitem__("canonicalJson", []),
        lambda wire: wire["provider"].__setitem__("correlationId", 7),
        lambda wire: wire["originAuthority"].__setitem__(
            "runStateVersion",
            True,
        ),
        lambda wire: wire["originAuthority"].__setitem__("leaseGeneration", 0),
    ],
)
def test_provider_effect_intent_decoder_rejects_primitive_coercion(
    mutate: object,
) -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    wire = _intent(capability).to_wire()
    apply_mutation = mutate
    assert callable(apply_mutation)
    apply_mutation(wire)

    with pytest.raises(ProviderEffectDecodeError):
        ProviderEffectIntent.from_wire(wire)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda wire: wire.__setitem__("effectId", 7),
        lambda wire: wire.__setitem__("method", ["status_lookup"]),
        lambda wire: wire.__setitem__("outcome", False),
        lambda wire: wire.__setitem__("observedAtUnixMs", True),
        lambda wire: wire.__setitem__("providerCorrelationId", {}),
    ],
)
def test_provider_evidence_decoder_rejects_primitive_coercion(mutate: object) -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    wire = _evidence(intent, capability).to_wire()
    apply_mutation = mutate
    assert callable(apply_mutation)
    apply_mutation(wire)

    with pytest.raises(ProviderEffectDecodeError):
        ProviderReconciliationEvidence.from_wire(wire)


@pytest.mark.parametrize(
    ("request_json", "request_digest"),
    [
        ('{"currency":"KRW", "amount":1200}', _DIGEST_A),
        (
            canonical_dumps(["not", "an", "object"]),
            canonical_hash(["not", "an", "object"]),
        ),
        (canonical_dumps({"amount": 1200}), _DIGEST_A),
    ],
)
def test_provider_effect_intent_rejects_noncanonical_or_unbound_request(
    request_json: str,
    request_digest: str,
) -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )

    with pytest.raises(ProviderEffectContractError):
        replace(
            _intent(capability),
            request_json=request_json,
            request_digest=request_digest,
        )


@pytest.mark.parametrize(
    ("deduplication", "status_lookup", "cancellation", "correlation_id", "expected"),
    [
        (
            ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY,
            ProviderStatusLookup.NONE,
            ProviderCancellation.NONE,
            None,
            frozenset({ProviderReconciliationMethod.ATOMIC_DEDUPE_REPLAY}),
        ),
        (
            ProviderDeduplication.NONE,
            ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY,
            ProviderCancellation.NONE,
            None,
            frozenset({ProviderReconciliationMethod.STATUS_LOOKUP}),
        ),
        (
            ProviderDeduplication.NONE,
            ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID,
            ProviderCancellation.NONE,
            "provider-correlation-7",
            frozenset({ProviderReconciliationMethod.STATUS_LOOKUP}),
        ),
        (
            ProviderDeduplication.NONE,
            ProviderStatusLookup.NONE,
            ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY,
            None,
            frozenset({ProviderReconciliationMethod.CONFIRMED_CANCELLATION}),
        ),
        (
            ProviderDeduplication.NONE,
            ProviderStatusLookup.NONE,
            ProviderCancellation.CONFIRMED_BY_PREBOUND_CORRELATION_ID,
            "provider-correlation-7",
            frozenset({ProviderReconciliationMethod.CONFIRMED_CANCELLATION}),
        ),
    ],
)
def test_provider_effect_admission_accepts_only_applicable_recovery_capabilities(
    deduplication: ProviderDeduplication,
    status_lookup: ProviderStatusLookup,
    cancellation: ProviderCancellation,
    correlation_id: str | None,
    expected: frozenset[ProviderReconciliationMethod],
) -> None:
    capability = _capability(
        deduplication=deduplication,
        status_lookup=status_lookup,
        cancellation=cancellation,
    )
    intent = _intent(capability, correlation_id=correlation_id)

    assert _admission(intent, capability).applicable_methods == expected


@pytest.mark.parametrize(
    ("status_lookup", "cancellation", "correlation_id"),
    [
        (ProviderStatusLookup.NONE, ProviderCancellation.NONE, None),
        (ProviderStatusLookup.NONE, ProviderCancellation.REQUEST_ONLY, None),
        (
            ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID,
            ProviderCancellation.NONE,
            None,
        ),
        (
            ProviderStatusLookup.NONE,
            ProviderCancellation.CONFIRMED_BY_PREBOUND_CORRELATION_ID,
            None,
        ),
    ],
)
def test_provider_effect_admission_fails_closed_without_safe_recovery(
    status_lookup: ProviderStatusLookup,
    cancellation: ProviderCancellation,
    correlation_id: str | None,
) -> None:
    capability = _capability(
        status_lookup=status_lookup,
        cancellation=cancellation,
    )
    intent = _intent(capability, correlation_id=correlation_id)

    with pytest.raises(ProviderEffectAdmissionError):
        _admission(intent, capability)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("adapter_id", "other.adapter"),
        ("adapter_release_digest", _DIGEST_B),
        ("target", "payments.secondary"),
        ("operation", "refund"),
    ],
)
def test_provider_effect_admission_binds_the_complete_capability_snapshot(
    field_name: str,
    replacement: str,
) -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    different_capability = replace(capability, **{field_name: replacement})

    with pytest.raises(ProviderEffectAdmissionError):
        _admission(intent, different_capability)


def test_provider_effect_admission_rejects_a_forged_snapshot_digest() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = replace(_intent(capability), capability_snapshot_digest=_DIGEST_C)

    with pytest.raises(ProviderEffectAdmissionError):
        _admission(intent, capability)


def test_provider_effect_admission_requires_registry_authentic_capability() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)

    with pytest.raises(ProviderEffectAdmissionError):
        admit_provider_effect_intent(
            intent,
            capability,
            _run_authority(),
            capability_authority=_CapabilityAuthority(accepts=False),
            verifier_authority=_VERIFIER_AUTHORITY,
            run_authority_verifier=_RUN_AUTHORITY_VERIFIER,
            claim_authority=_ClaimAuthority(),
            send_attempt_id="send-attempt-1",
            claim_owner_id="dispatcher-1",
            claim_generation=10,
            claim_fencing_token=20,
            claim_expires_at_unix_ms=1_110,
            admitted_at_unix_ms=1_010,
        )
    with pytest.raises(ProviderEffectAdmissionError):
        admit_provider_effect_intent(
            intent,
            capability,
            _run_authority(),
            capability_authority=_CAPABILITY_AUTHORITY,
            verifier_authority=_VERIFIER_AUTHORITY,
            run_authority_verifier=_RunAuthorityVerifier(accepts=False),
            claim_authority=_ClaimAuthority(),
            send_attempt_id="send-attempt-1",
            claim_owner_id="dispatcher-1",
            claim_generation=10,
            claim_fencing_token=20,
            claim_expires_at_unix_ms=1_110,
            admitted_at_unix_ms=1_010,
        )


def test_provider_effect_admission_requires_a_registered_verifier() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)

    with pytest.raises(ProviderEffectAdmissionError):
        _admission(
            intent,
            capability,
            verifier_authority=_VerifierAuthority(None),
        )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("tenant_id", "tenant-b"),
        ("run_id", "run-8"),
        ("owner_principal_id", "principal-b"),
        ("run_state_version", 4),
        ("lease_generation", 5),
        ("fencing_token", 6),
        ("checkpoint_digest", None),
    ],
)
def test_provider_effect_admission_binds_repository_run_authority(
    field_name: str,
    replacement: object,
) -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)

    with pytest.raises(ProviderEffectAdmissionError):
        admit_provider_effect_intent(
            intent,
            capability,
            replace(_run_authority(), **{field_name: replacement}),
            capability_authority=_CAPABILITY_AUTHORITY,
            verifier_authority=_VERIFIER_AUTHORITY,
            run_authority_verifier=_RUN_AUTHORITY_VERIFIER,
            claim_authority=_ClaimAuthority(),
            send_attempt_id="send-attempt-1",
            claim_owner_id="dispatcher-1",
            claim_generation=10,
            claim_fencing_token=20,
            claim_expires_at_unix_ms=1_110,
            admitted_at_unix_ms=1_010,
        )


def test_provider_effect_retry_requires_the_complete_immutable_intent() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    assert_same_provider_effect_intent(intent, intent)

    changed_request = {"amount": 1201, "currency": "KRW"}
    mutations = [
        replace(intent, effect_id="effect-8"),
        replace(intent, effect_kind=ProviderEffectKind.TOOL_MUTATION),
        replace(intent, tenant_id="tenant-b"),
        replace(intent, run_id="run-8"),
        replace(intent, owner_principal_id="principal-b"),
        replace(intent, idempotency_key="different-key"),
        replace(
            intent,
            request_json=canonical_dumps(changed_request),
            request_digest=canonical_hash(changed_request),
        ),
        replace(intent, provider_target="payments.secondary"),
        replace(intent, provider_operation="refund"),
        replace(intent, adapter_id="other.adapter"),
        replace(intent, adapter_release_digest=_DIGEST_C),
        replace(intent, capability_snapshot_digest=_DIGEST_C),
        replace(intent, provider_correlation_id="other-correlation"),
        replace(intent, origin_run_state_version=4),
        replace(intent, origin_lease_generation=5),
        replace(intent, origin_fencing_token=6),
        replace(intent, origin_authority_digest=_DIGEST_C),
        replace(intent, origin_checkpoint_digest=None),
        replace(intent, created_at_unix_ms=1_001),
    ]

    for changed in mutations:
        with pytest.raises(ProviderEffectIdentityConflictError):
            assert_same_provider_effect_intent(intent, changed)


def test_provider_effect_state_machine_exposes_only_structural_transitions() -> None:
    allowed = {
        (ProviderEffectState.PENDING, ProviderEffectTransition.CLAIM): (
            ProviderEffectState.CLAIMED
        ),
        (
            ProviderEffectState.CLAIMED,
            ProviderEffectTransition.RELEASE_BEFORE_SEND,
        ): ProviderEffectState.PENDING,
        (
            ProviderEffectState.SEND_STARTED,
            ProviderEffectTransition.RECORD_AMBIGUOUS,
        ): ProviderEffectState.QUARANTINED_UNKNOWN,
        (
            ProviderEffectState.QUARANTINED_UNKNOWN,
            ProviderEffectTransition.BEGIN_RECONCILIATION,
        ): ProviderEffectState.RECONCILING,
        (
            ProviderEffectState.QUARANTINED_UNKNOWN,
            ProviderEffectTransition.ESCALATE_MANUAL_REVIEW,
        ): ProviderEffectState.MANUAL_REVIEW_UNKNOWN,
        (
            ProviderEffectState.MANUAL_REVIEW_UNKNOWN,
            ProviderEffectTransition.RESUME_RECONCILIATION,
        ): ProviderEffectState.RECONCILING,
    }

    for state in ProviderEffectState:
        for transition in ProviderEffectTransition:
            expected = allowed.get((state, transition))
            if expected is None:
                with pytest.raises(ProviderEffectStateConflictError):
                    transition_provider_effect_state(state, transition)
            else:
                assert transition_provider_effect_state(state, transition) is expected


def test_provider_effect_send_requires_admission_and_advances_attempt_fence() -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    admission_1, attempt_1, claim_authority = _send_attempt(intent, capability)

    with pytest.raises(ProviderEffectStateConflictError):
        transition_provider_effect_state(
            ProviderEffectState.CLAIMED,
            ProviderEffectTransition.BEGIN_SEND,
        )
    with pytest.raises(ProviderEffectAdmissionError):
        begin_provider_effect_send(
            ProviderEffectState.CLAIMED,
            intent,
            capability,
            admission_1,
            claim_authority,
            started_at_unix_ms=1_021,
        )
    with pytest.raises(ProviderEffectAdmissionError):
        begin_provider_effect_send(
            ProviderEffectState.CLAIMED,
            intent,
            capability,
            admission_1,
            claim_authority,
            started_at_unix_ms=1_120,
            previous_send_attempt=attempt_1,
        )

    terminal_evidence = _evidence(
        intent,
        capability,
        send_attempt=attempt_1,
        outcome=ProviderReconciliationOutcome.NOT_COMMITTED,
    )
    assert (
        apply_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission_1,
            attempt_1,
            terminal_evidence,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )
        is ProviderEffectState.CONFIRMED_NOT_COMMITTED
    )

    admission_2 = _admission(
        intent,
        capability,
        previous_send_attempt=attempt_1,
        admitted_at_unix_ms=1_110,
        claim_authority=claim_authority,
    )
    state_2, attempt_2 = begin_provider_effect_send(
        ProviderEffectState.CLAIMED,
        intent,
        capability,
        admission_2,
        claim_authority,
        started_at_unix_ms=1_120,
        previous_send_attempt=attempt_1,
    )

    assert state_2 is ProviderEffectState.SEND_STARTED
    assert attempt_2.claim_generation > attempt_1.claim_generation
    assert attempt_2.claim_fencing_token > attempt_1.claim_fencing_token
    with pytest.raises(ProviderEffectAdmissionError):
        begin_provider_effect_send(
            ProviderEffectState.CLAIMED,
            intent,
            capability,
            admission_2,
            claim_authority,
            started_at_unix_ms=1_120,
            previous_send_attempt=attempt_1,
        )


def test_provider_effect_retry_is_terminally_safe_and_identity_bound() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)

    for state in (
        ProviderEffectState.CONFIRMED_NOT_COMMITTED,
        ProviderEffectState.CONFIRMED_CANCELLED,
    ):
        assert (
            retry_same_provider_effect_intent(state, intent, intent)
            is ProviderEffectState.PENDING
        )

    with pytest.raises(ProviderEffectIdentityConflictError):
        retry_same_provider_effect_intent(
            ProviderEffectState.CONFIRMED_NOT_COMMITTED,
            intent,
            replace(intent, idempotency_key="different-key"),
        )
    with pytest.raises(ProviderEffectStateConflictError):
        retry_same_provider_effect_intent(
            ProviderEffectState.CONFIRMED_COMMITTED,
            intent,
            intent,
        )
    with pytest.raises(ProviderEffectStateConflictError):
        transition_provider_effect_state(
            ProviderEffectState.CONFIRMED_NOT_COMMITTED,
            ProviderEffectTransition.RETRY_SAME_INTENT,
        )


@pytest.mark.parametrize(
    ("method", "outcome", "valid"),
    [
        (method, outcome, outcome in allowed)
        for method, allowed in (
            (
                ProviderReconciliationMethod.ATOMIC_DEDUPE_REPLAY,
                {
                    ProviderReconciliationOutcome.COMMITTED,
                    ProviderReconciliationOutcome.UNKNOWN,
                },
            ),
            (
                ProviderReconciliationMethod.STATUS_LOOKUP,
                {
                    ProviderReconciliationOutcome.COMMITTED,
                    ProviderReconciliationOutcome.NOT_COMMITTED,
                    ProviderReconciliationOutcome.UNKNOWN,
                },
            ),
            (
                ProviderReconciliationMethod.CONFIRMED_CANCELLATION,
                {
                    ProviderReconciliationOutcome.COMMITTED,
                    ProviderReconciliationOutcome.CANCELLED_CONFIRMED,
                    ProviderReconciliationOutcome.UNKNOWN,
                },
            ),
        )
        for outcome in ProviderReconciliationOutcome
    ],
)
def test_provider_reconciliation_method_has_a_closed_outcome_set(
    method: ProviderReconciliationMethod,
    outcome: ProviderReconciliationOutcome,
    valid: bool,
) -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY,
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY,
        cancellation=ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY,
    )
    intent = _intent(capability)

    if valid:
        assert (
            _evidence(intent, capability, method=method, outcome=outcome).outcome
            is outcome
        )
    else:
        with pytest.raises(ProviderEffectContractError):
            _evidence(intent, capability, method=method, outcome=outcome)


def test_provider_reconciliation_evidence_binds_intent_capability_and_time() -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY,
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY,
        cancellation=ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY,
    )
    intent = _intent(capability)
    admission, send_attempt, claim_authority = _send_attempt(intent, capability)
    evidence = _evidence(intent, capability, send_attempt=send_attempt)
    validate_provider_reconciliation_evidence(
        ProviderEffectState.SEND_STARTED,
        intent,
        capability,
        admission,
        send_attempt,
        evidence,
        claim_authority,
        _VERIFIER_AUTHORITY,
    )

    mutations = [
        replace(evidence, effect_id="effect-8"),
        replace(evidence, intent_digest=_DIGEST_C),
        replace(evidence, capability_snapshot_digest=_DIGEST_C),
        replace(evidence, send_attempt_digest=_DIGEST_C),
        replace(evidence, observed_at_unix_ms=1_019),
        replace(evidence, provider_correlation_id="other-correlation"),
    ]
    for changed in mutations:
        with pytest.raises(ProviderEffectEvidenceError):
            validate_provider_reconciliation_evidence(
                ProviderEffectState.SEND_STARTED,
                intent,
                capability,
                admission,
                send_attempt,
                changed,
                claim_authority,
                _VERIFIER_AUTHORITY,
            )


def test_provider_reconciliation_requires_authenticated_content() -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    admission, send_attempt, claim_authority = _send_attempt(intent, capability)
    evidence = _evidence(intent, capability, send_attempt=send_attempt)

    with pytest.raises(ProviderEffectContractError):
        replace(evidence, provider_evidence_digest=_DIGEST_A)

    forged_provider_body = {
        "adapterReleaseDigest": capability.adapter_release_digest,
        "effectId": intent.effect_id,
        "method": evidence.method.value,
        "outcome": ProviderReconciliationOutcome.NOT_COMMITTED.value,
        "sendAttemptDigest": send_attempt.digest,
    }
    forged = replace(
        evidence,
        provider_evidence_json=canonical_dumps(forged_provider_body),
        provider_evidence_digest=canonical_hash(forged_provider_body),
    )
    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            forged,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )
    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            forged,
            claim_authority,
            _CopycatEvidenceVerifier(),  # type: ignore[arg-type]
        )
    alternate_verifier = _AlternateEvidenceVerifier()
    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            replace(evidence, verifier_id=alternate_verifier.verifier_id),
            claim_authority,
            _VERIFIER_AUTHORITY,
        )
    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            evidence,
            claim_authority,
            _VerifierAuthority(_EvidenceVerifier(accepts=False)),
        )
    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            replace(evidence, verifier_id="untrusted.verifier"),
            claim_authority,
            _VERIFIER_AUTHORITY,
        )


@pytest.mark.parametrize(
    ("method", "outcome"),
    [
        (
            ProviderReconciliationMethod.STATUS_LOOKUP,
            ProviderReconciliationOutcome.NOT_COMMITTED,
        ),
        (
            ProviderReconciliationMethod.CONFIRMED_CANCELLATION,
            ProviderReconciliationOutcome.CANCELLED_CONFIRMED,
        ),
    ],
)
def test_prior_terminal_evidence_cannot_settle_a_retried_send(
    method: ProviderReconciliationMethod,
    outcome: ProviderReconciliationOutcome,
) -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY,
        cancellation=ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY,
    )
    intent = _intent(capability)
    admission_1, attempt_1, claim_authority = _send_attempt(intent, capability)
    evidence_1 = _evidence(
        intent,
        capability,
        send_attempt=attempt_1,
        method=method,
        outcome=outcome,
    )
    terminal_state = apply_provider_reconciliation_evidence(
        ProviderEffectState.SEND_STARTED,
        intent,
        capability,
        admission_1,
        attempt_1,
        evidence_1,
        claim_authority,
        _VERIFIER_AUTHORITY,
    )
    assert (
        retry_same_provider_effect_intent(terminal_state, intent, intent)
        is ProviderEffectState.PENDING
    )

    admission_2, attempt_2, _ = _send_attempt(
        intent,
        capability,
        previous_send_attempt=attempt_1,
        claim_authority=claim_authority,
    )
    with pytest.raises(ProviderEffectEvidenceError):
        apply_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission_1,
            attempt_1,
            evidence_1,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )
    with pytest.raises(ProviderEffectEvidenceError):
        apply_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission_2,
            attempt_2,
            evidence_1,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )

    evidence_before_attempt_2 = _evidence(
        intent,
        capability,
        send_attempt=attempt_2,
        method=method,
        outcome=outcome,
        observed_at_unix_ms=attempt_2.started_at_unix_ms - 1,
    )
    with pytest.raises(ProviderEffectEvidenceError):
        apply_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission_2,
            attempt_2,
            evidence_before_attempt_2,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )


def test_provider_reconciliation_rejects_a_method_missing_from_the_snapshot() -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    admission, send_attempt, claim_authority = _send_attempt(intent, capability)
    evidence = _evidence(
        intent,
        capability,
        send_attempt=send_attempt,
        method=ProviderReconciliationMethod.CONFIRMED_CANCELLATION,
        outcome=ProviderReconciliationOutcome.CANCELLED_CONFIRMED,
    )

    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            evidence,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )


@pytest.mark.parametrize(
    ("status_lookup", "cancellation", "method", "outcome"),
    [
        (
            ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID,
            ProviderCancellation.NONE,
            ProviderReconciliationMethod.STATUS_LOOKUP,
            ProviderReconciliationOutcome.COMMITTED,
        ),
        (
            ProviderStatusLookup.NONE,
            ProviderCancellation.CONFIRMED_BY_PREBOUND_CORRELATION_ID,
            ProviderReconciliationMethod.CONFIRMED_CANCELLATION,
            ProviderReconciliationOutcome.CANCELLED_CONFIRMED,
        ),
    ],
)
def test_provider_reconciliation_requires_the_prebound_correlation_identity(
    status_lookup: ProviderStatusLookup,
    cancellation: ProviderCancellation,
    method: ProviderReconciliationMethod,
    outcome: ProviderReconciliationOutcome,
) -> None:
    capability = _capability(
        status_lookup=status_lookup,
        cancellation=cancellation,
    )
    intent = _intent(capability)
    admission, send_attempt, claim_authority = _send_attempt(intent, capability)
    evidence = _evidence(
        intent,
        capability,
        send_attempt=send_attempt,
        method=method,
        outcome=outcome,
        correlation_id=None,
    )

    with pytest.raises(ProviderEffectEvidenceError):
        validate_provider_reconciliation_evidence(
            ProviderEffectState.SEND_STARTED,
            intent,
            capability,
            admission,
            send_attempt,
            evidence,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )


@pytest.mark.parametrize(
    ("method", "outcome", "expected"),
    [
        (
            ProviderReconciliationMethod.STATUS_LOOKUP,
            ProviderReconciliationOutcome.UNKNOWN,
            ProviderEffectState.QUARANTINED_UNKNOWN,
        ),
        (
            ProviderReconciliationMethod.ATOMIC_DEDUPE_REPLAY,
            ProviderReconciliationOutcome.COMMITTED,
            ProviderEffectState.CONFIRMED_COMMITTED,
        ),
        (
            ProviderReconciliationMethod.STATUS_LOOKUP,
            ProviderReconciliationOutcome.NOT_COMMITTED,
            ProviderEffectState.CONFIRMED_NOT_COMMITTED,
        ),
        (
            ProviderReconciliationMethod.CONFIRMED_CANCELLATION,
            ProviderReconciliationOutcome.CANCELLED_CONFIRMED,
            ProviderEffectState.CONFIRMED_CANCELLED,
        ),
    ],
)
def test_provider_evidence_controls_terminal_and_quarantine_transitions(
    method: ProviderReconciliationMethod,
    outcome: ProviderReconciliationOutcome,
    expected: ProviderEffectState,
) -> None:
    capability = _capability(
        deduplication=ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY,
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY,
        cancellation=ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY,
    )
    intent = _intent(capability)
    for current in (
        ProviderEffectState.SEND_STARTED,
        ProviderEffectState.RECONCILING,
    ):
        admission, send_attempt, claim_authority = _send_attempt(
            intent,
            capability,
        )
        evidence = _evidence(
            intent,
            capability,
            send_attempt=send_attempt,
            method=method,
            outcome=outcome,
        )

        assert (
            apply_provider_reconciliation_evidence(
                current,
                intent,
                capability,
                admission,
                send_attempt,
                evidence,
                claim_authority,
                _VERIFIER_AUTHORITY,
            )
            is expected
        )


def test_unknown_provider_evidence_cannot_escape_quarantine() -> None:
    capability = _capability(
        status_lookup=ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY
    )
    intent = _intent(capability)
    admission, send_attempt, claim_authority = _send_attempt(intent, capability)
    evidence = _evidence(
        intent,
        capability,
        send_attempt=send_attempt,
        outcome=ProviderReconciliationOutcome.UNKNOWN,
    )

    assert (
        apply_provider_reconciliation_evidence(
            ProviderEffectState.RECONCILING,
            intent,
            capability,
            admission,
            send_attempt,
            evidence,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )
        is ProviderEffectState.QUARANTINED_UNKNOWN
    )
    with pytest.raises(ProviderEffectStateConflictError):
        retry_same_provider_effect_intent(
            ProviderEffectState.QUARANTINED_UNKNOWN,
            intent,
            intent,
        )
    with pytest.raises(ProviderEffectStateConflictError):
        apply_provider_reconciliation_evidence(
            ProviderEffectState.QUARANTINED_UNKNOWN,
            intent,
            capability,
            admission,
            send_attempt,
            evidence,
            claim_authority,
            _VERIFIER_AUTHORITY,
        )
