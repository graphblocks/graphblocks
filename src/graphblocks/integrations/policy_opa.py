from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from graphblocks import (
    ContentPart,
    OutputPolicyDecision,
    PolicyDecision,
    PolicyObligation,
    PolicyRequest,
    canonical_dumps,
    canonical_loads,
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


def _exact_non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpaPolicyAdapterError(
            f"{field_name} must be a non-empty string"
        )
    if value != value.strip():
        raise OpaPolicyAdapterError(
            f"{field_name} must not contain surrounding whitespace"
        )
    return value


def _alias_value(
    value: Mapping[str, object],
    snake_case: str,
    camel_case: str,
    default: object,
) -> object:
    if snake_case in value and camel_case in value:
        raise OpaPolicyAdapterError(
            f"OPA result must not contain both {snake_case} and {camel_case}"
        )
    if snake_case in value:
        return value[snake_case]
    if camel_case in value:
        return value[camel_case]
    return default


def _result_body(
    result: object,
    *,
    contract_name: str,
) -> Mapping[str, object]:
    if not isinstance(result, Mapping):
        raise OpaPolicyAdapterError(f"{contract_name} must be an object")
    materialized = dict(result)
    try:
        canonical_dumps(materialized)
    except (TypeError, ValueError) as error:
        raise OpaPolicyAdapterError(
            f"{contract_name} must be valid strict JSON"
        ) from error
    body = materialized.get("result", materialized)
    if not isinstance(body, Mapping):
        raise OpaPolicyAdapterError(
            f"{contract_name} must contain an object result"
        )
    return body


@dataclass(frozen=True, slots=True)
class OpaPolicyInput:
    input_json: str
    package_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.input_json, str):
            raise OpaPolicyAdapterError("OPA policy input must be valid strict JSON")
        try:
            parsed_input = canonical_loads(self.input_json)
        except (TypeError, ValueError) as error:
            raise OpaPolicyAdapterError(
                "OPA policy input must be valid strict JSON"
            ) from error
        if not isinstance(parsed_input, Mapping):
            raise OpaPolicyAdapterError("OPA policy input must be a JSON object")
        if self.package_ref is not None:
            if not isinstance(self.package_ref, str):
                raise OpaPolicyAdapterError("package_ref must be a string")
            package_ref = self.package_ref.strip()
            if not package_ref:
                raise OpaPolicyAdapterError("package_ref must not be empty")
            object.__setattr__(self, "package_ref", package_ref)

    def input_contract(self) -> dict[str, object]:
        try:
            parsed_input = canonical_loads(self.input_json)
        except (TypeError, ValueError) as error:
            raise OpaPolicyAdapterError("OPA policy input must be valid strict JSON") from error
        if not isinstance(parsed_input, Mapping):
            raise OpaPolicyAdapterError("OPA policy input must be a JSON object")
        return {
            "package_ref": self.package_ref,
            "input": parsed_input,
        }


def prepare_opa_policy_input(request: PolicyRequest, *, package_ref: str | None = None) -> OpaPolicyInput:
    if not isinstance(request, PolicyRequest):
        raise OpaPolicyAdapterError("request must be a PolicyRequest")
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
    decision_id = _exact_non_empty("decision_id", decision_id)
    evaluated_at = _exact_non_empty("evaluated_at", evaluated_at)
    if not isinstance(request, PolicyRequest):
        raise OpaPolicyAdapterError("request must be a PolicyRequest")

    result_body = _result_body(result, contract_name="OPA result")

    effect = result_body.get("effect")
    if not isinstance(effect, str) or effect not in VALID_POLICY_EFFECTS:
        raise OpaPolicyAdapterError(f"unknown policy effect {effect}")

    reason_codes = _string_tuple(
        _alias_value(result_body, "reason_codes", "reasonCodes", ())
    )
    policy_refs = _string_tuple(
        _alias_value(result_body, "policy_refs", "policyRefs", reason_codes)
    )
    obligations = []
    raw_obligations = result_body.get("obligations", ())
    if not isinstance(raw_obligations, list | tuple):
        raise OpaPolicyAdapterError("OPA obligations must be a sequence")
    for raw_obligation in raw_obligations:
        if not isinstance(raw_obligation, Mapping):
            raise OpaPolicyAdapterError("OPA obligation must be an object")
        obligation_id = _alias_value(
            raw_obligation,
            "obligation_id",
            "id",
            None,
        )
        obligation_type = _alias_value(
            raw_obligation,
            "obligation_type",
            "type",
            None,
        )
        parameters = raw_obligation.get("parameters", {})
        if (
            not isinstance(obligation_id, str)
            or not obligation_id.strip()
            or obligation_id != obligation_id.strip()
            or not isinstance(obligation_type, str)
            or not obligation_type.strip()
            or obligation_type != obligation_type.strip()
        ):
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
    valid_until = _alias_value(
        result_body,
        "valid_until",
        "validUntil",
        None,
    )
    if valid_until is not None:
        valid_until = _exact_non_empty(
            "OPA policy decision valid_until",
            valid_until,
        )
    return PolicyDecision(
        decision_id=decision_id,
        effect=effect,
        reason_codes=reason_codes,
        policy_refs=policy_refs,
        obligations=tuple(obligations),
        advice=tuple(advice),
        evaluated_at=evaluated_at,
        valid_until=valid_until,  # type: ignore[arg-type]
        input_digest=digested_request.input_digest,
    )


def output_policy_decision_from_opa_result(
    *,
    decision_id: str,
    request: PolicyRequest,
    result: Mapping[str, object],
    evaluated_at: str,
) -> OutputPolicyDecision:
    decision_id = _exact_non_empty("decision_id", decision_id)
    evaluated_at = _exact_non_empty("evaluated_at", evaluated_at)
    if not isinstance(request, PolicyRequest):
        raise OpaPolicyAdapterError("request must be a PolicyRequest")

    result_body = _result_body(
        result,
        contract_name="OPA output policy result",
    )

    disposition = _alias_value(
        result_body,
        "disposition",
        "outputDisposition",
        None,
    )
    if not isinstance(disposition, str) or disposition not in VALID_OUTPUT_DISPOSITIONS:
        raise OpaPolicyAdapterError(f"unknown output policy disposition {disposition}")

    reason_codes = _string_tuple(
        _alias_value(result_body, "reason_codes", "reasonCodes", ())
    )
    policy_refs = _string_tuple(
        _alias_value(result_body, "policy_refs", "policyRefs", reason_codes)
    )
    digested_request = request if request.input_digest else request.with_input_digest()
    try:
        return OutputPolicyDecision(
            decision_id=decision_id,
            disposition=disposition,
            accepted_through_sequence=_optional_non_negative_int(
                _alias_value(
                    result_body,
                    "accepted_through_sequence",
                    "acceptedThroughSequence",
                    None,
                ),
                "accepted_through_sequence",
            ),
            replacement_parts=_content_parts(result_body),
            redactions=_redactions(result_body.get("redactions", ())),
            reason_codes=reason_codes,
            policy_refs=policy_refs,
            provider_cancellation=_optional_literal(
                _alias_value(
                    result_body,
                    "provider_cancellation",
                    "providerCancellation",
                    None,
                ),
                VALID_PROVIDER_CANCELLATIONS,
                "provider_cancellation",
                "request",
            ),
            draft_disposition=_optional_literal(
                _alias_value(
                    result_body,
                    "draft_disposition",
                    "draftDisposition",
                    None,
                ),
                VALID_DRAFT_DISPOSITIONS,
                "draft_disposition",
                "retract",
            ),
            pending_tool_calls=_optional_literal(
                _alias_value(
                    result_body,
                    "pending_tool_calls",
                    "pendingToolCalls",
                    None,
                ),
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
        if value != value.strip():
            raise OpaPolicyAdapterError(
                "policy result string collection contains surrounding whitespace"
            )
        return (value,)
    if not isinstance(value, list | tuple):
        raise OpaPolicyAdapterError("policy result string collection must be a sequence")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise OpaPolicyAdapterError("policy result string collection contains a non-string item")
        if not item.strip():
            raise OpaPolicyAdapterError("policy result string collection contains a blank string")
        if item != item.strip():
            raise OpaPolicyAdapterError(
                "policy result string collection contains surrounding whitespace"
            )
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
    raw_parts = _alias_value(
        result_body,
        "replacement_parts",
        "replacementParts",
        (),
    )
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
