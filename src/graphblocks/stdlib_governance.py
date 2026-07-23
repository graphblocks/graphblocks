from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Any

from .canonical import (
    MAX_CANONICAL_JSON_DEPTH,
    canonical_dumps,
    canonical_hash,
    canonical_loads,
)
from .diagnostics import Diagnostic
from .documents import ArtifactRef
from .evaluation import (
    CheckResult,
    EvidenceRef,
    GateConstraint,
    MetricObservation,
    ResourceSnapshotRef,
    ResultBundle,
    ReviewRecord,
    RunProvenance,
    TypedValueRef,
    evaluate_gate,
)
from .policy import PrincipalRef
from .review import ReviewRequest


def _unicode_string(owner: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{owner} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TypeError(
            f"{owner} must contain only Unicode scalar values"
        ) from error
    return value


def _mapping(owner: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{owner} must be a mapping")
    try:
        items = tuple(value.items())
    except Exception as error:
        raise TypeError(f"{owner} must be a stable mapping") from error
    snapshot: dict[str, Any] = {}
    for key, item in items:
        key = _unicode_string(f"{owner} keys", key)
        if key in snapshot:
            raise TypeError(f"{owner} keys must be unique")
        snapshot[key] = item
    return snapshot


def _sequence(owner: str, value: object) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Sequence):
        raise TypeError(f"{owner} must be a sequence")
    try:
        return list(value)
    except Exception as error:
        raise TypeError(f"{owner} must be a stable sequence") from error


def _flatten_records(owner: str, value: object) -> list[Any]:
    flattened: list[Any] = []

    def visit(current: object, depth: int, active: set[int]) -> None:
        if not isinstance(current, (list, tuple)):
            flattened.append(current)
            return
        if depth > MAX_CANONICAL_JSON_DEPTH:
            raise TypeError(
                f"{owner} nesting must not exceed "
                f"{MAX_CANONICAL_JSON_DEPTH} levels"
            )
        identity = id(current)
        if identity in active:
            raise TypeError(f"{owner} must not be recursive")
        active.add(identity)
        try:
            for item in _sequence(owner, current):
                visit(item, depth + 1, active)
        finally:
            active.remove(identity)

    visit(value, 0, set())
    return flattened


def _exact_string(owner: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TypeError(f"{owner} must be an exact non-empty string")
    value = _unicode_string(owner, value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise TypeError(f"{owner} must not contain control characters")
    return value


def _aliased_value(
    owner: str,
    record: Mapping[str, Any],
    camel_key: str,
    snake_key: str,
    default: Any = None,
) -> Any:
    if camel_key in record and snake_key in record:
        raise ValueError(
            f"{owner} must not contain both {camel_key!r} and {snake_key!r}"
        )
    if camel_key in record:
        return record[camel_key]
    return record.get(snake_key, default)


def _json_value(value: object) -> object:
    def convert(current: object, depth: int, active: set[int]) -> object:
        if depth > MAX_CANONICAL_JSON_DEPTH:
            raise TypeError(
                "governance JSON nesting must not exceed "
                f"{MAX_CANONICAL_JSON_DEPTH} levels"
            )
        if isinstance(current, Decimal):
            return str(current)
        if isinstance(current, str):
            return _unicode_string("governance JSON string", current)
        if is_dataclass(current) and not isinstance(current, type):
            identity = id(current)
            if identity in active:
                raise TypeError("governance JSON values must not be recursive")
            active.add(identity)
            try:
                return {
                    field.name: convert(
                        getattr(current, field.name),
                        depth + 1,
                        active,
                    )
                    for field in fields(current)
                }
            finally:
                active.remove(identity)
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in active:
                raise TypeError("governance JSON values must not be recursive")
            active.add(identity)
            try:
                return {
                    key: convert(item, depth + 1, active)
                    for key, item in _mapping(
                        "governance JSON object",
                        current,
                    ).items()
                }
            finally:
                active.remove(identity)
        if isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in active:
                raise TypeError("governance JSON values must not be recursive")
            active.add(identity)
            try:
                return [
                    convert(item, depth + 1, active)
                    for item in _sequence("governance JSON array", current)
                ]
            finally:
                active.remove(identity)
        return current

    return convert(value, 0, set())


def _resource_ref(value: object, *, fallback_id: str) -> ResourceSnapshotRef:
    if isinstance(value, ResourceSnapshotRef):
        return value
    if value is None:
        return ResourceSnapshotRef(fallback_id, canonical_hash({"resource_id": fallback_id}))
    if isinstance(value, Mapping):
        item = _mapping("resource", value)
        resource_id = item.get("resource_id", item.get("resourceId", item.get("id", fallback_id)))
        digest = item.get("digest", canonical_hash(_json_value(item)))
        return ResourceSnapshotRef(
            resource_id=_exact_string("resource resourceId", resource_id),
            digest=_exact_string("resource digest", digest),
            resource_kind=item.get("resource_kind", item.get("resourceKind")),
            uri=item.get("uri"),
            metadata=_mapping("resource metadata", item.get("metadata", {})),
        )
    return ResourceSnapshotRef(fallback_id, canonical_hash(_json_value(value)), metadata={"value": value})


def _check_result(value: object, *, subject: ResourceSnapshotRef) -> CheckResult:
    if isinstance(value, CheckResult):
        return value
    item = _mapping("check result", value)
    check_id = item.get("check_id", item.get("checkId", item.get("id")))
    raw_subject = item.get("subject", subject)
    diagnostics = []
    for raw_diagnostic in _sequence("check diagnostics", item.get("diagnostics", [])):
        if isinstance(raw_diagnostic, Diagnostic):
            diagnostics.append(raw_diagnostic)
            continue
        diagnostic = _mapping("diagnostic", raw_diagnostic)
        diagnostics.append(
            Diagnostic(
                code=_exact_string("diagnostic code", diagnostic.get("code")),
                message=_exact_string("diagnostic message", diagnostic.get("message")),
                path=_exact_string("diagnostic path", diagnostic.get("path", "$")),
                severity=_exact_string(  # type: ignore[arg-type]
                    "diagnostic severity",
                    diagnostic.get("severity", "error"),
                ),
            )
        )
    return CheckResult(
        check_id=_exact_string("check result checkId", check_id),
        subject=_resource_ref(raw_subject, fallback_id=subject.resource_id),
        status=_exact_string("check result status", item.get("status")),  # type: ignore[arg-type]
        diagnostics=diagnostics,
        tool=_mapping("check result tool", item.get("tool", {})),
    )


def _metric(value: object, *, subject: ResourceSnapshotRef) -> MetricObservation:
    if isinstance(value, MetricObservation):
        return value
    item = _mapping("metric", value)
    raw_subject = item.get("subject")
    return MetricObservation(
        name=_exact_string("metric name", item.get("name")),
        value=item.get("value"),
        unit=item.get("unit"),
        direction=_exact_string(  # type: ignore[arg-type]
            "metric direction",
            item.get("direction", "informational"),
        ),
        baseline_value=item.get("baseline_value", item.get("baselineValue")),
        subject=None if raw_subject is None else _resource_ref(raw_subject, fallback_id=subject.resource_id),
        evaluator=None if item.get("evaluator") is None else _mapping("metric evaluator", item["evaluator"]),
    )


def structured_generate_block(
    inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Return configured/provider-produced structured JSON without pretending to call a model.

    Production providers can place their already schema-validated JSON in ``inputs.response``.
    Tests and offline graphs can use ``config.response``. This block intentionally fails when
    neither is present instead of fabricating a model answer.
    """

    inputs = _mapping("model.structured_generate@1 inputs", inputs)
    config = _mapping("model.structured_generate@1 config", config)
    context = _mapping("model.structured_generate@1 context", context)
    output_schema = _exact_string(
        "model.structured_generate@1 config.outputSchema",
        _aliased_value(
            "model.structured_generate@1 config",
            config,
            "outputSchema",
            "output_schema",
        ),
    )
    raw = inputs.get("response", config.get("response"))
    if raw is None:
        raise ValueError(
            "model.structured_generate@1 requires inputs.response or config.response from a provider or test fixture"
        )
    if isinstance(raw, str):
        raw = canonical_loads(raw)
    if not isinstance(raw, (Mapping, list)):
        raise TypeError("model.structured_generate@1 response must be a JSON object or array")
    value = canonical_loads(canonical_dumps(raw))
    result: dict[str, Any] = {
        "value": value,
        "response": value,
        "items": [],
        "schemaId": output_schema,
        "schemaRef": output_schema,
        "contentDigest": canonical_hash(value),
    }
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        result["items"] = value["items"]
    elif isinstance(value, list):
        result["items"] = value
    if isinstance(value, dict):
        for output_name in ("questions", "scores"):
            if output_name in value:
                result[output_name] = value[output_name]
    return result


def check_run_suite_block(
    inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Normalize bound check outcomes; omitted outcomes are inconclusive, never passing."""

    inputs = _mapping("check.run_suite@1 inputs", inputs)
    config = _mapping("check.run_suite@1 config", config)
    context = _mapping("check.run_suite@1 context", context)
    subject = _resource_ref(inputs.get("subject"), fallback_id=f"run:{context.get('run_id', 'local')}:subject")
    configured = _sequence("check.run_suite@1 config.checks", config.get("checks", []))
    outcomes = _mapping("check.run_suite@1 config.outcomes", config.get("outcomes", {}))
    results: list[CheckResult] = []
    for index, configured_check in enumerate(configured):
        if isinstance(configured_check, str):
            check_id = _exact_string(f"check.run_suite@1 config.checks[{index}]", configured_check)
            outcome = outcomes.get(check_id)
            if outcome is None:
                outcome = {
                    "status": "inconclusive",
                    "diagnostics": [
                        {
                            "code": "CHECK_IMPLEMENTATION_UNBOUND",
                            "message": f"check {check_id!r} has no configured outcome or bound implementation",
                            "severity": "warning",
                        }
                    ],
                }
            if isinstance(outcome, str):
                outcome = {"status": outcome}
            item = {"checkId": check_id, **_mapping(f"check outcome {check_id}", outcome)}
        else:
            item = _mapping(f"check.run_suite@1 config.checks[{index}]", configured_check)
        results.append(_check_result(item, subject=subject))
        if config.get("stopOnFailure", config.get("stop_on_failure", False)) is True and results[-1].status != "passed":
            break
    contracts = [_json_value(result) for result in results]
    diagnostics = [item for result in results for item in (_json_value(entry) for entry in result.diagnostics)]
    passed = bool(results) and all(result.status == "passed" for result in results)
    return {
        "results": contracts,
        "checks": contracts,
        "diagnostics": diagnostics,
        "passed": passed,
        "hardGatePassed": passed,
    }


def gate_evaluate_block(
    inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    inputs = _mapping("gate.evaluate@1 inputs", inputs)
    config = _mapping("gate.evaluate@1 config", config)
    context = _mapping("gate.evaluate@1 context", context)
    raw_checks = _flatten_records("gate.evaluate@1 inputs.checks", inputs.get("checks", []))
    explicit_subject = inputs.get("subject")
    if explicit_subject is None and raw_checks and isinstance(raw_checks[0], Mapping):
        explicit_subject = raw_checks[0].get("subject")
    subject = _resource_ref(explicit_subject, fallback_id=f"run:{context.get('run_id', 'local')}:gate-subject")
    checks = [_check_result(item, subject=subject) for item in raw_checks]
    metrics = [
        _metric(item, subject=subject)
        for item in _sequence("gate.evaluate@1 inputs.metrics", inputs.get("metrics", []))
    ]
    raw_required = config.get("requiredChecks", config.get("required_check_ids", config.get("hardConstraints")))
    required = None if raw_required is None else [
        _exact_string("gate.evaluate@1 required check", item)
        for item in _sequence("gate.evaluate@1 required checks", raw_required)
    ]
    constraints = []
    for raw_constraint in _sequence("gate.evaluate@1 config.constraints", config.get("constraints", [])):
        item = _mapping("gate constraint", raw_constraint)
        constraints.append(
            GateConstraint(
                metric_name=_exact_string("gate constraint metric", item.get("metric", item.get("metricName"))),
                operator=_exact_string("gate constraint operator", item.get("operator")),  # type: ignore[arg-type]
                threshold=item.get("threshold"),  # type: ignore[arg-type]
            )
        )
    result = evaluate_gate(
        _exact_string(
            "gate.evaluate@1 config.gateId",
            _aliased_value(
                "gate.evaluate@1 config",
                config,
                "gateId",
                "gate_id",
                context.get("node", "gate"),
            ),
        ),
        subject,
        checks=checks,
        metrics=metrics,
        required_check_ids=required,
        constraints=constraints,
        policy_ref=config.get("policyRef", config.get("policy_ref")),
    )
    contract = _json_value(result)
    return {
        "result": contract,
        "decision": result.decision,
        "passed": result.decision == "pass",
        "violations": list(result.violated_constraints),
    }


def review_request_block(
    inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Create a deterministic review work item; durable wait/resume belongs to the host application."""

    inputs = _mapping("review.request@1 inputs", inputs)
    config = _mapping("review.request@1 config", config)
    context = _mapping("review.request@1 context", context)
    subject = _resource_ref(inputs.get("subject"), fallback_id=f"run:{context.get('run_id', 'local')}:review-subject")
    raw_scopes = config.get("scopes")
    if raw_scopes is None:
        raw_scopes = [config.get("scope", "review")]
    scopes = tuple(
        sorted(
            {
                _exact_string("review.request@1 scope", item)
                for item in _sequence("review.request@1 config.scopes", raw_scopes)
            }
        )
    )
    raw_principal = inputs.get("requestedBy", inputs.get("requested_by", config.get("requestedBy", {})))
    if isinstance(raw_principal, PrincipalRef):
        principal = raw_principal
    else:
        principal_item = _mapping("review.request@1 requestedBy", raw_principal)
        principal = PrincipalRef(
            principal_id=_exact_string(
                "review.request@1 requestedBy.principalId",
                principal_item.get("principalId", principal_item.get("principal_id", "graphblocks-runtime")),
            ),
            tenant_id=principal_item.get("tenantId", principal_item.get("tenant_id")),
            groups=tuple(
                _sequence(
                    "review.request@1 requestedBy.groups",
                    principal_item.get("groups", ()),
                )
            ),
            roles=tuple(
                _sequence(
                    "review.request@1 requestedBy.roles",
                    principal_item.get("roles", ()),
                )
            ),
            attributes=_mapping("review.request@1 requestedBy.attributes", principal_item.get("attributes", {})),
        )
    metadata = _mapping("review.request@1 config.metadata", config.get("metadata", {}))
    if "gate" in inputs:
        metadata["gate"] = _json_value(inputs["gate"])
    created_at = _exact_string(
        "review.request@1 config.createdAt",
        config.get("createdAt", config.get("created_at", "1970-01-01T00:00:00Z")),
    )
    request_id = config.get("requestId", config.get("request_id"))
    if request_id is None:
        request_id = "review-" + canonical_hash(
            {"run_id": context.get("run_id"), "subject": subject.digest, "scopes": scopes, "metadata": metadata}
        ).removeprefix("sha256:")[:24]
    request = ReviewRequest(
        request_id=_exact_string("review.request@1 requestId", request_id),
        subject=subject,
        requested_by=principal,
        required_scopes=scopes,
        created_at=created_at,
        metadata=metadata,
    )
    raw_review = inputs.get("review")
    if raw_review is not None:
        review_item = _mapping("review.request@1 input review", raw_review)
        decision = _exact_string(
            "review.request@1 review decision",
            review_item.get("decision"),
        )
        if decision not in {
            "accept",
            "accept_with_conditions",
            "revise",
            "reject",
        }:
            raise ValueError(
                "review.request@1 review decision must be accept, accept_with_conditions, revise, or reject"
            )
        review_subject_digest = review_item.get(
            "subjectDigest",
            review_item.get("subject_digest", subject.digest),
        )
        if review_subject_digest != subject.digest:
            raise ValueError(
                "review.request@1 review subject digest must match the requested subject"
            )
        review_scope = _exact_string(
            "review.request@1 review scope",
            review_item.get("scope", scopes[0] if scopes else None),
        )
        if review_scope not in scopes:
            raise ValueError(
                "review.request@1 review scope was not requested"
            )
        reviewer_item = _mapping(
            "review.request@1 review reviewer",
            review_item.get("reviewer", {}),
        )
        reviewer = PrincipalRef(
            principal_id=_exact_string(
                "review.request@1 review reviewer principalId",
                reviewer_item.get("principalId", reviewer_item.get("principal_id")),
            ),
            tenant_id=reviewer_item.get("tenantId", reviewer_item.get("tenant_id")),
            groups=tuple(
                _sequence(
                    "review.request@1 review reviewer groups",
                    reviewer_item.get("groups", ()),
                )
            ),
            roles=tuple(
                _sequence(
                    "review.request@1 review reviewer roles",
                    reviewer_item.get("roles", ()),
                )
            ),
            attributes=_mapping(
                "review.request@1 review reviewer attributes",
                reviewer_item.get("attributes", {}),
            ),
        )
        credential_refs = _sequence(
            "review.request@1 review credentialRefs",
            review_item.get(
                "credentialRefs",
                review_item.get("credential_refs", []),
            ),
        )
        required_credential = config.get(
            "requiredCredential",
            config.get("required_credential"),
        )
        if (
            isinstance(required_credential, str)
            and required_credential not in credential_refs
        ):
            raise ValueError(
                "review.request@1 review is missing the required credential"
            )
        review = ReviewRecord(
            review_id=_exact_string(
                "review.request@1 review reviewId",
                review_item.get("reviewId", review_item.get("review_id")),
            ),
            subject=subject,
            subject_digest=subject.digest,
            scope=review_scope,
            reviewer=reviewer,
            decision=decision,  # type: ignore[arg-type]
            comments=_sequence(
                "review.request@1 review comments",
                review_item.get("comments", []),
            ),
            credential_refs=credential_refs,
            created_at=_exact_string(
                "review.request@1 review createdAt",
                review_item.get("createdAt", review_item.get("created_at")),
            ),
        )
        accepted = review.decision in {"accept", "accept_with_conditions"}
        return {
            "request": _json_value(request),
            "requestDigest": request.content_digest(),
            "status": review.decision,
            "pending": False,
            "accepted": accepted,
            "approved": accepted,
            "record": _json_value(review),
            "waitMode": None,
        }
    return {
        "request": _json_value(request),
        "requestDigest": request.content_digest(),
        "status": "pending",
        "pending": True,
        "accepted": False,
        "approved": False,
        "record": None,
        "waitMode": "application_event",
    }


def result_bundle_block(
    inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    inputs = _mapping("result.bundle@1 inputs", inputs)
    config = _mapping("result.bundle@1 config", config)
    context = _mapping("result.bundle@1 context", context)
    run_id = _exact_string("result.bundle@1 runId", config.get("runId", context.get("run_id", "local-run")))
    release_id = _exact_string("result.bundle@1 releaseId", config.get("releaseId", "local-release"))
    raw_inputs = _sequence("result.bundle@1 inputs.inputs", inputs.get("inputs", []))
    resource_inputs = [
        _resource_ref(item, fallback_id=f"{run_id}:input:{index}") for index, item in enumerate(raw_inputs)
    ]
    raw_outputs = _sequence("result.bundle@1 inputs.outputs", inputs.get("outputs", []))
    typed_outputs: list[TypedValueRef] = []
    for index, item in enumerate(raw_outputs):
        if isinstance(item, TypedValueRef):
            typed_outputs.append(item)
            continue
        raw_item = (
            _mapping("result bundle output", item)
            if isinstance(item, Mapping)
            else {}
        )
        schema_id = raw_item.get(
            "schemaId",
            raw_item.get("schema_id", config.get("outputSchema", "graphblocks.core/JsonValue@1")),
        )
        schema_version = raw_item.get("schemaVersion", raw_item.get("schema_version", 1))
        typed_outputs.append(
            TypedValueRef(
                value_id=_exact_string(
                    "result bundle output valueId",
                    raw_item.get("valueId", raw_item.get("value_id", f"{run_id}:output:{index}")),
                ),
                schema_id=_exact_string("result bundle output schemaId", schema_id),
                schema_version=schema_version,
                digest=_exact_string(
                    "result bundle output digest",
                    raw_item.get("digest", canonical_hash(_json_value(item))),
                ),
                encoding=_exact_string("result bundle output encoding", raw_item.get("encoding", "json")),
            )
        )
    subject = resource_inputs[0] if resource_inputs else _resource_ref(None, fallback_id=f"{run_id}:result")
    checks = [
        _check_result(item, subject=subject)
        for item in _flatten_records("result.bundle@1 inputs.checks", inputs.get("checks", []))
    ]
    metrics = [
        _metric(item, subject=subject)
        for item in _sequence("result.bundle@1 inputs.metrics", inputs.get("metrics", []))
    ]
    artifacts: list[ArtifactRef] = []
    for raw_artifact in _flatten_records("result.bundle@1 inputs.artifacts", inputs.get("artifacts", [])):
        if isinstance(raw_artifact, ArtifactRef):
            artifacts.append(raw_artifact)
            continue
        artifact = _mapping("result bundle artifact", raw_artifact)
        artifacts.append(
            ArtifactRef(
                artifact_id=_exact_string(
                    "result bundle artifact artifactId",
                    artifact.get("artifactId", artifact.get("artifact_id")),
                ),
                uri=_exact_string("result bundle artifact uri", artifact.get("uri")),
                media_type=artifact.get("mediaType", artifact.get("media_type")),
                size_bytes=artifact.get("sizeBytes", artifact.get("size_bytes")),
                checksum=artifact.get("checksum"),
                etag=artifact.get("etag"),
                version=artifact.get("version"),
                filename=artifact.get("filename"),
                metadata=_mapping("result bundle artifact metadata", artifact.get("metadata", {})),
            )
        )
    evidence: list[EvidenceRef] = []
    for index, raw_evidence in enumerate(
        _flatten_records("result.bundle@1 inputs.evidence", inputs.get("evidence", []))
    ):
        if isinstance(raw_evidence, EvidenceRef):
            evidence.append(raw_evidence)
            continue
        item = _mapping("result bundle evidence", raw_evidence)
        raw_source = item.get("source", item)
        evidence.append(
            EvidenceRef(
                evidence_id=_exact_string(
                    "result bundle evidence evidenceId",
                    item.get("evidenceId", item.get("evidence_id", f"{run_id}:evidence:{index}")),
                ),
                source=_resource_ref(raw_source, fallback_id=f"{run_id}:evidence-source:{index}"),
                evidence_kind=_exact_string(
                    "result bundle evidence evidenceKind",
                    item.get("evidenceKind", item.get("evidence_kind", item.get("kind", "reference"))),
                ),
                metadata=_mapping("result bundle evidence metadata", item.get("metadata", {})),
            )
        )
    diagnostics: list[Diagnostic] = []
    for raw_diagnostic in _flatten_records(
        "result.bundle@1 inputs.diagnostics", inputs.get("diagnostics", [])
    ):
        if isinstance(raw_diagnostic, Diagnostic):
            diagnostics.append(raw_diagnostic)
            continue
        item = _mapping("result bundle diagnostic", raw_diagnostic)
        diagnostics.append(
            Diagnostic(
                code=_exact_string("result bundle diagnostic code", item.get("code")),
                message=_exact_string("result bundle diagnostic message", item.get("message")),
                path=_exact_string(
                    "result bundle diagnostic path",
                    item.get("path", "$"),
                ),
                severity=_exact_string(  # type: ignore[arg-type]
                    "result bundle diagnostic severity",
                    item.get("severity", "error"),
                ),
            )
        )
    reviews: list[ReviewRecord] = []
    for raw_review in _flatten_records("result.bundle@1 inputs.reviews", inputs.get("reviews", [])):
        if raw_review is None:
            continue
        if isinstance(raw_review, ReviewRecord):
            reviews.append(raw_review)
            continue
        item = _mapping("result bundle review", raw_review)
        review_subject = _resource_ref(item.get("subject"), fallback_id=subject.resource_id)
        raw_reviewer = item.get("reviewer", {})
        if isinstance(raw_reviewer, PrincipalRef):
            reviewer = raw_reviewer
        else:
            reviewer_item = _mapping("result bundle review reviewer", raw_reviewer)
            reviewer = PrincipalRef(
                principal_id=_exact_string(
                    "result bundle review reviewer principalId",
                    reviewer_item.get("principalId", reviewer_item.get("principal_id")),
                ),
                tenant_id=reviewer_item.get("tenantId", reviewer_item.get("tenant_id")),
                groups=tuple(
                    _sequence(
                        "result bundle review reviewer groups",
                        reviewer_item.get("groups", ()),
                    )
                ),
                roles=tuple(
                    _sequence(
                        "result bundle review reviewer roles",
                        reviewer_item.get("roles", ()),
                    )
                ),
                attributes=_mapping("result bundle review reviewer attributes", reviewer_item.get("attributes", {})),
            )
        reviews.append(
            ReviewRecord(
                review_id=_exact_string(
                    "result bundle review reviewId", item.get("reviewId", item.get("review_id"))
                ),
                subject=review_subject,
                subject_digest=_exact_string(
                    "result bundle review subjectDigest",
                    item.get("subjectDigest", item.get("subject_digest", review_subject.digest)),
                ),
                scope=_exact_string("result bundle review scope", item.get("scope")),
                reviewer=reviewer,
                decision=_exact_string("result bundle review decision", item.get("decision")),  # type: ignore[arg-type]
                comments=_sequence(
                    "result bundle review comments",
                    item.get("comments", []),
                ),
                credential_refs=_sequence(
                    "result bundle review credentialRefs",
                    item.get(
                        "credentialRefs",
                        item.get("credential_refs", []),
                    ),
                ),
                created_at=_exact_string(
                    "result bundle review createdAt", item.get("createdAt", item.get("created_at"))
                ),
                invalidated_at=item.get("invalidatedAt", item.get("invalidated_at")),
            )
        )
    provenance = RunProvenance(
        graph_hash=_exact_string("result.bundle@1 graphHash", config.get("graphHash", "sha256:" + "0" * 64)),
        started_at=_exact_string("result.bundle@1 startedAt", config.get("startedAt", "1970-01-01T00:00:00Z")),
        completed_at=config.get("completedAt"),
        release_id=release_id,
        deployment_revision_id=config.get("deploymentRevisionId"),
        physical_plan_hash=config.get("physicalPlanHash"),
        metadata=_mapping("result.bundle@1 provenanceMetadata", config.get("provenanceMetadata", {})),
    )
    bundle_id = config.get("bundleId", config.get("bundle_id"))
    if bundle_id is None:
        bundle_id = "bundle-" + canonical_hash(
            {"run_id": run_id, "outputs": [_json_value(item) for item in typed_outputs]}
        ).removeprefix("sha256:")[:24]
    bundle = ResultBundle(
        bundle_id=_exact_string("result.bundle@1 bundleId", bundle_id),
        run_id=run_id,
        release_id=release_id,
        deployment_revision_id=config.get("deploymentRevisionId"),
        inputs=resource_inputs,
        outputs=typed_outputs,
        artifacts=artifacts,
        diagnostics=diagnostics,
        checks=checks,
        metrics=metrics,
        evidence=evidence,
        reviews=reviews,
        usage_records=_sequence(
            "result.bundle@1 inputs.usage", inputs.get("usage", inputs.get("usageRecords", []))
        ),
        policy_decision_refs=_sequence(
            "result.bundle@1 inputs.policyDecisionRefs", inputs.get("policyDecisionRefs", [])
        ),
        provenance=provenance,
    )
    contract = _json_value(bundle)
    return {"result": contract, "bundle": contract, "contentDigest": bundle.content_digest()}


GOVERNANCE_BLOCKS: dict[str, Any] = {
    "model.structured_generate@1": structured_generate_block,
    "check.run_suite@1": check_run_suite_block,
    "gate.evaluate@1": gate_evaluate_block,
    "review.request@1": review_request_block,
    "result.bundle@1": result_bundle_block,
}


__all__ = [
    "GOVERNANCE_BLOCKS",
    "check_run_suite_block",
    "gate_evaluate_block",
    "result_bundle_block",
    "review_request_block",
    "structured_generate_block",
]
