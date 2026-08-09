"""Deterministic Python reference oracle for the closed outcome TCK."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

from .canonical import canonical_dumps_reference
from .outcome import (
    InputDependency,
    Outcome,
    PortRef,
    Readiness,
    ReadinessTracker,
    ResolvedInput,
)


OUTCOME_TCK_CONTRACT_VERSION = "graphblocks.outcome.tck.v1"

_ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "authorization",
        "budget",
        "cancelled",
        "capacity",
        "configuration",
        "conflict",
        "internal",
        "not_found",
        "permanent",
        "policy",
        "provider",
        "quota",
        "rate_limit",
        "timeout",
        "transient",
        "validation",
    }
)
_CANCEL_CODES = frozenset(
    {
        "barge_in",
        "budget_exhausted",
        "client_disconnect",
        "dependency_failed",
        "entitlement_revoked",
        "lease_lost",
        "policy_denied",
        "provider_quota_exhausted",
        "rollout_drain",
        "shutdown",
        "superseded",
        "timeout",
        "user_cancel",
    }
)
_OUTCOME_FIELDS_BY_STATUS = {
    "value": frozenset({"status", "value"}),
    "absent": frozenset({"status"}),
    "skipped": frozenset({"status", "reason"}),
    "denied": frozenset({"status", "decisionId"}),
    "budget_exhausted": frozenset({"status", "code", "message"}),
    "paused": frozenset({"status", "code", "message"}),
    "failed": frozenset({"status", "error"}),
    "cancelled": frozenset({"status", "reason"}),
}
_OUTCOME_ALIASES_BY_STATUS = {
    status: {
        "kind": "status",
        **({"payload": "value"} if status == "value" else {}),
        **({"decision_id": "decisionId"} if status == "denied" else {}),
    }
    for status in _OUTCOME_FIELDS_BY_STATUS
}
_ALL_OUTCOME_FIELDS = frozenset().union(*_OUTCOME_FIELDS_BY_STATUS.values())
_ALL_OUTCOME_ROOT_ALIASES = frozenset({"kind", "payload", "decision_id"})


class _OutcomeContractError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True, slots=True)
class _DecodedOutcome:
    value: Outcome
    wire: dict[str, object]


def _error(category: str, message: str) -> _OutcomeContractError:
    return _OutcomeContractError(category, message)


def _mapping(
    value: object,
    *,
    owner: str,
    invalid_category: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(invalid_category, f"{owner} must be an object")
    if any(type(key) is not str for key in value):
        raise _error("unknown_field", f"{owner} keys must be strings")
    return value


def _closed_mapping(
    value: object,
    *,
    owner: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    aliases: frozenset[str] = frozenset(),
    forbidden: frozenset[str] = frozenset(),
    invalid_category: str = "invalid_outcome",
) -> Mapping[str, object]:
    mapping = _mapping(value, owner=owner, invalid_category=invalid_category)
    keys = set(mapping)
    present_aliases = sorted(keys & aliases)
    if present_aliases:
        raise _error(
            "noncanonical_alias",
            f"{owner} uses noncanonical field {present_aliases[0]!r}",
        )
    present_forbidden = sorted(keys & forbidden)
    if present_forbidden:
        raise _error(
            "forbidden_field", f"{owner} contains {present_forbidden[0]!r}"
        )
    unknown = sorted(keys - required - optional - aliases - forbidden)
    if unknown:
        raise _error("unknown_field", f"{owner} contains {unknown[0]!r}")
    missing = sorted(required - keys)
    if missing:
        raise _error("missing_field", f"{owner} is missing {missing[0]!r}")
    return mapping


def _string(
    value: object,
    *,
    owner: str,
    nullable: bool = False,
    invalid_category: str = "invalid_outcome",
) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise _error(invalid_category, f"{owner} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _error(
            invalid_category, f"{owner} must contain Unicode scalar values"
        ) from error
    return value


def _identifier(value: object, *, owner: str) -> str:
    text = _string(value, owner=owner, invalid_category="invalid_identifier")
    if text is None:  # pragma: no cover - nullable=False is enforced above
        raise _error("invalid_identifier", f"{owner} must be a string")
    if (
        not text
        or text != text.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
    ):
        raise _error(
            "invalid_identifier",
            f"{owner} must be a canonical non-empty identifier",
        )
    return text


def _message(value: object, *, owner: str, nullable: bool = False) -> str | None:
    text = _string(value, owner=owner, nullable=nullable)
    if text is None:
        return None
    if not text.strip() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in text
    ):
        raise _error(
            "invalid_outcome",
            f"{owner} must be non-empty control-free text",
        )
    return text


def _required_message(value: object, *, owner: str) -> str:
    text = _message(value, owner=owner)
    if text is None:  # pragma: no cover - nullable=False is enforced above
        raise _error("invalid_outcome", f"{owner} must be a string")
    return text


def _json_value(value: object, *, owner: str, depth: int = 0) -> object:
    if depth > 64:
        raise _error("invalid_outcome", f"{owner} exceeds maximum depth")
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            _string(value, owner=owner)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error("invalid_outcome", f"{owner} numbers must be finite")
        return value
    if isinstance(value, Mapping):
        snapshot: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _error("invalid_outcome", f"{owner} keys must be strings")
            if key in snapshot:
                raise _error("invalid_outcome", f"{owner} keys must be unique")
            snapshot[key] = _json_value(item, owner=f"{owner}.{key}", depth=depth + 1)
        return snapshot
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _json_value(item, owner=f"{owner}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise _error("invalid_outcome", f"{owner} must be JSON-compatible")


def _decode_outcome(value: object, *, owner: str = "outcome") -> _DecodedOutcome:
    root = _mapping(value, owner=owner, invalid_category="invalid_outcome")
    if "status" not in root:
        if "kind" in root:
            raise _error(
                "noncanonical_alias", f"{owner} uses noncanonical field 'kind'"
            )
        raise _error("missing_field", f"{owner} is missing 'status'")
    status = root["status"]
    if type(status) is not str or status not in _OUTCOME_FIELDS_BY_STATUS:
        raise _error("invalid_outcome", f"{owner}.status is invalid")
    required = _OUTCOME_FIELDS_BY_STATUS[status]
    aliases = frozenset(_OUTCOME_ALIASES_BY_STATUS[status])
    forbidden = (
        _ALL_OUTCOME_FIELDS | _ALL_OUTCOME_ROOT_ALIASES
    ) - required - aliases
    root = _closed_mapping(
        root,
        owner=owner,
        required=required,
        aliases=aliases,
        forbidden=forbidden,
    )

    if status == "value":
        payload = _json_value(root["value"], owner=f"{owner}.value")
        outcome = Outcome.value(payload)
        return _DecodedOutcome(outcome, {"status": outcome.status, "value": payload})

    if status == "absent":
        outcome = Outcome.absent()
        return _DecodedOutcome(outcome, {"status": outcome.status})

    if status == "skipped":
        reason = _closed_mapping(
            root["reason"],
            owner=f"{owner}.reason",
            required=frozenset({"code", "message"}),
        )
        code = _identifier(reason["code"], owner=f"{owner}.reason.code")
        message = _message(
            reason["message"], owner=f"{owner}.reason.message", nullable=True
        )
        outcome = Outcome.skipped(code, message)
        return _DecodedOutcome(
            outcome,
            {
                "status": outcome.status,
                "reason": {"code": outcome.code, "message": outcome.message},
            },
        )

    if status == "denied":
        decision_id = _identifier(root["decisionId"], owner=f"{owner}.decisionId")
        outcome = Outcome.denied(decision_id)
        return _DecodedOutcome(
            outcome,
            {"status": outcome.status, "decisionId": outcome.code},
        )

    if status in {"budget_exhausted", "paused"}:
        code = _identifier(root["code"], owner=f"{owner}.code")
        message = _message(root["message"], owner=f"{owner}.message", nullable=True)
        outcome = (
            Outcome.budget_exhausted(code, message)
            if status == "budget_exhausted"
            else Outcome.paused(code, message)
        )
        return _DecodedOutcome(
            outcome,
            {
                "status": outcome.status,
                "code": outcome.code,
                "message": outcome.message,
            },
        )

    if status == "failed":
        error = _closed_mapping(
            root["error"],
            owner=f"{owner}.error",
            required=frozenset(
                {"code", "category", "message", "retryable", "details", "causeChain"}
            ),
            aliases=frozenset({"cause_chain"}),
        )
        code = _identifier(error["code"], owner=f"{owner}.error.code")
        category = error["category"]
        if type(category) is not str or category not in _ERROR_CATEGORIES:
            raise _error("invalid_outcome", f"{owner}.error.category is invalid")
        message = _required_message(error["message"], owner=f"{owner}.error.message")
        retryable = error["retryable"]
        if type(retryable) is not bool:
            raise _error(
                "invalid_outcome", f"{owner}.error.retryable must be a boolean"
            )
        details = _json_value(error["details"], owner=f"{owner}.error.details")
        if not isinstance(details, dict):
            raise _error("invalid_outcome", f"{owner}.error.details must be an object")
        cause_chain_value = error["causeChain"]
        if not isinstance(cause_chain_value, list) or not all(
            type(item) is str for item in cause_chain_value
        ):
            raise _error("invalid_outcome", f"{owner}.error.causeChain must be strings")
        cause_chain = [
            _required_message(item, owner=f"{owner}.error.causeChain[{index}]")
            for index, item in enumerate(cause_chain_value)
        ]
        # The public facade owns the common status/code/message/retryable state.
        # Structured error category/details/cause data is retained by this closed
        # wire adapter until the ergonomic facade grows equivalent fields.
        outcome = Outcome.failed(code, message, retryable=retryable)
        return _DecodedOutcome(
            outcome,
            {
                "status": outcome.status,
                "error": {
                    "code": outcome.code,
                    "category": category,
                    "message": outcome.message,
                    "retryable": outcome.retryable,
                    "details": details,
                    "causeChain": cause_chain,
                },
            },
        )

    reason = _closed_mapping(
        root["reason"],
        owner=f"{owner}.reason",
        required=frozenset({"code", "message", "requestedBy", "policyDecisionRef"}),
        aliases=frozenset({"requested_by", "policy_decision_ref"}),
    )
    code = _identifier(reason["code"], owner=f"{owner}.reason.code")
    if code not in _CANCEL_CODES:
        raise _error("invalid_outcome", f"{owner}.reason.code is invalid")
    message = _message(
        reason["message"], owner=f"{owner}.reason.message", nullable=True
    )
    requested_by = (
        None
        if reason["requestedBy"] is None
        else _identifier(reason["requestedBy"], owner=f"{owner}.reason.requestedBy")
    )
    policy_decision_ref = (
        None
        if reason["policyDecisionRef"] is None
        else _identifier(
            reason["policyDecisionRef"], owner=f"{owner}.reason.policyDecisionRef"
        )
    )
    outcome = Outcome.cancelled(code, message)
    return _DecodedOutcome(
        outcome,
        {
            "status": outcome.status,
            "reason": {
                "code": outcome.code,
                "message": outcome.message,
                "requestedBy": requested_by,
                "policyDecisionRef": policy_decision_ref,
            },
        },
    )


def _decode_port_ref(value: object, *, owner: str) -> PortRef:
    port = _closed_mapping(
        value,
        owner=owner,
        required=frozenset({"node", "port"}),
        invalid_category="invalid_readiness",
    )
    node = _identifier(port["node"], owner=f"{owner}.node")
    name = _identifier(port["port"], owner=f"{owner}.port")
    return PortRef(node, name)


def _port_ref_wire(port: PortRef) -> dict[str, object]:
    return {"node": port.node, "port": port.port}


def _readiness_wire(
    readiness: Readiness,
    *,
    outcome_wire_by_identity: Mapping[int, dict[str, object]],
) -> dict[str, object]:
    if readiness.kind == "waiting":
        return {
            "status": "waiting",
            "missing": [_port_ref_wire(port) for port in readiness.missing],
        }
    if readiness.kind == "blocked":
        if (
            readiness.input is None
            or readiness.source is None
            or readiness.outcome is None
        ):
            raise _error("invalid_readiness", "blocked readiness is incomplete")
        return {
            "status": "blocked",
            "input": readiness.input,
            "source": _port_ref_wire(readiness.source),
            "outcome": dict(outcome_wire_by_identity[id(readiness.outcome)]),
        }
    inputs: dict[str, object] = {}
    for name, resolved in readiness.inputs.items():
        if not isinstance(resolved, ResolvedInput):
            raise _error("invalid_readiness", "ready input is invalid")
        if resolved.kind == "value":
            inputs[name] = {
                "mode": "value",
                "value": _json_value(
                    resolved.payload, owner=f"readiness.inputs.{name}.value"
                ),
            }
        else:
            if not isinstance(resolved.payload, Outcome):
                raise _error("invalid_readiness", "outcome input payload is invalid")
            inputs[name] = {
                "mode": "outcome",
                "outcome": dict(outcome_wire_by_identity[id(resolved.payload)]),
            }
    return {"status": "ready", "inputs": inputs}


def _evaluate_readiness(case: Mapping[str, object]) -> dict[str, object]:
    signals_value = case["signals"]
    dependencies_value = case["dependencies"]
    if not isinstance(signals_value, list):
        raise _error("invalid_readiness", "signals must be an array")
    if not isinstance(dependencies_value, list):
        raise _error("invalid_readiness", "dependencies must be an array")

    signals: dict[PortRef, Outcome] = {}
    outcome_wire_by_identity: dict[int, dict[str, object]] = {}
    for index, raw_signal in enumerate(signals_value):
        signal = _closed_mapping(
            raw_signal,
            owner=f"signals[{index}]",
            required=frozenset({"portRef", "outcome"}),
            aliases=frozenset({"port_ref"}),
            invalid_category="invalid_readiness",
        )
        port = _decode_port_ref(signal["portRef"], owner=f"signals[{index}].portRef")
        if port in signals:
            raise _error("duplicate_signal", f"signals[{index}] duplicates a port")
        decoded = _decode_outcome(signal["outcome"], owner=f"signals[{index}].outcome")
        signals[port] = decoded.value
        outcome_wire_by_identity[id(decoded.value)] = decoded.wire

    dependencies: list[InputDependency] = []
    dependency_inputs: set[str] = set()
    for index, raw_dependency in enumerate(dependencies_value):
        dependency = _closed_mapping(
            raw_dependency,
            owner=f"dependencies[{index}]",
            required=frozenset({"input", "source", "mode"}),
            invalid_category="invalid_readiness",
        )
        input_name = _identifier(
            dependency["input"], owner=f"dependencies[{index}].input"
        )
        if input_name in dependency_inputs:
            raise _error(
                "duplicate_dependency",
                f"dependencies[{index}] duplicates input {input_name!r}",
            )
        dependency_inputs.add(input_name)
        source = _decode_port_ref(
            dependency["source"], owner=f"dependencies[{index}].source"
        )
        mode = dependency["mode"]
        if mode == "value":
            dependencies.append(InputDependency.value(input_name, source))
        elif mode == "outcome":
            dependencies.append(InputDependency.outcome(input_name, source))
        else:
            raise _error("invalid_readiness", f"dependencies[{index}].mode is invalid")

    tracker = ReadinessTracker(signals)
    readiness = tracker.readiness(dependencies)
    return _readiness_wire(
        readiness,
        outcome_wire_by_identity=outcome_wire_by_identity,
    )


def _evaluate_local_terminal(case: Mapping[str, object]) -> dict[str, object]:
    decoded = _decode_outcome(case["outcome"])
    status = decoded.wire["status"]
    status_contract = {
        "value": ("succeeded", "run_succeeded"),
        "failed": ("failed", "run_failed"),
        "cancelled": ("cancelled", "run_cancelled"),
        "denied": ("rejected", "run_rejected"),
        "paused": ("paused", "run_paused"),
        "budget_exhausted": ("exhausted", "run_exhausted"),
        "absent": ("failed", "run_failed"),
        "skipped": ("failed", "run_failed"),
    }
    if not isinstance(status, str) or status not in status_contract:
        raise _error("invalid_outcome", "local terminal outcome is invalid")
    run_status, terminal_kind = status_contract[status]
    if status == "value":
        journal_kinds = [
            "run_started",
            "node_started",
            "node_completed",
            terminal_kind,
        ]
    elif status in {"failed", "absent", "skipped"}:
        journal_kinds = [
            "run_started",
            "node_started",
            "node_failed",
            terminal_kind,
        ]
    else:
        journal_kinds = ["run_started", "node_started", terminal_kind]
    return {
        "status": run_status,
        "terminalKind": terminal_kind,
        "terminalCount": 1,
        "journalKinds": journal_kinds,
    }


def _decode_case(case: object) -> tuple[Mapping[str, object], str]:
    mapping = _mapping(case, owner="case", invalid_category="invalid_outcome")
    if "scenario" not in mapping:
        raise _error("missing_field", "case is missing 'scenario'")
    scenario = mapping["scenario"]
    if type(scenario) is not str:
        raise _error("invalid_outcome", "case.scenario must be a string")
    if scenario == "normalize_outcome":
        required = frozenset({"name", "scenario", "outcome"})
    elif scenario == "evaluate_readiness":
        required = frozenset({"name", "scenario", "signals", "dependencies"})
    elif scenario == "execute_local_terminal":
        required = frozenset({"name", "scenario", "outcome"})
    else:
        raise _error("unsupported_scenario", "case scenario is not supported")
    return (
        _closed_mapping(mapping, owner="case", required=required),
        scenario,
    )


def evaluate_outcome_tck_case_reference(case: object) -> dict[str, object]:
    """Evaluate one closed outcome fixture without consulting expected output."""

    scenario_value = case.get("scenario") if isinstance(case, Mapping) else None
    scenario = scenario_value if type(scenario_value) is str else ""
    base: dict[str, object] = {
        "contractVersion": OUTCOME_TCK_CONTRACT_VERSION,
        "ok": False,
        "scenario": scenario,
    }
    try:
        canonical_dumps_reference(case)
        normalized_case, scenario = _decode_case(case)
        base["scenario"] = scenario
        _identifier(normalized_case["name"], owner="case.name")
        if scenario == "normalize_outcome":
            decoded = _decode_outcome(normalized_case["outcome"])
            return {**base, "ok": True, "outcome": decoded.wire}
        if scenario == "execute_local_terminal":
            return {
                **base,
                "ok": True,
                "run": _evaluate_local_terminal(normalized_case),
            }
        return {
            **base,
            "ok": True,
            "readiness": _evaluate_readiness(normalized_case),
        }
    except _OutcomeContractError as error:
        return {**base, "errorCategory": error.category}
    except (OverflowError, TypeError, ValueError):
        return {**base, "errorCategory": "invalid_outcome"}


__all__ = ["OUTCOME_TCK_CONTRACT_VERSION", "evaluate_outcome_tck_case_reference"]
