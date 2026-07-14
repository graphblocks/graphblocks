from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing.exceptions import Unresolvable

from .canonical import PSEUDO_NODES, _normalize_graph_unchecked, canonical_dumps, canonical_hash
from .diagnostics import Diagnostic, DiagnosticSet
from .duration import parse_duration_seconds
from .migration import (
    GRAPH_API_VERSION,
    LEGACY_GRAPH_API_VERSIONS,
    MigrationError,
    migrate_document,
)
from .output_policy import (
    VALID_DELIVERY_MODES as VALID_OUTPUT_DELIVERY_MODES,
    VALID_DRAFT_DISPOSITIONS,
    VALID_FLUSH_BOUNDARIES,
    VALID_OUTPUT_DISPOSITIONS,
    VALID_OUTPUT_DURABLE_RESULTS,
    VALID_PENDING_TOOL_CALLS_DISPOSITIONS,
    VALID_PROVIDER_CANCELLATIONS,
    VALID_VIOLATION_ACTIONS,
)
from .plugins import BlockCatalog, builtin_block_catalog
from .policy import VALID_ENFORCEMENT_POINTS as VALID_POLICY_ENFORCEMENT_POINTS
from .schema import SchemaId, SchemaIdError, resource_schema_errors
from .tools import (
    VALID_TOOL_APPROVALS,
    VALID_TOOL_CANCELLATIONS,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_IDEMPOTENCIES,
    VALID_TOOL_RESULT_MODES,
)
from .url_validation import validate_webhook_url

STATE_CHANGING_TOOL_EFFECTS = frozenset({"external_write", "filesystem_write", "process", "destructive"})
FORBIDDEN_TOOL_DEFINITION_FIELDS = frozenset(
    {
        "credentials",
        "credential",
        "secret",
        "secrets",
        "connection",
        "transport",
        "providerSdk",
        "provider_sdk",
        "implementation",
    }
)
MANDATORY_CALLBACK_FAILURE_POLICIES = frozenset({"pause_run_on_failure", "fail_run_on_failure"})
VALID_CALLBACK_SUBSCRIPTION_SCOPES = frozenset({"run", "conversation", "project", "tenant", "deployment"})
VALID_CALLBACK_DELIVERY_KINDS = frozenset({
    "webhook",
    "websocket",
    "sse",
    "push_notification",
    "email",
    "local_callback",
})
ORDER_CAPABLE_CALLBACK_TARGETS = frozenset({"webhook", "websocket", "sse"})
DEFAULT_CALLBACK_MAX_PAYLOAD_BYTES = 262_144


def _config_error_path(base: str, error: ValidationError) -> str:
    path = base
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            path += f".{part}"
        else:
            path += f"[{canonical_dumps(part)}]"
    return path


def _config_error_message(error: ValidationError) -> str:
    if error.validator == "type":
        return f"value must have JSON type {canonical_dumps(error.validator_value)}"
    if error.validator == "required" and isinstance(error.instance, Mapping):
        required = error.validator_value
        if isinstance(required, list):
            missing = sorted(
                item
                for item in required
                if isinstance(item, str) and item not in error.instance
            )
            return f"required properties are missing: {canonical_dumps(missing)}"
    if error.validator == "additionalProperties" and isinstance(error.instance, Mapping):
        declared = error.schema.get("properties", {})
        if isinstance(declared, Mapping):
            unexpected = sorted(str(key) for key in error.instance if key not in declared)
            return f"unexpected properties are not allowed: {canonical_dumps(unexpected)}"
    if error.validator == "const":
        return f"value must equal {canonical_dumps(error.validator_value)}"
    if error.validator == "enum":
        return f"value must be one of {canonical_dumps(error.validator_value)}"
    if error.validator == "uniqueItems":
        return "array items must be unique"
    if error.validator == "oneOf":
        return "value must match exactly one allowed schema"
    if error.validator == "anyOf":
        return "value must match at least one allowed schema"
    if error.validator == "not":
        return "value matches a forbidden schema"
    return error.message


@dataclass(frozen=True, slots=True)
class Plan:
    normalized: dict[str, Any]
    graph_hash: str
    diagnostics: DiagnosticSet

    @property
    def ok(self) -> bool:
        return self.diagnostics.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.graph_hash,
            "ok": self.ok,
            "diagnostics": self.diagnostics.to_list(),
            "graph": self.normalized,
        }


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _has_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_canonical_sha256_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _has_async_relative_timeout(config: dict[str, Any]) -> bool:
    timeout = (
        config.get("timeout")
        or config.get("timeoutMs")
        or config.get("timeout_ms")
        or config.get("deadline")
    )
    timeout_ms = _duration_milliseconds(timeout)
    return timeout_ms is not None and timeout_ms > 0


def _has_async_absolute_deadline(config: dict[str, Any]) -> bool:
    return _is_positive_integer(config.get("expiresAtUnixMs") or config.get("expires_at_unix_ms"))


def _has_async_explicit_infinite_wait(config: dict[str, Any]) -> bool:
    infinite_wait = config.get("infiniteWait", config.get("infinite_wait", False))
    explicit_infinite_wait_policy = config.get("infiniteWaitPolicy") or config.get("infinite_wait_policy")
    return infinite_wait is True or _has_non_empty_string(explicit_infinite_wait_policy)


def _invalid_optional_duration_field(config: dict[str, Any], *names: str) -> str | None:
    for name in names:
        if name in config and _duration_milliseconds(config.get(name)) is None:
            return name
    return None


def _has_async_idempotency_key(config: dict[str, Any]) -> bool:
    return _has_non_empty_string(config.get("idempotencyKey") or config.get("idempotency_key"))


def _has_async_callback_schema(config: dict[str, Any]) -> bool:
    callback = config.get("callback")
    if isinstance(callback, dict):
        schema = (
            callback.get("schema")
            or callback.get("acceptedSchema")
            or callback.get("accepted_schema")
            or callback.get("expectedSchema")
            or callback.get("expected_schema")
        )
        return _has_non_empty_string(schema)
    return _has_non_empty_string(config.get("callbackSchema") or config.get("callback_schema"))


def _has_async_callback_completion_ref(config: dict[str, Any]) -> bool:
    callback = config.get("callback")
    return (
        isinstance(callback, dict)
        or _has_non_empty_string(config.get("callbackRef") or config.get("callback_ref"))
    )


def _has_async_polling_completion_ref(config: dict[str, Any]) -> bool:
    polling = config.get("polling")
    return (
        isinstance(polling, dict)
        or _has_non_empty_string(config.get("pollingRef") or config.get("polling_ref"))
    )


def _configured_positive_integer(config: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = config.get(name)
        if _is_positive_integer(value):
            return value
    return None


def _truthy_config_flag(config: dict[str, Any], *names: str) -> bool:
    return any(config.get(name) is True for name in names)


def _duration_milliseconds(value: object) -> int | None:
    if _is_positive_integer(value):
        return int(value)
    if not isinstance(value, str):
        return None
    duration_text = value.strip()
    duration_units = (
        ("ms", 1),
        ("s", 1_000),
        ("m", 60_000),
        ("h", 3_600_000),
        ("d", 86_400_000),
    )
    for suffix, multiplier in duration_units:
        if not duration_text.endswith(suffix):
            continue
        amount_text = duration_text[: -len(suffix)]
        if amount_text.isascii() and amount_text.isdigit() and int(amount_text) > 0:
            return int(amount_text) * multiplier
    return None


def _callback_schema_required(config: dict[str, Any]) -> bool:
    callback = config.get("callback")
    if isinstance(callback, dict):
        return callback.get("required", True) is not False
    return "callback" in config or "callbackSchema" in config or "callback_schema" in config


def _has_async_resume_reevaluation(config: dict[str, Any]) -> bool:
    resume = config.get("resume")
    if not isinstance(resume, dict):
        resume = {}
    policy_ok = _truthy_config_flag(
        resume,
        "requirePolicyReevaluation",
        "require_policy_reevaluation",
        "policyReevaluation",
        "policy_reevaluation",
    ) or _truthy_config_flag(
        config,
        "requirePolicyReevaluation",
        "require_policy_reevaluation",
    )
    budget_ok = _truthy_config_flag(
        resume,
        "requireBudgetReservation",
        "require_budget_reservation",
        "budgetReservation",
        "budget_reservation",
    ) or _truthy_config_flag(
        config,
        "requireBudgetReservation",
        "require_budget_reservation",
    )
    release_ok = _truthy_config_flag(
        resume,
        "requireReleaseCompatibility",
        "require_release_compatibility",
        "releaseCompatibility",
        "release_compatibility",
    ) or _truthy_config_flag(
        config,
        "requireReleaseCompatibility",
        "require_release_compatibility",
    )
    return policy_ok and budget_ok and release_ok


def _has_async_attempt_fencing(config: dict[str, Any]) -> bool:
    callback = config.get("callback")
    callback_config = callback if isinstance(callback, dict) else {}
    return (
        _truthy_config_flag(config, "attemptFencing", "attempt_fencing", "fencingTokenRequired", "fencing_token_required")
        or _truthy_config_flag(
            callback_config,
            "attemptFencing",
            "attempt_fencing",
            "fencingTokenRequired",
            "fencing_token_required",
        )
    )


def _has_async_ownership_fence(config: dict[str, Any]) -> bool:
    resume = config.get("resume")
    resume_config = resume if isinstance(resume, dict) else {}
    return (
        _truthy_config_flag(config, "ownershipFence", "ownership_fence", "runOwnershipLease", "run_ownership_lease")
        or _truthy_config_flag(
            resume_config,
            "requireOwnershipFence",
            "require_ownership_fence",
            "ownershipFence",
            "ownership_fence",
            "runOwnershipLease",
            "run_ownership_lease",
        )
    )


def _diagnose_async_operation_config(
    diagnostics: list[Diagnostic],
    config: dict[str, Any],
    path: str,
    *,
    require_callback_schema: bool = False,
) -> None:
    callback = config.get("callback")
    callback_config = callback if isinstance(callback, dict) else {}
    if _has_async_callback_completion_ref(config) and _has_async_polling_completion_ref(config):
        diagnostics.append(
            Diagnostic(
                "GB1026",
                "async operation must not define both callback and polling completion refs",
                path,
            )
        )
    resume_token_hash = config.get("resumeTokenHash", config.get("resume_token_hash"))
    if resume_token_hash is not None and not _is_canonical_sha256_digest(resume_token_hash):
        diagnostics.append(
            Diagnostic(
                "GB1026",
                "async operation resumeTokenHash must be a canonical sha256 digest",
                f"{path}.resumeTokenHash",
            )
        )
    has_relative_timeout = _has_async_relative_timeout(config)
    has_absolute_deadline = _has_async_absolute_deadline(config)
    has_bounded_timeout = has_relative_timeout or has_absolute_deadline
    has_infinite_wait = _has_async_explicit_infinite_wait(config)
    if has_relative_timeout and has_absolute_deadline:
        diagnostics.append(
            Diagnostic(
                "GB1026",
                "async operation wait must not define both expiresAtUnixMs and timeout",
                path,
            )
        )
    if has_bounded_timeout and has_infinite_wait:
        diagnostics.append(
            Diagnostic(
                "GB1026",
                "async operation wait must not define both timeout and infinite-wait policy",
                path,
            )
        )
    if not has_bounded_timeout and not has_infinite_wait:
        diagnostics.append(
            Diagnostic(
                "GB6001",
                "async operation callback waits require a timeout or explicit infinite-wait policy",
                path,
            )
        )
    if not _has_async_idempotency_key(config):
        diagnostics.append(
            Diagnostic(
                "GB6003",
                "async operation callbacks require an idempotency key",
                path,
            )
        )
    on_timeout = config.get("onTimeout", config.get("on_timeout"))
    if isinstance(on_timeout, str) and on_timeout not in {"fail", "cancel", "expire"}:
        diagnostics.append(
            Diagnostic(
                "GB1026",
                "async await onTimeout must be one of fail, cancel, or expire",
                f"{path}.onTimeout",
            )
        )
    for field, names in (
        ("interval", ("interval", "intervalMs", "interval_ms")),
        ("maxInterval", ("maxInterval", "max_interval", "maxIntervalMs", "max_interval_ms")),
    ):
        if _invalid_optional_duration_field(config, *names) is not None:
            diagnostics.append(
                Diagnostic(
                    "GB1026",
                    f"async operation {field} must be a positive duration",
                    f"{path}.{field}",
                )
            )
    if (require_callback_schema or _callback_schema_required(config)) and not _has_async_callback_schema(config):
        diagnostics.append(
            Diagnostic(
                "GB6007",
                "async operation callbacks require an expected callback schema",
                f"{path}.callback",
            )
        )
    for payload_field, field_names in (
        (
            "expectedPayloadBytes",
            (
                "expectedPayloadBytes",
                "expected_payload_bytes",
                "expectedMaxPayloadBytes",
                "expected_max_payload_bytes",
            ),
        ),
        ("maxPayloadBytes", ("maxPayloadBytes", "max_payload_bytes")),
    ):
        for payload_config, payload_path in ((callback_config, f"{path}.callback"), (config, path)):
            for field_name in field_names:
                if field_name in payload_config and not _is_positive_integer(payload_config.get(field_name)):
                    diagnostics.append(
                        Diagnostic(
                            "GB1026",
                            f"async callback {payload_field} must be a positive integer",
                            f"{payload_path}.{field_name}",
                        )
                    )
    expected_payload_bytes = _configured_positive_integer(
        callback_config,
        "expectedPayloadBytes",
        "expected_payload_bytes",
        "expectedMaxPayloadBytes",
        "expected_max_payload_bytes",
    ) or _configured_positive_integer(
        config,
        "expectedPayloadBytes",
        "expected_payload_bytes",
        "expectedMaxPayloadBytes",
        "expected_max_payload_bytes",
    )
    max_payload_bytes = _configured_positive_integer(
        callback_config,
        "maxPayloadBytes",
        "max_payload_bytes",
    ) or _configured_positive_integer(
        config,
        "maxPayloadBytes",
        "max_payload_bytes",
    ) or DEFAULT_CALLBACK_MAX_PAYLOAD_BYTES
    if expected_payload_bytes is not None and expected_payload_bytes > max_payload_bytes:
        diagnostics.append(
            Diagnostic(
                "GB6010",
                "async callback payload contract exceeds the configured inline payload limit",
                f"{path}.callback.maxPayloadBytes",
            )
        )
    if not _has_async_resume_reevaluation(config):
        diagnostics.append(
            Diagnostic(
                "GB6008",
                "callback resume must re-evaluate policy, budget, and release compatibility",
                f"{path}.resume",
            )
        )
    if not _has_async_attempt_fencing(config):
        diagnostics.append(
            Diagnostic(
                "GB6015",
                "async callbacks require attempt fencing so stale callbacks cannot resume newer attempts",
                path,
            )
        )
    if not _has_async_ownership_fence(config):
        diagnostics.append(
            Diagnostic(
                "GB6016",
                "callback resume requires run ownership lease or fencing protection",
                f"{path}.resume",
            )
        )


def _is_background_run(execution: dict[str, Any]) -> bool:
    mode = (
        execution.get("runLifetime")
        or execution.get("run_lifetime")
        or execution.get("lifetime")
        or execution.get("invocationMode")
        or execution.get("invocation_mode")
        or execution.get("responseMode")
        or execution.get("response_mode")
    )
    return mode in {"accepted", "background", "job"}


def _execution_is_client_bound(execution: dict[str, Any]) -> bool:
    if _truthy_config_flag(
        execution,
        "clientConnectionRequired",
        "client_connection_required",
        "websocketRequired",
        "websocket_required",
        "processBound",
        "process_bound",
    ):
        return True
    detach = execution.get("detach")
    if isinstance(detach, dict):
        disconnect_behavior = detach.get("onClientDisconnect") or detach.get("on_client_disconnect")
        return disconnect_behavior in {"cancel", "cancel_run", "client_connection"}
    return False


def _event_stream_is_replayable(event_stream: dict[str, Any] | None) -> bool:
    if event_stream is None:
        return False
    return _truthy_config_flag(
        event_stream,
        "replayable",
        "cursorReplay",
        "cursor_replay",
        "authoritative",
    )


def _diagnose_background_execution_config(
    diagnostics: list[Diagnostic],
    execution: dict[str, Any],
    event_stream: dict[str, Any] | None,
) -> None:
    if not _is_background_run(execution):
        return
    if not _event_stream_is_replayable(event_stream):
        diagnostics.append(
            Diagnostic(
                "GB6005",
                "background runs require a replayable ApplicationEventStream",
                "$.spec.eventStream",
            )
        )
    if _execution_is_client_bound(execution):
        diagnostics.append(
            Diagnostic(
                "GB6009",
                "background or job runs must not be bound to a single client connection",
                "$.spec.execution",
            )
        )
    if event_stream is None:
        return
    retention = _duration_milliseconds(
        event_stream.get("retention")
        or event_stream.get("eventRetention")
        or event_stream.get("event_retention")
        or event_stream.get("retentionDuration")
        or event_stream.get("retention_duration")
    )
    replay_guarantee = _duration_milliseconds(
        event_stream.get("reconnectReplayGuarantee")
        or event_stream.get("reconnect_replay_guarantee")
        or event_stream.get("replayGuarantee")
        or event_stream.get("replay_guarantee")
    )
    if retention is not None and replay_guarantee is not None and retention < replay_guarantee:
        diagnostics.append(
            Diagnostic(
                "GB6013",
                "event retention is shorter than the declared reconnect replay guarantee",
                "$.spec.eventStream.retention",
            )
        )


def _has_callback_signing(delivery: dict[str, Any]) -> bool:
    signing = delivery.get("signing")
    if not isinstance(signing, dict):
        return False
    algorithm = signing.get("algorithm")
    secret_ref = signing.get("secretRef") or signing.get("secret_ref")
    return algorithm in {"hmac-sha256", "ed25519"} and _has_non_empty_string(secret_ref)


def _callback_url_is_unsafe(url: object) -> bool:
    return not validate_webhook_url(
        url,
        allowed_schemes=frozenset({"https"}),
        allow_private=False,
    ).allowed


def _has_callback_dead_letter_behavior(config: dict[str, Any], delivery: dict[str, Any]) -> bool:
    dead_letter = (
        config.get("deadLetterPolicy")
        or config.get("dead_letter_policy")
        or config.get("deadLetterRef")
        or config.get("dead_letter_ref")
        or delivery.get("deadLetterPolicy")
        or delivery.get("dead_letter_policy")
        or delivery.get("deadLetterRef")
        or delivery.get("dead_letter_ref")
    )
    fallback = (
        config.get("fallbackPolicy")
        or config.get("fallback_policy")
        or config.get("fallbackRef")
        or config.get("fallback_ref")
        or delivery.get("fallbackPolicy")
        or delivery.get("fallback_policy")
        or delivery.get("fallbackRef")
        or delivery.get("fallback_ref")
    )
    return (
        _has_non_empty_string(dead_letter)
        or isinstance(dead_letter, dict)
        or _has_non_empty_string(fallback)
        or isinstance(fallback, dict)
    )


def _diagnose_callback_subscription_config(
    diagnostics: list[Diagnostic],
    config: dict[str, Any],
    path: str,
) -> None:
    delivery = config.get("delivery")
    delivery_is_mapping = isinstance(delivery, dict)
    if not delivery_is_mapping:
        diagnostics.append(
            Diagnostic(
                "GB1027",
                "callback subscription delivery must be a mapping",
                f"{path}.delivery",
            )
        )
        delivery = {}
    scope = config.get("scope")
    if scope not in VALID_CALLBACK_SUBSCRIPTION_SCOPES:
        diagnostics.append(
            Diagnostic(
                "GB1027",
                "callback subscription scope must be one of run, conversation, project, tenant, or deployment",
                f"{path}.scope",
            )
        )
    delivery_kind = delivery.get("kind")
    if delivery_is_mapping and delivery_kind not in VALID_CALLBACK_DELIVERY_KINDS:
        diagnostics.append(
            Diagnostic(
                "GB1027",
                "callback delivery kind must be one of webhook, websocket, sse, push_notification, email, or local_callback",
                f"{path}.delivery.kind",
            )
        )
    if delivery_kind == "webhook":
        method = delivery.get("method", "POST")
        if method != "POST":
            diagnostics.append(
                Diagnostic(
                    "GB1027",
                    "webhook callback delivery method must be POST",
                    f"{path}.delivery.method",
                )
            )
        if not _has_callback_signing(delivery):
            diagnostics.append(
                Diagnostic(
                    "GB6002",
                    "webhook callback subscriptions require signing configuration",
                    f"{path}.delivery.signing",
                )
            )
        if _callback_url_is_unsafe(delivery.get("url")):
            diagnostics.append(
                Diagnostic(
                    "GB6011",
                    "webhook callback endpoint is unsafe or forbidden by default egress policy",
                    f"{path}.delivery.url",
                )
            )

    authoritative_for = config.get("authoritativeFor", config.get("authoritative_for"))
    if config.get("sourceOfTruth") is True or config.get("source_of_truth") is True or authoritative_for:
        diagnostics.append(
            Diagnostic(
                "GB6004",
                "callback delivery must not be used as the source of truth for run correctness or accounting",
                path,
            )
        )

    failure_policy = config.get("failurePolicy") or config.get("failure_policy")
    mandatory = (
        config.get("mandatory") is True
        or delivery.get("mandatory") is True
        or failure_policy in MANDATORY_CALLBACK_FAILURE_POLICIES
    )
    retry_policy = (
        config.get("retryPolicyRef")
        or config.get("retry_policy_ref")
        or delivery.get("retryPolicyRef")
        or delivery.get("retry_policy_ref")
    )
    has_retry_policy = _has_non_empty_string(retry_policy) or isinstance(retry_policy, dict)
    if (
        mandatory
        and not failure_policy
        and not has_retry_policy
        and not _has_callback_dead_letter_behavior(config, delivery)
    ):
        diagnostics.append(
            Diagnostic(
                "GB6006",
                "mandatory callback delivery requires retry, dead-letter, or fallback failure policy",
                f"{path}.failurePolicy",
            )
        )

    ordering = delivery.get("ordering")
    if not isinstance(ordering, dict):
        ordering = config.get("ordering")
    if isinstance(ordering, dict) and ordering.get("mode") == "ordered" and delivery_kind not in ORDER_CAPABLE_CALLBACK_TARGETS:
        diagnostics.append(
            Diagnostic(
                "GB6012",
                "callback subscription requests ordered delivery on a target that cannot guarantee it",
                f"{path}.delivery.ordering",
            )
        )

    if (
        failure_policy in {*MANDATORY_CALLBACK_FAILURE_POLICIES, "retry_then_dead_letter"}
        and not _has_callback_dead_letter_behavior(config, delivery)
    ):
        diagnostics.append(
            Diagnostic(
                "GB6014",
                "mandatory callback failure policy requires dead-letter or fallback behavior",
                f"{path}.deadLetterPolicy",
            )
        )


def compile_graph(
    document: dict[str, Any],
    block_catalog: BlockCatalog | None = None,
    *,
    allow_unknown_blocks: bool = False,
) -> Plan:
    if not isinstance(allow_unknown_blocks, bool):
        raise TypeError("allow_unknown_blocks must be a boolean")
    api_version = document.get("apiVersion")
    if block_catalog is None:
        profile = "stable" if api_version == GRAPH_API_VERSION else "preview"
        block_catalog = builtin_block_catalog(profile=profile)
    if allow_unknown_blocks and not block_catalog.allow_unknown_blocks:
        block_catalog = BlockCatalog(
            block_catalog.descriptors,
            allow_unknown_blocks=True,
        )

    diagnostics: list[Diagnostic] = []
    domain_violations = tuple(
        violation
        for violation in resource_schema_errors(document)
        if violation.keyword
        in {
            "finiteNumber",
            "jsonObjectKey",
            "jsonValue",
            "maxDepth",
            "recursive",
            "unicodeScalar",
        }
    )
    if domain_violations:
        invalid_resource_identity = {
            "invalidResource": [
                {
                    "code": violation.code,
                    "keyword": violation.keyword,
                    "message": violation.message,
                    "path": violation.path,
                }
                for violation in domain_violations
            ]
        }
        return Plan(
            document,
            canonical_hash(invalid_resource_identity),
            DiagnosticSet(
                tuple(
                    Diagnostic(
                        violation.code,
                        violation.message,
                        violation.path,
                    )
                    for violation in domain_violations
                )
            ),
        )
    try:
        migrated = migrate_document(document)
    except MigrationError:
        migrated = document
    if migrated.get("kind") != "Graph":
        diagnostics.append(Diagnostic("GB0001", "document kind must be Graph", "$.kind"))
        normalized = _normalize_graph_unchecked(migrated)
        return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))

    schema_violations = ()
    if api_version not in {GRAPH_API_VERSION, *LEGACY_GRAPH_API_VERSIONS}:
        diagnostics.append(
            Diagnostic("GB0002", f"unsupported Graph apiVersion {api_version!r}", "$.apiVersion")
        )
    else:
        schema_violations = resource_schema_errors(migrated)

    metadata = migrated.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"]:
        diagnostics.append(Diagnostic("GB0003", "metadata.name is required", "$.metadata.name"))

    spec = migrated.get("spec")
    if not isinstance(spec, dict):
        diagnostics.append(Diagnostic("GB0004", "spec must be a mapping", "$.spec"))
        normalized = _normalize_graph_unchecked(migrated)
        diagnostic_paths = {diagnostic.path for diagnostic in diagnostics}
        schema_diagnostics = [
            Diagnostic(violation.code, violation.message, violation.path)
            for violation in schema_violations
            if violation.keyword == "additionalProperties"
            or violation.path not in diagnostic_paths
        ]
        return Plan(
            normalized,
            canonical_hash(normalized),
            DiagnosticSet(tuple([*schema_diagnostics, *diagnostics])),
        )

    nodes = spec.get("nodes", {})
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, dict):
        diagnostics.append(Diagnostic("GB0005", "spec.nodes must be a mapping", "$.spec.nodes"))
        nodes = {}

    if "composition" in spec:
        diagnostics.append(
            Diagnostic(
                "GB1052",
                "graph composition must be materialized before compilation",
                "$.spec.composition",
            )
        )

    interface = spec.get("interface")
    if isinstance(interface, dict):
        for direction in ("inputs", "outputs"):
            ports = interface.get(direction)
            if isinstance(ports, dict):
                for port_name, schema_id in ports.items():
                    path = f"$.spec.interface.{direction}.{port_name}"
                    if not isinstance(schema_id, str):
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"graph interface {direction[:-1]} schema id must be a string",
                                path,
                            )
                        )
                        continue
                    try:
                        SchemaId.parse(schema_id)
                    except SchemaIdError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"graph interface {direction[:-1]} schema id is invalid: {error}",
                                path,
                            )
                        )

    for node_name, node in nodes.items():
        if not isinstance(node_name, str) or not node_name:
            diagnostics.append(Diagnostic("GB0006", "node name must be a non-empty string", "$.spec.nodes"))
            continue
        if node_name.startswith("$"):
            diagnostics.append(Diagnostic("GB0007", "node names cannot use pseudo-node prefix '$'", f"$.spec.nodes.{node_name}"))
        if not isinstance(node, dict):
            diagnostics.append(Diagnostic("GB0008", "node spec must be a mapping", f"$.spec.nodes.{node_name}"))
            continue
        if "slot" in node:
            diagnostics.append(
                Diagnostic(
                    "GB1052",
                    "slot placeholders must be materialized before compilation",
                    f"$.spec.nodes.{node_name}.slot",
                )
            )
            continue
        block = node.get("block")
        if not isinstance(block, str) or "@" not in block or block.endswith("@"):
            diagnostics.append(Diagnostic("GB0009", "node.block must use '<type>@<major>'", f"$.spec.nodes.{node_name}.block"))
        block_type = block.split("@", 1)[0] if isinstance(block, str) and "@" in block else None
        if block_type in {"async.start_operation", "async.await_callback", "async.poll_operation"}:
            config = node.get("config", {})
            if config is None:
                config = {}
            if isinstance(config, dict):
                _diagnose_async_operation_config(
                    diagnostics,
                    config,
                    f"$.spec.nodes.{node_name}.config",
                    require_callback_schema=block_type == "async.await_callback",
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        "GB1026",
                        "async operation node config must be a mapping",
                        f"$.spec.nodes.{node_name}.config",
                    )
                )
        if "connection" in node and "bindings" in node:
            diagnostics.append(
                Diagnostic(
                    "GB1006",
                    "connection shorthand cannot be combined with explicit bindings",
                    f"$.spec.nodes.{node_name}",
                )
            )
        effects = node.get("effects", [])
        if isinstance(effects, str):
            effects = [effects]
        effect_set = {str(effect) for effect in effects} if isinstance(effects, list) else set()
        flow = node.get("flow", {})
        if isinstance(flow, dict) and "timeout" in flow and parse_duration_seconds(flow["timeout"]) is None:
            diagnostics.append(
                Diagnostic(
                    "GB1019",
                    "flow.timeout must be a positive finite duration",
                    f"$.spec.nodes.{node_name}.flow.timeout",
                )
            )
        retry = flow.get("retry", {}) if isinstance(flow, dict) else {}
        max_attempts = 1
        idempotency_key = None
        if isinstance(retry, dict):
            configured_max_attempts = retry.get("maxAttempts", retry.get("max_attempts", 1))
            if isinstance(configured_max_attempts, int) and not isinstance(configured_max_attempts, bool):
                max_attempts = configured_max_attempts
            idempotency_key = retry.get("idempotencyKey") or retry.get("idempotency_key")
        elif isinstance(retry, int) and not isinstance(retry, bool):
            max_attempts = retry
        effect_retry_requires_key = bool(effect_set & STATE_CHANGING_TOOL_EFFECTS)
        valid_idempotency_key = (
            isinstance(idempotency_key, str)
            and bool(idempotency_key.strip())
            and idempotency_key == idempotency_key.strip()
        )
        if effect_retry_requires_key and max_attempts > 1 and not valid_idempotency_key:
            diagnostics.append(
                Diagnostic(
                    "GB1011",
                    "retrying effectful nodes requires an idempotency key",
                    f"$.spec.nodes.{node_name}.flow.retry",
                )
            )

    execution = spec.get("execution")
    if isinstance(execution, dict):
        event_stream_key = "eventStream" if "eventStream" in spec else "event_stream"
        event_stream = spec.get(event_stream_key)
        _diagnose_background_execution_config(
            diagnostics,
            execution,
            event_stream if isinstance(event_stream, dict) else None,
        )

    async_operations_key = "asyncOperations" if "asyncOperations" in spec else "async_operations"
    async_operations = spec.get(async_operations_key)
    if async_operations is not None:
        if isinstance(async_operations, dict):
            for operation_key, operation_config in async_operations.items():
                operation_path = f"$.spec.{async_operations_key}.{operation_key}"
                if not isinstance(operation_config, dict):
                    diagnostics.append(
                        Diagnostic(
                            "GB1026",
                            "async operation config must be a mapping",
                            operation_path,
                        )
                    )
                    continue
                _diagnose_async_operation_config(diagnostics, operation_config, operation_path)
        elif isinstance(async_operations, list):
            for operation_index, operation_config in enumerate(async_operations):
                operation_path = f"$.spec.{async_operations_key}[{operation_index}]"
                if not isinstance(operation_config, dict):
                    diagnostics.append(
                        Diagnostic(
                            "GB1026",
                            "async operation config must be a mapping",
                            operation_path,
                        )
                    )
                    continue
                _diagnose_async_operation_config(diagnostics, operation_config, operation_path)
        else:
            diagnostics.append(
                Diagnostic(
                    "GB1026",
                    "asyncOperations must be a mapping or list",
                    f"$.spec.{async_operations_key}",
                )
            )

    callback_subscriptions_key = "callbackSubscriptions" if "callbackSubscriptions" in spec else "callback_subscriptions"
    callback_subscriptions = spec.get(callback_subscriptions_key)
    if callback_subscriptions is not None:
        if isinstance(callback_subscriptions, dict):
            for subscription_key, subscription_config in callback_subscriptions.items():
                subscription_path = f"$.spec.{callback_subscriptions_key}.{subscription_key}"
                if not isinstance(subscription_config, dict):
                    diagnostics.append(
                        Diagnostic(
                            "GB1027",
                            "callback subscription config must be a mapping",
                            subscription_path,
                        )
                    )
                    continue
                _diagnose_callback_subscription_config(diagnostics, subscription_config, subscription_path)
        elif isinstance(callback_subscriptions, list):
            for subscription_index, subscription_config in enumerate(callback_subscriptions):
                subscription_path = f"$.spec.{callback_subscriptions_key}[{subscription_index}]"
                if not isinstance(subscription_config, dict):
                    diagnostics.append(
                        Diagnostic(
                            "GB1027",
                            "callback subscription config must be a mapping",
                            subscription_path,
                        )
                    )
                    continue
                _diagnose_callback_subscription_config(diagnostics, subscription_config, subscription_path)
        else:
            diagnostics.append(
                Diagnostic(
                    "GB1027",
                    "callbackSubscriptions must be a mapping or list",
                    f"$.spec.{callback_subscriptions_key}",
                )
            )

    output_policy_key = "outputPolicy" if "outputPolicy" in spec else "output_policy"
    output_policy = spec.get(output_policy_key)
    if output_policy is not None and not isinstance(output_policy, dict):
        diagnostics.append(
            Diagnostic(
                "GB1034",
                "outputPolicy must be a mapping",
                f"$.spec.{output_policy_key}",
            )
        )
        output_policy = None
    if output_policy is not None:
        delivery = output_policy.get("delivery")
        if delivery is not None and not isinstance(delivery, dict):
            diagnostics.append(
                Diagnostic(
                    "GB1034",
                    "outputPolicy delivery must be a mapping",
                    f"$.spec.{output_policy_key}.delivery",
                )
            )
            delivery = None
        if delivery is not None:
            mode = delivery.get("mode")
            if mode is not None and (not isinstance(mode, str) or mode not in VALID_OUTPUT_DELIVERY_MODES):
                diagnostics.append(
                    Diagnostic(
                        "GB1030",
                        f"invalid output delivery mode {mode}",
                        "$.spec.outputPolicy.delivery.mode",
                    )
                )
            delivery_on_violation = delivery.get("onViolation", delivery.get("on_violation"))
            if delivery_on_violation is not None and (
                not isinstance(delivery_on_violation, str) or delivery_on_violation not in VALID_VIOLATION_ACTIONS
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1044",
                        f"invalid violation action {delivery_on_violation}",
                        "$.spec.outputPolicy.delivery.onViolation",
                    )
                )
            if "deliveredDraftDisposition" in delivery:
                delivered_draft_disposition = delivery.get("deliveredDraftDisposition")
                delivered_draft_path = "$.spec.outputPolicy.delivery.deliveredDraftDisposition"
            else:
                delivered_draft_disposition = delivery.get("delivered_draft_disposition")
                delivered_draft_path = "$.spec.outputPolicy.delivery.delivered_draft_disposition"
            if delivered_draft_disposition is not None and (
                not isinstance(delivered_draft_disposition, str)
                or delivered_draft_disposition not in VALID_DRAFT_DISPOSITIONS
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1028",
                        f"invalid draft disposition {delivered_draft_disposition}",
                        delivered_draft_path,
                    )
                )
            if "flushBoundaries" in delivery:
                flush_boundaries = delivery.get("flushBoundaries")
                flush_boundaries_path = "$.spec.outputPolicy.delivery.flushBoundaries"
            else:
                flush_boundaries = delivery.get("flush_boundaries")
                flush_boundaries_path = "$.spec.outputPolicy.delivery.flush_boundaries"
            if flush_boundaries is not None:
                if isinstance(flush_boundaries, list):
                    for boundary_index, boundary in enumerate(flush_boundaries):
                        if not isinstance(boundary, str) or boundary not in VALID_FLUSH_BOUNDARIES:
                            diagnostics.append(
                                Diagnostic(
                                    "GB1029",
                                    f"invalid flush boundary {boundary}",
                                    f"{flush_boundaries_path}[{boundary_index}]",
                                )
                            )
                else:
                    diagnostics.append(
                        Diagnostic(
                            "GB1029",
                            "flush boundaries must be a list of strings",
                            flush_boundaries_path,
                        )
                    )
            if mode == "bounded_holdback":
                holdback_max_tokens = delivery.get("holdbackMaxTokens", delivery.get("holdback_max_tokens"))
                holdback_max_bytes = delivery.get("holdbackMaxBytes", delivery.get("holdback_max_bytes"))
                holdback_max_duration = (
                    delivery.get("holdbackMaxDuration")
                    or delivery.get("holdback_max_duration")
                    or delivery.get("holdbackMaxDurationMs")
                    or delivery.get("holdback_max_duration_ms")
                )
                has_token_bound = _is_positive_integer(holdback_max_tokens)
                has_byte_bound = _is_positive_integer(holdback_max_bytes)
                has_duration_bound = _is_positive_integer(holdback_max_duration)
                if not has_duration_bound and isinstance(holdback_max_duration, str):
                    duration_text = holdback_max_duration.strip()
                    for suffix in ("ms", "s", "m", "h"):
                        if duration_text.endswith(suffix):
                            duration_amount = duration_text[: -len(suffix)]
                            has_duration_bound = (
                                duration_amount.isascii() and duration_amount.isdigit() and int(duration_amount) > 0
                            )
                            break
                if not has_token_bound and not has_byte_bound and not has_duration_bound:
                    diagnostics.append(
                        Diagnostic(
                            "GB1051",
                            "bounded_holdback output delivery requires a token, byte, or duration bound",
                            "$.spec.outputPolicy.delivery",
                        )
                    )

            if mode == "immediate_draft":
                delivered_draft_disposition = delivery.get(
                    "deliveredDraftDisposition",
                    delivery.get("delivered_draft_disposition", "retract"),
                )
                if delivered_draft_disposition == "keep":
                    diagnostics.append(
                        Diagnostic(
                            "GB1025",
                            "immediate_draft output delivery requires incomplete or retracted draft semantics",
                            "$.spec.outputPolicy.delivery.deliveredDraftDisposition",
                        )
                    )

        evaluation = (
            output_policy.get("evaluation")
            or output_policy.get("outputEvaluation")
            or output_policy.get("output_evaluation")
        )
        if evaluation is not None and not isinstance(evaluation, dict):
            diagnostics.append(
                Diagnostic(
                    "GB1034",
                    "outputPolicy evaluation must be a mapping",
                    f"$.spec.{output_policy_key}.evaluation",
                )
            )
            evaluation = None
        enforcement_points = None
        if evaluation is not None:
            enforcement_points = evaluation.get("enforcementPoints") or evaluation.get("enforcement_points")
        if isinstance(enforcement_points, list):
            on_generation_chunk_index = None
            before_client_delivery_index = None
            before_output_commit_index = None
            for index, enforcement_point in enumerate(enforcement_points):
                if not isinstance(enforcement_point, str) or enforcement_point not in VALID_POLICY_ENFORCEMENT_POINTS:
                    diagnostics.append(
                        Diagnostic(
                            "GB1033",
                            f"invalid output policy enforcement point {enforcement_point}",
                            f"$.spec.outputPolicy.evaluation.enforcementPoints[{index}]",
                        )
                    )
                if enforcement_point == "on_generation_chunk":
                    on_generation_chunk_index = index
                elif enforcement_point == "before_client_delivery":
                    before_client_delivery_index = index
                elif enforcement_point == "before_output_commit":
                    before_output_commit_index = index
            if before_client_delivery_index is None:
                diagnostics.append(
                    Diagnostic(
                        "GB1046",
                        "output policy enforcement must include the before_client_delivery gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            elif on_generation_chunk_index is None:
                diagnostics.append(
                    Diagnostic(
                        "GB1046",
                        "output policy enforcement must include the on_generation_chunk gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            elif before_output_commit_index is None:
                diagnostics.append(
                    Diagnostic(
                        "GB1046",
                        "output policy enforcement must include the before_output_commit gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            if (
                before_client_delivery_index is not None
                and on_generation_chunk_index is not None
                and before_client_delivery_index < on_generation_chunk_index
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1048",
                        "on_generation_chunk policy evaluation must precede before_client_delivery",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
        elif enforcement_points is not None:
            diagnostics.append(
                Diagnostic(
                    "GB1033",
                    "output policy enforcementPoints must be a list of strings",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )
            diagnostics.append(
                Diagnostic(
                    "GB1046",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )
        else:
            diagnostics.append(
                Diagnostic(
                    "GB1046",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )

        on_violation = output_policy.get("onViolation") or output_policy.get("on_violation")
        if on_violation is not None and not isinstance(on_violation, dict):
            diagnostics.append(
                Diagnostic(
                    "GB1034",
                    "outputPolicy onViolation must be a mapping",
                    f"$.spec.{output_policy_key}.onViolation",
                )
            )
            on_violation = None
        if on_violation is not None:
            disposition = on_violation.get("disposition", "abort_response")
            valid_disposition = isinstance(disposition, str) and disposition in VALID_OUTPUT_DISPOSITIONS
            if not valid_disposition:
                diagnostics.append(
                    Diagnostic(
                        "GB1031",
                        f"invalid output disposition {disposition}",
                        "$.spec.outputPolicy.onViolation.disposition",
                    )
                )

            provider_cancellation = on_violation.get("providerCancellation", on_violation.get("provider_cancellation"))
            if isinstance(provider_cancellation, dict):
                provider_cancellation_mode = provider_cancellation.get("mode", "request")
                provider_cancellation_path = "$.spec.outputPolicy.onViolation.providerCancellation.mode"
            else:
                provider_cancellation_mode = provider_cancellation
                provider_cancellation_path = "$.spec.outputPolicy.onViolation.providerCancellation"
            if provider_cancellation_mode is not None and (
                not isinstance(provider_cancellation_mode, str)
                or provider_cancellation_mode not in VALID_PROVIDER_CANCELLATIONS
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1036",
                        f"invalid provider cancellation {provider_cancellation_mode}",
                        provider_cancellation_path,
                    )
                )

            pending_tool_calls = on_violation.get("pendingToolCalls") or on_violation.get("pending_tool_calls")
            pending_tool_calls = pending_tool_calls if isinstance(pending_tool_calls, dict) else {}
            pending_tool_calls_disposition = pending_tool_calls.get("disposition", "deny")
            valid_pending_tool_calls_disposition = (
                isinstance(pending_tool_calls_disposition, str)
                and pending_tool_calls_disposition in VALID_PENDING_TOOL_CALLS_DISPOSITIONS
            )
            if not valid_pending_tool_calls_disposition:
                diagnostics.append(
                    Diagnostic(
                        "GB1035",
                        f"invalid pending tool calls disposition {pending_tool_calls_disposition}",
                        "$.spec.outputPolicy.onViolation.pendingToolCalls.disposition",
                    )
                )

            delivered_draft = on_violation.get("deliveredDraft") or on_violation.get("delivered_draft")
            delivered_draft = delivered_draft if isinstance(delivered_draft, dict) else {}
            delivered_draft_disposition = delivered_draft.get("disposition", "retract")
            if not (
                isinstance(delivered_draft_disposition, str)
                and delivered_draft_disposition in VALID_DRAFT_DISPOSITIONS
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1028",
                        f"invalid draft disposition {delivered_draft_disposition}",
                        "$.spec.outputPolicy.onViolation.deliveredDraft.disposition",
                    )
                )

            durable_result = on_violation.get("durableResult") or on_violation.get("durable_result")
            durable_result = durable_result if isinstance(durable_result, dict) else {}
            durable_result_disposition = durable_result.get("disposition", "none")
            valid_durable_result_disposition = (
                isinstance(durable_result_disposition, str)
                and durable_result_disposition in VALID_OUTPUT_DURABLE_RESULTS
            )
            if not valid_durable_result_disposition:
                diagnostics.append(
                    Diagnostic(
                        "GB1032",
                        f"invalid output durable result {durable_result_disposition}",
                        "$.spec.outputPolicy.onViolation.durableResult.disposition",
                    )
                )

            if disposition in {"abort_response", "abort_turn"}:
                if valid_pending_tool_calls_disposition and pending_tool_calls_disposition == "keep":
                    diagnostics.append(
                        Diagnostic(
                            "GB1047",
                            "policy-aborted responses must deny or cancel pending tool calls",
                            "$.spec.outputPolicy.onViolation.pendingToolCalls.disposition",
                        )
                    )

                if valid_durable_result_disposition and durable_result_disposition != "none":
                    diagnostics.append(
                        Diagnostic(
                            "GB1024",
                            "policy-stopped responses must not commit a durable result",
                            "$.spec.outputPolicy.onViolation.durableResult.disposition",
                        )
                    )

    bindings = spec.get("bindings")
    bindings = bindings if isinstance(bindings, dict) else None
    tools = bindings.get("tools") if bindings is not None else None
    tools = tools if isinstance(tools, dict) else None
    if tools is not None:
        tool_execution_key = "toolExecution" if "toolExecution" in spec else "tool_execution"
        tool_execution = spec.get(tool_execution_key)
        if tool_execution is not None and not isinstance(tool_execution, dict):
            diagnostics.append(
                Diagnostic(
                    "GB1041",
                    "toolExecution must be a mapping",
                    f"$.spec.{tool_execution_key}",
                )
            )
            tool_execution = None
        maximum_parallelism = 1
        parallel_tool_calls = False
        has_effect_serialization_key = False
        if tool_execution is not None:
            maximum_parallelism_key = (
                "maximumParallelism" if "maximumParallelism" in tool_execution else "maximum_parallelism"
            )
            configured_parallelism = tool_execution.get(
                maximum_parallelism_key,
                1,
            )
            if isinstance(configured_parallelism, int) and not isinstance(configured_parallelism, bool):
                maximum_parallelism = configured_parallelism
                if maximum_parallelism < 1:
                    diagnostics.append(
                        Diagnostic(
                            "GB1041",
                            "toolExecution maximumParallelism must be a positive integer",
                            f"$.spec.{tool_execution_key}.{maximum_parallelism_key}",
                        )
                    )
            elif maximum_parallelism_key in tool_execution:
                diagnostics.append(
                    Diagnostic(
                        "GB1041",
                        "toolExecution maximumParallelism must be a positive integer",
                        f"$.spec.{tool_execution_key}.{maximum_parallelism_key}",
                    )
                )
            parallel_tool_calls_key = (
                "parallelToolCalls" if "parallelToolCalls" in tool_execution else "parallel_tool_calls"
            )
            configured_parallel_tool_calls = tool_execution.get(
                parallel_tool_calls_key,
                False,
            )
            if isinstance(configured_parallel_tool_calls, bool):
                parallel_tool_calls = configured_parallel_tool_calls
            elif parallel_tool_calls_key in tool_execution:
                diagnostics.append(
                    Diagnostic(
                        "GB1041",
                        "toolExecution parallelToolCalls must be a boolean",
                        f"$.spec.{tool_execution_key}.{parallel_tool_calls_key}",
                    )
                )
            effect_serialization_key = (
                "effectSerialization" if "effectSerialization" in tool_execution else "effect_serialization"
            )
            effect_serialization = tool_execution.get(effect_serialization_key)
            if isinstance(effect_serialization, dict):
                key_template_key = "keyTemplate" if "keyTemplate" in effect_serialization else "key_template"
                key_template = effect_serialization.get(key_template_key)
                has_effect_serialization_key = isinstance(key_template, str) and bool(key_template.strip())
                if key_template_key in effect_serialization and not has_effect_serialization_key:
                    diagnostics.append(
                        Diagnostic(
                            "GB1041",
                            "toolExecution effectSerialization keyTemplate must be a non-empty string",
                            f"$.spec.{tool_execution_key}.{effect_serialization_key}.{key_template_key}",
                        )
                    )
            elif effect_serialization_key in tool_execution:
                diagnostics.append(
                    Diagnostic(
                        "GB1041",
                        "toolExecution effectSerialization must be a mapping",
                        f"$.spec.{tool_execution_key}.{effect_serialization_key}",
                    )
                )

        has_state_changing_tool = False
        for tool_key, tool in tools.items():
            if not isinstance(tool, dict):
                continue
            effects_value = tool.get("effects", [])
            if isinstance(effects_value, str):
                effects = [effects_value]
            elif isinstance(effects_value, list):
                effects = effects_value
            else:
                effects = []
                diagnostics.append(
                    Diagnostic(
                        "GB1040",
                        "tool effects must be a string or list of strings",
                        f"$.spec.bindings.tools.{tool_key}.effects",
                    )
                )
            valid_effects: set[str] = set()
            for effect_index, effect in enumerate(effects):
                if not isinstance(effect, str) or effect not in VALID_TOOL_EFFECTS:
                    effect_path = (
                        f"$.spec.bindings.tools.{tool_key}.effects"
                        if isinstance(effects_value, str)
                        else f"$.spec.bindings.tools.{tool_key}.effects[{effect_index}]"
                    )
                    diagnostics.append(
                        Diagnostic(
                            "GB1040",
                            f"invalid tool effect {effect}",
                            effect_path,
                        )
                    )
                    continue
                valid_effects.add(effect)
            if "none" in valid_effects and len(valid_effects) > 1:
                diagnostics.append(
                    Diagnostic(
                        "GB1040",
                        "tool effect none cannot be combined with other effects",
                        f"$.spec.bindings.tools.{tool_key}.effects",
                    )
                )
            state_changing_tool = bool(STATE_CHANGING_TOOL_EFFECTS & valid_effects)
            has_state_changing_tool = has_state_changing_tool or state_changing_tool

            approval = tool.get("approval")
            if isinstance(approval, dict):
                mode = approval.get("mode", "policy")
                valid_approval = isinstance(mode, str) and mode in VALID_TOOL_APPROVALS
                if not valid_approval:
                    diagnostics.append(
                        Diagnostic(
                            "GB1037",
                            f"invalid tool approval {mode}",
                            f"$.spec.bindings.tools.{tool_key}.approval.mode",
                        )
                    )
                bind_arguments_digest = approval.get(
                    "bindArgumentsDigest",
                    approval.get("bind_arguments_digest", False),
                )
                arguments_digest = (
                    approval.get("argumentsDigest")
                    or approval.get("arguments_digest")
                    or approval.get("argumentsDigestRef")
                    or approval.get("arguments_digest_ref")
                )
                binds_arguments_digest = bool(bind_arguments_digest) or (
                    isinstance(arguments_digest, str) and bool(arguments_digest.strip())
                )
                if valid_approval and mode in {"policy", "always"} and not binds_arguments_digest:
                    diagnostics.append(
                        Diagnostic(
                            "GB1023",
                            "explicit tool approval must be bound to immutable argument digest",
                            f"$.spec.bindings.tools.{tool_key}.approval",
                        )
                    )
            elif approval is not None:
                if not isinstance(approval, str) or approval not in VALID_TOOL_APPROVALS:
                    diagnostics.append(
                        Diagnostic(
                            "GB1037",
                            f"invalid tool approval {approval}",
                            f"$.spec.bindings.tools.{tool_key}.approval",
                        )
                    )
                elif approval == "always":
                    diagnostics.append(
                        Diagnostic(
                            "GB1023",
                            "explicit tool approval must be bound to immutable argument digest",
                            f"$.spec.bindings.tools.{tool_key}.approval",
                        )
                    )

            idempotency = tool.get("idempotency")
            if idempotency is not None and (
                not isinstance(idempotency, str) or idempotency not in VALID_TOOL_IDEMPOTENCIES
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1042",
                        f"invalid tool idempotency {idempotency}",
                        f"$.spec.bindings.tools.{tool_key}.idempotency",
                    )
                )

            cancellation = tool.get("cancellation")
            if cancellation is not None and (
                not isinstance(cancellation, str) or cancellation not in VALID_TOOL_CANCELLATIONS
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1038",
                        f"invalid tool cancellation {cancellation}",
                        f"$.spec.bindings.tools.{tool_key}.cancellation",
                    )
                )

            result_mode_key = "resultMode" if "resultMode" in tool else "result_mode"
            result_mode = tool.get(result_mode_key)
            if result_mode is not None and (
                not isinstance(result_mode, str) or result_mode not in VALID_TOOL_RESULT_MODES
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1043",
                        f"invalid tool result mode {result_mode}",
                        f"$.spec.bindings.tools.{tool_key}.{result_mode_key}",
                    )
                )

            retry_policy_ref = tool.get("retryPolicyRef") or tool.get("retry_policy_ref")
            has_retry_policy_ref = isinstance(retry_policy_ref, str) and bool(retry_policy_ref.strip())
            if state_changing_tool and has_retry_policy_ref and tool.get("idempotency") != "required":
                diagnostics.append(
                    Diagnostic(
                        "GB1045",
                        "retrying state-changing tool effects requires required idempotency",
                        f"$.spec.bindings.tools.{tool_key}.idempotency",
                    )
                )

            definition = tool.get("definition")
            if isinstance(definition, dict):
                for definition_field in ("name", "description"):
                    definition_value = definition.get(definition_field)
                    if not isinstance(definition_value, str) or not definition_value.strip():
                        diagnostics.append(
                            Diagnostic(
                                "GB1039",
                                f"tool definition {definition_field} must be a non-empty string",
                                f"$.spec.bindings.tools.{tool_key}.definition.{definition_field}",
                            )
                        )
                version = definition.get("version")
                if version is not None and (not isinstance(version, str) or not version.strip()):
                    diagnostics.append(
                        Diagnostic(
                            "GB1039",
                            "tool definition version must be a non-empty string",
                            f"$.spec.bindings.tools.{tool_key}.definition.version",
                        )
                    )
                tags = definition.get("tags")
                if tags is not None:
                    if isinstance(tags, list):
                        for tag_index, tag in enumerate(tags):
                            if not isinstance(tag, str) or not tag.strip():
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1039",
                                        "tool definition tags must be non-empty strings",
                                        f"$.spec.bindings.tools.{tool_key}.definition.tags[{tag_index}]",
                                    )
                                )
                    else:
                        diagnostics.append(
                            Diagnostic(
                                "GB1039",
                                "tool definition tags must be a list of non-empty strings",
                                f"$.spec.bindings.tools.{tool_key}.definition.tags",
                            )
                        )
                for forbidden_field in FORBIDDEN_TOOL_DEFINITION_FIELDS:
                    if forbidden_field in definition:
                        diagnostics.append(
                            Diagnostic(
                                "GB1039",
                                f"tool definition must not contain execution detail {forbidden_field}",
                                f"$.spec.bindings.tools.{tool_key}.definition.{forbidden_field}",
                            )
                        )
                input_schema = definition.get("inputSchema") or definition.get("input_schema")
                if not isinstance(input_schema, str) or not input_schema.strip():
                    diagnostics.append(
                        Diagnostic(
                            "GB1050",
                            "model-visible tool definitions require an input schema",
                            f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                        )
                    )
                else:
                    try:
                        SchemaId.parse(input_schema)
                    except SchemaIdError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"tool input schema id is invalid: {error}",
                                f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                            )
                        )
                output_schema = definition.get("outputSchema") or definition.get("output_schema")
                if isinstance(output_schema, str) and output_schema.strip():
                    try:
                        SchemaId.parse(output_schema)
                    except SchemaIdError as error:
                        diagnostics.append(
                            Diagnostic(
                                "GB0015",
                                f"tool output schema id is invalid: {error}",
                                f"$.spec.bindings.tools.{tool_key}.definition.outputSchema",
                            )
                        )
            else:
                diagnostics.append(
                    Diagnostic(
                        "GB1050",
                        "model-visible tool definitions require an input schema",
                        f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                    )
                )
            implementation = tool.get("implementation")
            if not isinstance(implementation, dict):
                diagnostics.append(
                    Diagnostic(
                        "GB1049",
                        "model-visible tools require an executable binding implementation",
                        f"$.spec.bindings.tools.{tool_key}.implementation",
                    )
                )
            else:
                implementation_kind = implementation.get("kind")
                missing_implementation_field: str | None = None
                if implementation_kind == "block":
                    value = implementation.get("block")
                    if not isinstance(value, str) or not value.strip():
                        missing_implementation_field = "block"
                elif implementation_kind == "graph":
                    value = implementation.get("graph")
                    if not isinstance(value, str) or not value.strip():
                        missing_implementation_field = "graph"
                elif implementation_kind == "remote":
                    connection = implementation.get("connection")
                    operation = implementation.get("operation")
                    if not isinstance(connection, str) or not connection.strip():
                        missing_implementation_field = "connection"
                    elif not isinstance(operation, str) or not operation.strip():
                        missing_implementation_field = "operation"
                elif implementation_kind == "mcp":
                    server = implementation.get("server")
                    remote_name = implementation.get("remoteName") or implementation.get("remote_name")
                    if not isinstance(server, str) or not server.strip():
                        missing_implementation_field = "server"
                    elif not isinstance(remote_name, str) or not remote_name.strip():
                        missing_implementation_field = "remoteName"
                elif implementation_kind == "openapi":
                    connection = implementation.get("connection")
                    operation_id = implementation.get("operationId") or implementation.get("operation_id")
                    if not isinstance(connection, str) or not connection.strip():
                        missing_implementation_field = "connection"
                    elif not isinstance(operation_id, str) or not operation_id.strip():
                        missing_implementation_field = "operationId"
                else:
                    diagnostics.append(
                        Diagnostic(
                            "GB1049",
                            "tool implementation kind must be one of block, graph, remote, mcp, or openapi",
                            f"$.spec.bindings.tools.{tool_key}.implementation.kind",
                        )
                    )

                if missing_implementation_field is not None:
                    diagnostics.append(
                        Diagnostic(
                            "GB1049",
                            f"{implementation_kind} tool implementation requires {missing_implementation_field}",
                            f"$.spec.bindings.tools.{tool_key}.implementation.{missing_implementation_field}",
                        )
                    )

        if (maximum_parallelism > 1 or parallel_tool_calls) and has_state_changing_tool and not has_effect_serialization_key:
            diagnostics.append(
                Diagnostic(
                    "GB1053",
                    "parallel state-changing tool execution requires an effect serialization key",
                    "$.spec.toolExecution.effectSerialization",
                )
            )

    normalized = _normalize_graph_unchecked(migrated)
    normalized_spec = normalized.get("spec", {})
    normalized_nodes = normalized_spec.get("nodes", {}) if isinstance(normalized_spec, dict) else {}
    edges = normalized_spec.get("edges", []) if isinstance(normalized_spec, dict) else []
    normalized_interface = normalized_spec.get("interface", {}) if isinstance(normalized_spec, dict) else {}
    extensions = normalized_spec.get("extensions", []) if isinstance(normalized_spec, dict) else []
    execution = normalized_spec.get("execution", {}) if isinstance(normalized_spec, dict) else {}
    voice = normalized_spec.get("voice", {}) if isinstance(normalized_spec, dict) else {}
    voice_pipeline = voice.get("pipeline", {}) if isinstance(voice, dict) else {}
    allows_duplex_voice_feedback = (
        isinstance(extensions, list)
        and "graphblocks.voice/v1alpha1" in extensions
        and isinstance(execution, dict)
        and execution.get("lifetime") == "session"
        and execution.get("interaction") == "duplex"
        and execution.get("durability") == "checkpointed"
        and isinstance(voice_pipeline, dict)
        and voice_pipeline.get("kind") == "realtime"
    )
    interface_inputs = normalized_interface.get("inputs") if isinstance(normalized_interface, dict) else None
    interface_outputs = normalized_interface.get("outputs") if isinstance(normalized_interface, dict) else None
    if not isinstance(interface_inputs, dict):
        interface_inputs = None
    if not isinstance(interface_outputs, dict):
        interface_outputs = None
    produced_nodes: set[str] = set()
    consumed_nodes: set[str] = set()
    invalid_input_port_nodes: set[str] = set()
    invalid_resource_binding_nodes: set[str] = set()
    dependency_graph: dict[str, set[str]] = {
        node_name: set() for node_name in normalized_nodes
    }
    edge_dependency_endpoints: set[tuple[str, str]] = set()
    guard_dependencies: set[tuple[str, str]] = set()

    if isinstance(edges, list):
        seen_edge_identities: set[tuple[str, str]] = set()
        for index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                diagnostics.append(Diagnostic("GB0010", "edge must be a mapping", f"$.spec.edges[{index}]"))
                continue
            source = edge.get("from")
            target = edge.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                diagnostics.append(Diagnostic("GB0011", "edge.from and edge.to must be strings", f"$.spec.edges[{index}]"))
                continue
            edge_identity = (source, target)
            if edge_identity in seen_edge_identities:
                diagnostics.append(
                    Diagnostic(
                        "GB1005",
                        f"duplicate edge identity {source!r} -> {target!r}",
                        f"$.spec.edges[{index}]",
                    )
                )
            seen_edge_identities.add(edge_identity)
            for key, endpoint in (("from", source), ("to", target)):
                owner, separator, endpoint_path = endpoint.partition(".")
                if (
                    not separator
                    or not owner
                    or not endpoint_path
                    or any(not part for part in endpoint_path.split("."))
                ):
                    diagnostics.append(
                        Diagnostic(
                            "GB1020",
                            f"edge {key} endpoint must include a port path",
                            f"$.spec.edges[{index}].{key}",
                        )
                    )
                    continue
                if key == "from" and owner == "$output":
                    diagnostics.append(
                        Diagnostic(
                            "GB1020",
                            "$output cannot be used as an edge source",
                            f"$.spec.edges[{index}].from",
                        )
                    )
                    continue
                if owner in {
                    "$context",
                    "$execution",
                    "$state",
                }:
                    endpoint_direction = "source" if key == "from" else "target"
                    diagnostics.append(
                        Diagnostic(
                            "GB1020",
                            f"{owner} is not supported as an edge {endpoint_direction} by the local runtime",
                            f"$.spec.edges[{index}].{key}",
                        )
                    )
                    continue
                if key == "to" and owner == "$input":
                    diagnostics.append(
                        Diagnostic(
                            "GB1020",
                            "$input cannot be used as an edge target",
                            f"$.spec.edges[{index}].to",
                        )
                    )
                    continue
                if key == "from" and owner == "$input":
                    port_name = endpoint.partition(".")[2].split(".", 1)[0]
                    if interface_inputs is not None and port_name not in interface_inputs:
                        diagnostics.append(
                            Diagnostic(
                                "GB1014",
                                f"graph interface has no input port {port_name!r}",
                                f"$.spec.edges[{index}].from",
                            )
                        )
                    continue
                if key == "to" and owner == "$output":
                    port_name = endpoint.partition(".")[2].split(".", 1)[0]
                    if interface_outputs is not None and port_name not in interface_outputs:
                        diagnostics.append(
                            Diagnostic(
                                "GB1013",
                                f"graph interface has no output port {port_name!r}",
                                f"$.spec.edges[{index}].to",
                            )
                        )
                    continue
                if owner in PSEUDO_NODES:
                    continue
                if owner not in normalized_nodes:
                    diagnostics.append(
                        Diagnostic(
                            "GB1002",
                            f"edge {key} endpoint references unknown node {owner!r}",
                            f"$.spec.edges[{index}].{key}",
                        )
                    )
                elif key == "from":
                    produced_nodes.add(owner)
                else:
                    consumed_nodes.add(owner)

            source_owner, source_separator, source_path = source.partition(".")
            target_owner, target_separator, target_path = target.partition(".")
            if (
                source_separator
                and target_separator
                and source_path
                and target_path
                and all(source_path.split("."))
                and all(target_path.split("."))
                and source_owner in dependency_graph
                and target_owner in dependency_graph
            ):
                dependency_graph[source_owner].add(target_owner)
                edge_dependency_endpoints.add((source, target))

            if block_catalog is not None:
                source_type = None
                target_type = None
                source_required = None
                target_required = None
                source_owner, _, source_path = source.partition(".")
                target_owner, _, target_path = target.partition(".")
                if source_owner == "$input":
                    port_name, separator, _nested_path = source_path.partition(".")
                    if interface_inputs is not None and port_name in interface_inputs and not separator:
                        schema_id = interface_inputs[port_name]
                        if isinstance(schema_id, str):
                            source_type = schema_id
                elif source_owner not in PSEUDO_NODES and source_owner in normalized_nodes and source_path:
                    source_node = normalized_nodes[source_owner]
                    if isinstance(source_node, dict):
                        descriptor = block_catalog.get(str(source_node.get("block")))
                        if descriptor is not None:
                            port_name, separator, _nested_path = source_path.partition(".")
                            output_ports = {port.name: port for port in descriptor.outputs}
                            if port_name not in output_ports:
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1014",
                                        f"block {descriptor.block_id} has no output port {port_name!r}",
                                        f"$.spec.edges[{index}].from",
                                    )
                                )
                            else:
                                source_port = output_ports[port_name]
                                if not separator:
                                    source_type = source_port.type_ref
                                    source_config = source_node.get("config", {})
                                    if not isinstance(source_config, dict):
                                        source_config = {}
                                    source_required = source_port.required_for(
                                        source_config,
                                        phase="initial",
                                    )
                if target_owner == "$output":
                    port_name, separator, _nested_path = target_path.partition(".")
                    if interface_outputs is not None and port_name in interface_outputs and not separator:
                        schema_id = interface_outputs[port_name]
                        if isinstance(schema_id, str):
                            target_type = schema_id
                            target_required = True
                elif target_owner not in PSEUDO_NODES and target_owner in normalized_nodes and target_path:
                    target_node = normalized_nodes[target_owner]
                    if isinstance(target_node, dict):
                        descriptor = block_catalog.get(str(target_node.get("block")))
                        if descriptor is not None:
                            port_name, separator, _nested_path = target_path.partition(".")
                            input_ports = {port.name: port for port in descriptor.inputs}
                            if port_name not in input_ports:
                                invalid_input_port_nodes.add(target_owner)
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1013",
                                        f"block {descriptor.block_id} has no input port {port_name!r}",
                                        f"$.spec.edges[{index}].to",
                                    )
                                )
                            else:
                                target_port = input_ports[port_name]
                                if not separator:
                                    target_type = target_port.type_ref
                                    target_required = target_port.required
                if source_type and target_type and source_type != "Any" and target_type != "Any" and source_type != target_type:
                    diagnostics.append(
                        Diagnostic(
                            "GB1018",
                            f"port type mismatch: {source_type} cannot feed {target_type}",
                            f"$.spec.edges[{index}]",
                        )
                    )
                if source_required is False and target_required is True:
                    diagnostics.append(
                        Diagnostic(
                            "GB1015",
                            "optional branch output cannot feed required input",
                            f"$.spec.edges[{index}]",
                        )
                    )

    if block_catalog is not None:
        inbound_by_node: dict[str, set[str]] = {name: set() for name in normalized_nodes}
        if isinstance(edges, list):
            for edge in edges:
                if not isinstance(edge, dict) or not isinstance(edge.get("to"), str):
                    continue
                target_owner, _, target_path = edge["to"].partition(".")
                if target_owner in inbound_by_node and target_path:
                    inbound_by_node[target_owner].add(target_path.split(".", 1)[0])
        for node_name, node in normalized_nodes.items():
            if not isinstance(node, dict):
                continue
            descriptor = block_catalog.get(str(node.get("block")))
            if descriptor is None:
                if not block_catalog.allow_unknown_blocks:
                    diagnostics.append(
                        Diagnostic(
                            "GB1022",
                            f"block {node.get('block')!r} is not declared in the block catalog",
                            f"$.spec.nodes.{node_name}.block",
                        )
                    )
                continue
            config = node.get("config", {})
            config_path = f"$.spec.nodes.{node_name}.config"
            try:
                config_errors = list(
                    Draft202012Validator(descriptor.config_schema).iter_errors(config)
                )
            except (RecursionError, Unresolvable):
                diagnostics.append(
                    Diagnostic(
                        "GB2019",
                        (
                            f"node config cannot be validated against "
                            f"{descriptor.block_id} because its configSchema "
                            "contains an unresolved or nonterminating local reference"
                        ),
                        config_path,
                    )
                )
                config_errors = []
            for error in sorted(
                config_errors,
                key=lambda item: (
                    _config_error_path(config_path, item),
                    canonical_dumps(list(item.absolute_schema_path)),
                    str(item.validator or "schema"),
                    _config_error_message(item),
                ),
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB2019",
                        (
                            f"node config does not satisfy {descriptor.block_id} "
                            f"configSchema: {_config_error_message(error)}"
                        ),
                        _config_error_path(config_path, error),
                    )
                )
            if descriptor.resource_slots:
                bindings = node.get("bindings", {})
                if bindings is None:
                    bindings = {}
                if not isinstance(bindings, dict):
                    diagnostics.append(
                        Diagnostic("GB1017", "node bindings must be a mapping", f"$.spec.nodes.{node_name}.bindings")
                    )
                    bindings = {}
                slot_names = {slot.name for slot in descriptor.resource_slots}
                for binding_name in bindings:
                    if binding_name not in slot_names:
                        invalid_resource_binding_nodes.add(node_name)
                        diagnostics.append(
                            Diagnostic(
                                "GB1017",
                                f"block {descriptor.block_id} has no resource slot {binding_name!r}",
                                f"$.spec.nodes.{node_name}.bindings.{binding_name}",
                            )
                        )
                for slot in descriptor.resource_slots:
                    if node_name not in invalid_resource_binding_nodes and not slot.optional and slot.name not in bindings:
                        diagnostics.append(
                            Diagnostic(
                                "GB1016",
                                f"required resource slot {slot.name!r} is not bound for node {node_name!r}",
                                f"$.spec.nodes.{node_name}.bindings",
                            )
                        )
            if node_name in invalid_input_port_nodes:
                continue
            for port in descriptor.inputs:
                if port.required and port.name not in inbound_by_node[node_name]:
                    diagnostics.append(
                        Diagnostic(
                            "GB1003",
                            f"required input {port.name!r} is never produced for node {node_name!r}",
                            f"$.spec.nodes.{node_name}",
                        )
                    )

    for node_name, node in normalized_nodes.items():
        if isinstance(node, dict) and "when" in node:
            when = node["when"]
            if not isinstance(when, str):
                diagnostics.append(
                    Diagnostic(
                        "GB1020",
                        "node when reference must be a string",
                        f"$.spec.nodes.{node_name}.when",
                    )
                )
                continue
            owner, separator, when_path = when.partition(".")
            if (
                not separator
                or not owner
                or not when_path
                or any(not part for part in when_path.split("."))
            ):
                diagnostics.append(
                    Diagnostic(
                        "GB1020",
                        "node when reference must include a port path",
                        f"$.spec.nodes.{node_name}.when",
                    )
                )
            elif owner == "$output":
                diagnostics.append(
                    Diagnostic(
                        "GB1020",
                        "$output cannot be used as a when source",
                        f"$.spec.nodes.{node_name}.when",
                    )
                )
            elif owner == "$input":
                port_name = when_path.split(".", 1)[0]
                if interface_inputs is not None and port_name not in interface_inputs:
                    diagnostics.append(
                        Diagnostic(
                            "GB1014",
                            f"graph interface has no input port {port_name!r}",
                            f"$.spec.nodes.{node_name}.when",
                        )
                    )
            elif owner in {"$context", "$execution", "$state"}:
                diagnostics.append(
                    Diagnostic(
                        "GB1020",
                        f"{owner} is not supported as a when source by the local runtime",
                        f"$.spec.nodes.{node_name}.when",
                    )
                )
            elif owner not in PSEUDO_NODES and owner not in normalized_nodes:
                diagnostics.append(
                    Diagnostic("GB1002", f"when references unknown node {owner!r}", f"$.spec.nodes.{node_name}.when")
                )
            elif owner not in PSEUDO_NODES:
                source_node = normalized_nodes[owner]
                descriptor = None
                if block_catalog is not None and isinstance(source_node, dict):
                    descriptor = block_catalog.get(str(source_node.get("block")))
                port_name = when_path.split(".", 1)[0]
                if descriptor is not None and port_name not in {port.name for port in descriptor.outputs}:
                    diagnostics.append(
                        Diagnostic(
                            "GB1014",
                            f"block {descriptor.block_id} has no output port {port_name!r}",
                            f"$.spec.nodes.{node_name}.when",
                        )
                    )
                    continue
                produced_nodes.add(owner)
                consumed_nodes.add(node_name)
                dependency_graph[owner].add(node_name)
                guard_dependencies.add((owner, node_name))

    if allows_duplex_voice_feedback:
        reverse_dependency_graph: dict[str, set[str]] = {
            node_name: set() for node_name in dependency_graph
        }
        for source_owner, targets in dependency_graph.items():
            for target_owner in targets:
                reverse_dependency_graph[target_owner].add(source_owner)

        allowed_feedback_dependencies: set[tuple[str, str]] = set()
        for source_endpoint, target_endpoint in sorted(edge_dependency_endpoints):
            source_owner, _, source_path = source_endpoint.partition(".")
            target_owner, _, target_path = target_endpoint.partition(".")
            if source_path != "results" or target_path != "toolResults":
                continue
            source_node = normalized_nodes.get(source_owner)
            target_node = normalized_nodes.get(target_owner)
            if (
                not isinstance(source_node, dict)
                or source_node.get("block") != "tools.dispatch@1"
                or not isinstance(target_node, dict)
                or target_node.get("block") != "realtime.session@1"
            ):
                continue
            reverse_endpoint = (
                f"{target_owner}.toolCalls",
                f"{source_owner}.calls",
            )
            if reverse_endpoint not in edge_dependency_endpoints:
                continue

            reachable_from_session: set[str] = set()
            stack = [target_owner]
            while stack:
                current = stack.pop()
                if current in reachable_from_session:
                    continue
                reachable_from_session.add(current)
                stack.extend(dependency_graph[current] - reachable_from_session)

            can_reach_session: set[str] = set()
            stack = [target_owner]
            while stack:
                current = stack.pop()
                if current in can_reach_session:
                    continue
                can_reach_session.add(current)
                stack.extend(reverse_dependency_graph[current] - can_reach_session)

            component = reachable_from_session & can_reach_session
            if component != {source_owner, target_owner}:
                continue
            internal_endpoints = {
                (edge_source, edge_target)
                for edge_source, edge_target in edge_dependency_endpoints
                if edge_source.partition(".")[0] in component
                and edge_target.partition(".")[0] in component
            }
            if internal_endpoints != {
                (source_endpoint, target_endpoint),
                reverse_endpoint,
            }:
                continue
            if any(
                guard_source in component and guard_target in component
                for guard_source, guard_target in guard_dependencies
            ):
                continue
            allowed_feedback_dependencies.add((source_owner, target_owner))

        for source_owner, target_owner in allowed_feedback_dependencies:
            dependency_graph[source_owner].discard(target_owner)

    dependency_states: dict[str, Literal["visiting", "done"]] = {}
    dependency_cycle: list[str] | None = None
    for root in sorted(dependency_graph):
        if root in dependency_states:
            continue
        dependency_states[root] = "visiting"
        path = [root]
        path_positions = {root: 0}
        stack: list[tuple[str, Iterator[str]]] = [
            (root, iter(sorted(dependency_graph[root])))
        ]
        while stack:
            current, neighbors = stack[-1]
            try:
                neighbor = next(neighbors)
            except StopIteration:
                dependency_states[current] = "done"
                stack.pop()
                path_positions.pop(current, None)
                path.pop()
                continue
            state = dependency_states.get(neighbor)
            if state is None:
                dependency_states[neighbor] = "visiting"
                path_positions[neighbor] = len(path)
                path.append(neighbor)
                stack.append((neighbor, iter(sorted(dependency_graph[neighbor]))))
            elif state == "visiting":
                cycle_start = path_positions[neighbor]
                dependency_cycle = [*path[cycle_start:], neighbor]
                break
        if dependency_cycle is not None:
            break
    if dependency_cycle is not None:
        diagnostics.append(
            Diagnostic(
                "GB1021",
                f"graph dependency cycle detected: {' -> '.join(dependency_cycle)}",
                "$.spec",
            )
        )

    interface = normalized_spec.get("interface", {}) if isinstance(normalized_spec, dict) else {}
    outputs = interface.get("outputs", {}) if isinstance(interface, dict) else {}
    has_declared_output = isinstance(outputs, dict) and bool(outputs)
    output_edges = [edge for edge in edges if isinstance(edge, dict) and isinstance(edge.get("to"), str) and edge["to"].startswith("$output.")]
    if has_declared_output and not output_edges:
        diagnostics.append(
            Diagnostic(
                "GB1004",
                "graph declares outputs but no edge writes to $output",
                "$.spec.interface.outputs",
                "warning",
            )
        )

    if output_edges:
        reachable: set[str] = set()
        stack = [edge["from"].split(".", 1)[0] for edge in output_edges if isinstance(edge.get("from"), str)]
        reverse_edges: dict[str, list[str]] = {}
        for edge in edges:
            if isinstance(edge, dict) and isinstance(edge.get("from"), str) and isinstance(edge.get("to"), str):
                source_owner = edge["from"].split(".", 1)[0]
                target_owner = edge["to"].split(".", 1)[0]
                reverse_edges.setdefault(target_owner, []).append(source_owner)
        while stack:
            owner = stack.pop()
            if owner in reachable or owner in PSEUDO_NODES:
                continue
            reachable.add(owner)
            stack.extend(reverse_edges.get(owner, []))
        for node_name in sorted(normalized_nodes):
            if node_name not in reachable and node_name not in produced_nodes and node_name not in consumed_nodes:
                diagnostics.append(Diagnostic("GB1001", f"node {node_name!r} is not connected", f"$.spec.nodes.{node_name}", "warning"))

    diagnostic_paths = {diagnostic.path for diagnostic in diagnostics}
    schema_diagnostics = [
        Diagnostic(violation.code, violation.message, violation.path)
        for violation in schema_violations
        if violation.keyword == "additionalProperties"
        or violation.path not in diagnostic_paths
    ]
    return Plan(
        normalized,
        canonical_hash(normalized),
        DiagnosticSet(tuple([*schema_diagnostics, *diagnostics])),
    )


def compile_graph_native(
    document: dict[str, object],
    block_catalog: object | None = None,
    *,
    allow_unknown_blocks: bool = False,
) -> dict[str, object]:
    from graphblocks_runtime import compile_graph as native_compile_graph

    return native_compile_graph(
        document,
        block_catalog=block_catalog,
        allow_unknown_blocks=allow_unknown_blocks,
    )
