from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json

from graphblocks import (
    ContentPart,
    OutputPolicyDecision,
    PolicyDecision,
    PolicyObligation,
    PolicyRequest,
    canonical_dumps,
)
from graphblocks.output_policy import (
    VALID_DRAFT_DISPOSITIONS,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS as VALID_PENDING_TOOL_CALLS,
    VALID_PROVIDER_CANCELLATIONS,
)
from graphblocks.policy import VALID_POLICY_EFFECTS


class OpaPolicyAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpaPolicyInput:
    input_json: str
    package_ref: str | None = None

    def __post_init__(self) -> None:
        if self.package_ref is not None:
            package_ref = self.package_ref.strip()
            if not package_ref:
                raise OpaPolicyAdapterError("package_ref must not be empty")
            object.__setattr__(self, "package_ref", package_ref)

    def input_contract(self) -> dict[str, object]:
        return {
            "package_ref": self.package_ref,
            "input": json.loads(self.input_json),
        }


def prepare_opa_policy_input(request: PolicyRequest, *, package_ref: str | None = None) -> OpaPolicyInput:
    digested_request = request if request.input_digest else request.with_input_digest()
    atomic_unit = None
    if digested_request.atomic_unit is not None:
        atomic_unit = {
            "resource_id": digested_request.atomic_unit.resource_id,
            "resource_kind": digested_request.atomic_unit.resource_kind,
            "tenant_id": digested_request.atomic_unit.tenant_id,
            "attributes": dict(digested_request.atomic_unit.attributes),
        }
    input_document = {
        "request_id": digested_request.request_id,
        "enforcement_point": digested_request.enforcement_point,
        "action": digested_request.action,
        "principal": None
        if digested_request.principal is None
        else {
            "principal_id": digested_request.principal.principal_id,
            "tenant_id": digested_request.principal.tenant_id,
            "groups": list(digested_request.principal.groups),
            "roles": list(digested_request.principal.roles),
            "attributes": dict(digested_request.principal.attributes),
        },
        "resource": {
            "resource_id": digested_request.resource.resource_id,
            "resource_kind": digested_request.resource.resource_kind,
            "tenant_id": digested_request.resource.tenant_id,
            "attributes": dict(digested_request.resource.attributes),
        },
        "tenant": None
        if digested_request.tenant is None
        else {
            "resource_id": digested_request.tenant.resource_id,
            "resource_kind": digested_request.tenant.resource_kind,
            "tenant_id": digested_request.tenant.tenant_id,
            "attributes": dict(digested_request.tenant.attributes),
        },
        "occurred_at": digested_request.occurred_at,
        "release_id": digested_request.release_id,
        "deployment_revision_id": digested_request.deployment_revision_id,
        "run_id": digested_request.run_id,
        "atomic_unit": atomic_unit,
        "data_labels": list(digested_request.data_labels),
        "requested_usage": [dict(usage) for usage in digested_request.requested_usage],
        "attributes": dict(digested_request.attributes),
        "policy_snapshot_id": digested_request.policy_snapshot_id,
        "input_digest": digested_request.input_digest,
    }
    try:
        input_json = canonical_dumps(input_document)
    except (TypeError, ValueError) as error:
        raise OpaPolicyAdapterError("policy request input must be canonical JSON") from error
    return OpaPolicyInput(input_json=input_json, package_ref=package_ref)


def policy_decision_from_opa_result(
    *,
    decision_id: str,
    request: PolicyRequest,
    result: Mapping[str, object],
    evaluated_at: str,
) -> PolicyDecision:
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise OpaPolicyAdapterError("decision_id must be a non-empty string")
    if not isinstance(evaluated_at, str) or not evaluated_at.strip():
        raise OpaPolicyAdapterError("evaluated_at must be a non-empty string")

    result_body = result.get("result", result)
    if not isinstance(result_body, Mapping):
        raise OpaPolicyAdapterError("OPA result must contain an object result")

    effect = result_body.get("effect")
    if not isinstance(effect, str) or effect not in VALID_POLICY_EFFECTS:
        raise OpaPolicyAdapterError(f"unknown policy effect {effect}")

    reason_codes = _string_tuple(result_body.get("reason_codes", result_body.get("reasonCodes", ())))
    policy_refs = _string_tuple(result_body.get("policy_refs", result_body.get("policyRefs", reason_codes)))
    obligations = []
    raw_obligations = result_body.get("obligations", ())
    if not isinstance(raw_obligations, list | tuple):
        raise OpaPolicyAdapterError("OPA obligations must be a sequence")
    for raw_obligation in raw_obligations:
        if not isinstance(raw_obligation, Mapping):
            raise OpaPolicyAdapterError("OPA obligation must be an object")
        obligation_id = raw_obligation.get("obligation_id", raw_obligation.get("id"))
        obligation_type = raw_obligation.get("obligation_type", raw_obligation.get("type"))
        parameters = raw_obligation.get("parameters", {})
        if not isinstance(obligation_id, str) or not isinstance(obligation_type, str):
            raise OpaPolicyAdapterError("OPA obligation requires string id and type")
        if not isinstance(parameters, Mapping):
            raise OpaPolicyAdapterError("OPA obligation parameters must be an object")
        obligations.append(PolicyObligation(obligation_id, obligation_type, dict(parameters)))

    raw_advice = result_body.get("advice", ())
    if not isinstance(raw_advice, list | tuple):
        raise OpaPolicyAdapterError("OPA advice must be a sequence")
    advice = []
    for item in raw_advice:
        if not isinstance(item, Mapping):
            raise OpaPolicyAdapterError("OPA advice item must be an object")
        advice.append(dict(item))

    digested_request = request if request.input_digest else request.with_input_digest()
    return PolicyDecision(
        decision_id=decision_id,
        effect=effect,
        reason_codes=reason_codes,
        policy_refs=policy_refs,
        obligations=tuple(obligations),
        advice=tuple(advice),
        evaluated_at=evaluated_at,
        input_digest=digested_request.input_digest,
    )


def output_policy_decision_from_opa_result(
    *,
    decision_id: str,
    request: PolicyRequest,
    result: Mapping[str, object],
    evaluated_at: str,
) -> OutputPolicyDecision:
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise OpaPolicyAdapterError("decision_id must be a non-empty string")
    if not isinstance(evaluated_at, str) or not evaluated_at.strip():
        raise OpaPolicyAdapterError("evaluated_at must be a non-empty string")

    result_body = result.get("result", result)
    if not isinstance(result_body, Mapping):
        raise OpaPolicyAdapterError("OPA output policy result must contain an object result")

    disposition = result_body.get("disposition", result_body.get("outputDisposition"))
    if not isinstance(disposition, str) or disposition not in VALID_OUTPUT_DISPOSITIONS:
        raise OpaPolicyAdapterError(f"unknown output policy disposition {disposition}")

    reason_codes = _string_tuple(result_body.get("reason_codes", result_body.get("reasonCodes", ())))
    policy_refs = _string_tuple(result_body.get("policy_refs", result_body.get("policyRefs", reason_codes)))
    digested_request = request if request.input_digest else request.with_input_digest()
    try:
        return OutputPolicyDecision(
            decision_id=decision_id,
            disposition=disposition,
            accepted_through_sequence=_optional_non_negative_int(
                result_body.get("accepted_through_sequence", result_body.get("acceptedThroughSequence")),
                "accepted_through_sequence",
            ),
            replacement_parts=_content_parts(result_body),
            redactions=_redactions(result_body.get("redactions", ())),
            reason_codes=reason_codes,
            policy_refs=policy_refs,
            provider_cancellation=_optional_literal(
                result_body.get("provider_cancellation", result_body.get("providerCancellation")),
                VALID_PROVIDER_CANCELLATIONS,
                "provider_cancellation",
                "request",
            ),
            draft_disposition=_optional_literal(
                result_body.get("draft_disposition", result_body.get("draftDisposition")),
                VALID_DRAFT_DISPOSITIONS,
                "draft_disposition",
                "retract",
            ),
            pending_tool_calls=_optional_literal(
                result_body.get("pending_tool_calls", result_body.get("pendingToolCalls")),
                VALID_PENDING_TOOL_CALLS,
                "pending_tool_calls",
                "deny",
            ),
            evaluated_at=evaluated_at,
            input_digest=digested_request.input_digest,
        )
    except ValueError as error:
        raise OpaPolicyAdapterError(str(error)) from error


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        if not value.strip():
            raise OpaPolicyAdapterError("policy result string collection contains a blank string")
        return (value,)
    if not isinstance(value, list | tuple):
        raise OpaPolicyAdapterError("policy result string collection must be a sequence")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise OpaPolicyAdapterError("policy result string collection contains a non-string item")
        if not item.strip():
            raise OpaPolicyAdapterError("policy result string collection contains a blank string")
        items.append(item)
    return tuple(items)


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OpaPolicyAdapterError(f"{field_name} must be a non-negative integer")
    return value


def _optional_literal(value: object, valid_values: frozenset[str], field_name: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in valid_values:
        raise OpaPolicyAdapterError(f"invalid {field_name} {value!r}")
    return value


def _content_parts(result_body: Mapping[str, object]) -> tuple[ContentPart, ...]:
    raw_parts = result_body.get("replacement_parts", result_body.get("replacementParts", ()))
    if not isinstance(raw_parts, list | tuple):
        raise OpaPolicyAdapterError("output policy replacement parts must be a sequence")
    parts = [_content_part(raw_part) for raw_part in raw_parts]
    replacement = result_body.get("replacement")
    if not parts and replacement is not None:
        if not isinstance(replacement, str):
            raise OpaPolicyAdapterError("output policy replacement must be a string")
        parts.append(ContentPart(kind="text", text=replacement))
    return tuple(parts)


def _content_part(raw_part: object) -> ContentPart:
    if isinstance(raw_part, str):
        return ContentPart(kind="text", text=raw_part)
    if not isinstance(raw_part, Mapping):
        raise OpaPolicyAdapterError("output policy replacement part must be an object")
    kind = raw_part.get("kind")
    if kind == "text":
        text = raw_part.get("text")
        if not isinstance(text, str):
            raise OpaPolicyAdapterError("text output policy replacement part requires string text")
        return ContentPart(kind="text", text=text)
    if kind in {"json", "artifact_ref"}:
        data = raw_part.get("data")
        if not isinstance(data, Mapping):
            raise OpaPolicyAdapterError(f"{kind} output policy replacement part requires object data")
        return ContentPart(kind=kind, data=dict(data))  # type: ignore[arg-type]
    raise OpaPolicyAdapterError(f"unknown output policy replacement part kind {kind}")


def _redactions(raw_redactions: object) -> tuple[dict[str, object], ...]:
    if not isinstance(raw_redactions, list | tuple):
        raise OpaPolicyAdapterError("output policy redactions must be a sequence")
    redactions = []
    for raw_redaction in raw_redactions:
        if not isinstance(raw_redaction, Mapping):
            raise OpaPolicyAdapterError("output policy redaction must be an object")
        redactions.append(dict(raw_redaction))
    return tuple(redactions)


__all__ = [
    "OpaPolicyAdapterError",
    "OpaPolicyInput",
    "VALID_DRAFT_DISPOSITIONS",
    "VALID_OUTPUT_DISPOSITIONS",
    "VALID_PENDING_TOOL_CALLS",
    "VALID_POLICY_EFFECTS",
    "VALID_PROVIDER_CANCELLATIONS",
    "output_policy_decision_from_opa_result",
    "policy_decision_from_opa_result",
    "prepare_opa_policy_input",
]
