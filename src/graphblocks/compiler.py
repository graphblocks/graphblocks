from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from .canonical import PSEUDO_NODES, canonical_hash, normalize_graph
from .diagnostics import Diagnostic, DiagnosticSet
from .migration import GRAPH_API_VERSION, LEGACY_GRAPH_API_VERSIONS, migrate_document
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
from .plugins import BlockCatalog
from .policy import VALID_ENFORCEMENT_POINTS as VALID_POLICY_ENFORCEMENT_POINTS
from .schema import SchemaId, SchemaIdError
from .tools import (
    VALID_TOOL_APPROVALS,
    VALID_TOOL_CANCELLATIONS,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_IDEMPOTENCIES,
    VALID_TOOL_RESULT_MODES,
)

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
ORDER_CAPABLE_CALLBACK_TARGETS = frozenset({"webhook", "websocket", "sse"})
UNSAFE_CALLBACK_HOSTS = frozenset({"localhost", "metadata.google.internal"})
DEFAULT_CALLBACK_MAX_PAYLOAD_BYTES = 262_144


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


def _has_async_timeout(config: dict[str, Any]) -> bool:
    timeout = (
        config.get("timeout")
        or config.get("timeoutMs")
        or config.get("timeout_ms")
        or config.get("deadline")
    )
    if _has_non_empty_string(timeout) or _is_positive_integer(timeout):
        return True
    infinite_wait = config.get("infiniteWait", config.get("infinite_wait", False))
    explicit_infinite_wait_policy = config.get("infiniteWaitPolicy") or config.get("infinite_wait_policy")
    return infinite_wait is True or _has_non_empty_string(explicit_infinite_wait_policy)


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
) -> None:
    callback = config.get("callback")
    callback_config = callback if isinstance(callback, dict) else {}
    if not _has_async_timeout(config):
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
    if _callback_schema_required(config) and not _has_async_callback_schema(config):
        diagnostics.append(
            Diagnostic(
                "GB6007",
                "async operation callbacks require an expected callback schema",
                f"{path}.callback",
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
    if not isinstance(url, str) or not url.strip():
        return True
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    hostname = parsed.hostname
    if not hostname:
        return True
    normalized_host = hostname.rstrip(".").lower()
    if normalized_host in UNSAFE_CALLBACK_HOSTS or normalized_host.endswith(".localhost"):
        return True
    try:
        address = ip_address(normalized_host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _has_callback_dead_letter_behavior(config: dict[str, Any], delivery: dict[str, Any]) -> bool:
    failure_policy = config.get("failurePolicy") or config.get("failure_policy")
    if failure_policy == "retry_then_dead_letter":
        return True
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
    if not isinstance(delivery, dict):
        return
    delivery_kind = delivery.get("kind")
    if delivery_kind == "webhook":
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
    if mandatory and not failure_policy:
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
        failure_policy in MANDATORY_CALLBACK_FAILURE_POLICIES
        and not _has_callback_dead_letter_behavior(config, delivery)
    ):
        diagnostics.append(
            Diagnostic(
                "GB6014",
                "mandatory callback failure policy requires dead-letter or fallback behavior",
                f"{path}.deadLetterPolicy",
            )
        )


def compile_graph(document: dict[str, Any], block_catalog: BlockCatalog | None = None) -> Plan:
    diagnostics: list[Diagnostic] = []
    migrated = migrate_document(document)
    if migrated.get("kind") != "Graph":
        diagnostics.append(Diagnostic("GB0001", "document kind must be Graph", "$.kind"))
        normalized = normalize_graph(migrated)
        return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))

    api_version = document.get("apiVersion")
    if api_version not in {GRAPH_API_VERSION, *LEGACY_GRAPH_API_VERSIONS}:
        diagnostics.append(
            Diagnostic("GB0002", f"unsupported Graph apiVersion {api_version!r}", "$.apiVersion")
        )

    metadata = migrated.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"]:
        diagnostics.append(Diagnostic("GB0003", "metadata.name is required", "$.metadata.name"))

    spec = migrated.get("spec")
    if not isinstance(spec, dict):
        diagnostics.append(Diagnostic("GB0004", "spec must be a mapping", "$.spec"))
        normalized = normalize_graph(migrated)
        return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))

    nodes = spec.get("nodes", {})
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, dict):
        diagnostics.append(Diagnostic("GB0005", "spec.nodes must be a mapping", "$.spec.nodes"))
        nodes = {}

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
                                "InvalidSchemaId",
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
                                "InvalidSchemaId",
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
        block = node.get("block")
        if not isinstance(block, str) or "@" not in block or block.endswith("@"):
            diagnostics.append(Diagnostic("GB0009", "node.block must use '<type>@<major>'", f"$.spec.nodes.{node_name}.block"))
        if isinstance(block, str) and block.split("@", 1)[0] in {
            "async.start_operation",
            "async.await_callback",
            "async.poll_operation",
        }:
            config = node.get("config", {})
            if config is None:
                config = {}
            if isinstance(config, dict):
                _diagnose_async_operation_config(
                    diagnostics,
                    config,
                    f"$.spec.nodes.{node_name}.config",
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        "InvalidAsyncOperation",
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
        if effect_retry_requires_key and max_attempts > 1 and not idempotency_key:
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
                            "InvalidAsyncOperation",
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
                            "InvalidAsyncOperation",
                            "async operation config must be a mapping",
                            operation_path,
                        )
                    )
                    continue
                _diagnose_async_operation_config(diagnostics, operation_config, operation_path)
        else:
            diagnostics.append(
                Diagnostic(
                    "InvalidAsyncOperation",
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
                            "InvalidCallbackSubscription",
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
                            "InvalidCallbackSubscription",
                            "callback subscription config must be a mapping",
                            subscription_path,
                        )
                    )
                    continue
                _diagnose_callback_subscription_config(diagnostics, subscription_config, subscription_path)
        else:
            diagnostics.append(
                Diagnostic(
                    "InvalidCallbackSubscription",
                    "callbackSubscriptions must be a mapping or list",
                    f"$.spec.{callback_subscriptions_key}",
                )
            )

    output_policy_key = "outputPolicy" if "outputPolicy" in spec else "output_policy"
    output_policy = spec.get(output_policy_key)
    if output_policy is not None and not isinstance(output_policy, dict):
        diagnostics.append(
            Diagnostic(
                "InvalidOutputPolicy",
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
                    "InvalidOutputPolicy",
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
                        "InvalidOutputDeliveryMode",
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
                        "InvalidViolationAction",
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
                        "InvalidDraftDisposition",
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
                                    "InvalidFlushBoundary",
                                    f"invalid flush boundary {boundary}",
                                    f"{flush_boundaries_path}[{boundary_index}]",
                                )
                            )
                else:
                    diagnostics.append(
                        Diagnostic(
                            "InvalidFlushBoundary",
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
                            "UnboundedPolicyHoldback",
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
                            "ImmediateDraftWithoutRetractionSupport",
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
                    "InvalidOutputPolicy",
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
                            "InvalidOutputEnforcementPoint",
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
                        "OutputPolicyBypass",
                        "output policy enforcement must include the before_client_delivery gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            elif on_generation_chunk_index is None:
                diagnostics.append(
                    Diagnostic(
                        "OutputPolicyBypass",
                        "output policy enforcement must include the on_generation_chunk gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            elif before_output_commit_index is None:
                diagnostics.append(
                    Diagnostic(
                        "OutputPolicyBypass",
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
                        "PolicyGateAfterDelivery",
                        "on_generation_chunk policy evaluation must precede before_client_delivery",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
        elif enforcement_points is not None:
            diagnostics.append(
                Diagnostic(
                    "InvalidOutputEnforcementPoint",
                    "output policy enforcementPoints must be a list of strings",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )
            diagnostics.append(
                Diagnostic(
                    "OutputPolicyBypass",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )
        else:
            diagnostics.append(
                Diagnostic(
                    "OutputPolicyBypass",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )

        on_violation = output_policy.get("onViolation") or output_policy.get("on_violation")
        if on_violation is not None and not isinstance(on_violation, dict):
            diagnostics.append(
                Diagnostic(
                    "InvalidOutputPolicy",
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
                        "InvalidOutputDisposition",
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
                        "InvalidProviderCancellation",
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
                        "InvalidPendingToolCallsDisposition",
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
                        "InvalidDraftDisposition",
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
                        "InvalidOutputDurableResult",
                        f"invalid output durable result {durable_result_disposition}",
                        "$.spec.outputPolicy.onViolation.durableResult.disposition",
                    )
                )

            if disposition in {"abort_response", "abort_turn"}:
                if valid_pending_tool_calls_disposition and pending_tool_calls_disposition == "keep":
                    diagnostics.append(
                        Diagnostic(
                            "PendingToolCallAfterAbort",
                            "policy-aborted responses must deny or cancel pending tool calls",
                            "$.spec.outputPolicy.onViolation.pendingToolCalls.disposition",
                        )
                    )

                if valid_durable_result_disposition and durable_result_disposition != "none":
                    diagnostics.append(
                        Diagnostic(
                            "CommitAfterPolicyStop",
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
                    "InvalidToolExecution",
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
                            "InvalidToolExecution",
                            "toolExecution maximumParallelism must be a positive integer",
                            f"$.spec.{tool_execution_key}.{maximum_parallelism_key}",
                        )
                    )
            elif maximum_parallelism_key in tool_execution:
                diagnostics.append(
                    Diagnostic(
                        "InvalidToolExecution",
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
                        "InvalidToolExecution",
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
                            "InvalidToolExecution",
                            "toolExecution effectSerialization keyTemplate must be a non-empty string",
                            f"$.spec.{tool_execution_key}.{effect_serialization_key}.{key_template_key}",
                        )
                    )
            elif effect_serialization_key in tool_execution:
                diagnostics.append(
                    Diagnostic(
                        "InvalidToolExecution",
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
                        "InvalidToolEffect",
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
                            "InvalidToolEffect",
                            f"invalid tool effect {effect}",
                            effect_path,
                        )
                    )
                    continue
                valid_effects.add(effect)
            if "none" in valid_effects and len(valid_effects) > 1:
                diagnostics.append(
                    Diagnostic(
                        "InvalidToolEffect",
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
                            "InvalidToolApproval",
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
                            "ApprovalWithoutArgumentDigest",
                            "explicit tool approval must be bound to immutable argument digest",
                            f"$.spec.bindings.tools.{tool_key}.approval",
                        )
                    )
            elif approval is not None:
                if not isinstance(approval, str) or approval not in VALID_TOOL_APPROVALS:
                    diagnostics.append(
                        Diagnostic(
                            "InvalidToolApproval",
                            f"invalid tool approval {approval}",
                            f"$.spec.bindings.tools.{tool_key}.approval",
                        )
                    )
                elif approval == "always":
                    diagnostics.append(
                        Diagnostic(
                            "ApprovalWithoutArgumentDigest",
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
                        "InvalidToolIdempotency",
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
                        "InvalidToolCancellation",
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
                        "InvalidToolResultMode",
                        f"invalid tool result mode {result_mode}",
                        f"$.spec.bindings.tools.{tool_key}.{result_mode_key}",
                    )
                )

            retry_policy_ref = tool.get("retryPolicyRef") or tool.get("retry_policy_ref")
            has_retry_policy_ref = isinstance(retry_policy_ref, str) and bool(retry_policy_ref.strip())
            if state_changing_tool and has_retry_policy_ref and tool.get("idempotency") != "required":
                diagnostics.append(
                    Diagnostic(
                        "NonIdempotentRetry",
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
                                "InvalidToolDefinition",
                                f"tool definition {definition_field} must be a non-empty string",
                                f"$.spec.bindings.tools.{tool_key}.definition.{definition_field}",
                            )
                        )
                version = definition.get("version")
                if version is not None and (not isinstance(version, str) or not version.strip()):
                    diagnostics.append(
                        Diagnostic(
                            "InvalidToolDefinition",
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
                                        "InvalidToolDefinition",
                                        "tool definition tags must be non-empty strings",
                                        f"$.spec.bindings.tools.{tool_key}.definition.tags[{tag_index}]",
                                    )
                                )
                    else:
                        diagnostics.append(
                            Diagnostic(
                                "InvalidToolDefinition",
                                "tool definition tags must be a list of non-empty strings",
                                f"$.spec.bindings.tools.{tool_key}.definition.tags",
                            )
                        )
                for forbidden_field in FORBIDDEN_TOOL_DEFINITION_FIELDS:
                    if forbidden_field in definition:
                        diagnostics.append(
                            Diagnostic(
                                "InvalidToolDefinition",
                                f"tool definition must not contain execution detail {forbidden_field}",
                                f"$.spec.bindings.tools.{tool_key}.definition.{forbidden_field}",
                            )
                        )
                input_schema = definition.get("inputSchema") or definition.get("input_schema")
                if not isinstance(input_schema, str) or not input_schema.strip():
                    diagnostics.append(
                        Diagnostic(
                            "ToolSchemaMissing",
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
                                "InvalidSchemaId",
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
                                "InvalidSchemaId",
                                f"tool output schema id is invalid: {error}",
                                f"$.spec.bindings.tools.{tool_key}.definition.outputSchema",
                            )
                        )
            else:
                diagnostics.append(
                    Diagnostic(
                        "ToolSchemaMissing",
                        "model-visible tool definitions require an input schema",
                        f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                    )
                )
            implementation = tool.get("implementation")
            if not isinstance(implementation, dict):
                diagnostics.append(
                    Diagnostic(
                        "ToolBindingMissing",
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
                            "ToolBindingMissing",
                            "tool implementation kind must be one of block, graph, remote, mcp, or openapi",
                            f"$.spec.bindings.tools.{tool_key}.implementation.kind",
                        )
                    )

                if missing_implementation_field is not None:
                    diagnostics.append(
                        Diagnostic(
                            "ToolBindingMissing",
                            f"{implementation_kind} tool implementation requires {missing_implementation_field}",
                            f"$.spec.bindings.tools.{tool_key}.implementation.{missing_implementation_field}",
                        )
                    )

        if (maximum_parallelism > 1 or parallel_tool_calls) and has_state_changing_tool and not has_effect_serialization_key:
            diagnostics.append(
                Diagnostic(
                    "UnsafeParallelEffects",
                    "parallel state-changing tool execution requires an effect serialization key",
                    "$.spec.toolExecution.effectSerialization",
                )
            )

    normalized = normalize_graph(migrated)
    normalized_spec = normalized.get("spec", {})
    normalized_nodes = normalized_spec.get("nodes", {}) if isinstance(normalized_spec, dict) else {}
    edges = normalized_spec.get("edges", []) if isinstance(normalized_spec, dict) else []
    produced_nodes: set[str] = set()
    consumed_nodes: set[str] = set()
    invalid_input_port_nodes: set[str] = set()
    invalid_resource_binding_nodes: set[str] = set()

    if isinstance(edges, list):
        for index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                diagnostics.append(Diagnostic("GB0010", "edge must be a mapping", f"$.spec.edges[{index}]"))
                continue
            source = edge.get("from")
            target = edge.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                diagnostics.append(Diagnostic("GB0011", "edge.from and edge.to must be strings", f"$.spec.edges[{index}]"))
                continue
            for key, endpoint in (("from", source), ("to", target)):
                owner = endpoint.split(".", 1)[0]
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

            if block_catalog is not None:
                source_type = None
                target_type = None
                source_required = None
                target_required = None
                source_owner, _, source_path = source.partition(".")
                target_owner, _, target_path = target.partition(".")
                if source_owner not in PSEUDO_NODES and source_owner in normalized_nodes and source_path:
                    source_node = normalized_nodes[source_owner]
                    if isinstance(source_node, dict):
                        descriptor = block_catalog.get(str(source_node.get("block")))
                        if descriptor is not None and descriptor.outputs:
                            port_name = source_path.split(".", 1)[0]
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
                                source_type = source_port.type_ref
                                source_required = source_port.required
                if target_owner not in PSEUDO_NODES and target_owner in normalized_nodes and target_path:
                    target_node = normalized_nodes[target_owner]
                    if isinstance(target_node, dict):
                        descriptor = block_catalog.get(str(target_node.get("block")))
                        if descriptor is not None and descriptor.inputs:
                            port_name = target_path.split(".", 1)[0]
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
                continue
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
        if isinstance(node, dict) and isinstance(node.get("when"), str):
            owner = node["when"].split(".", 1)[0]
            if owner not in PSEUDO_NODES and owner not in normalized_nodes:
                diagnostics.append(
                    Diagnostic("GB1002", f"when references unknown node {owner!r}", f"$.spec.nodes.{node_name}.when")
                )
            elif owner not in PSEUDO_NODES:
                produced_nodes.add(owner)
                consumed_nodes.add(node_name)

    interface = normalized_spec.get("interface", {}) if isinstance(normalized_spec, dict) else {}
    outputs = interface.get("outputs", {}) if isinstance(interface, dict) else {}
    has_declared_output = isinstance(outputs, dict) and bool(outputs)
    output_edges = [edge for edge in edges if isinstance(edge, dict) and isinstance(edge.get("to"), str) and edge["to"].startswith("$output.")]
    if has_declared_output and not output_edges:
        diagnostics.append(
            Diagnostic(
                "GB1003",
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

    return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))


def compile_graph_native(document: dict[str, object], block_catalog: object | None = None) -> dict[str, object]:
    from graphblocks_runtime import compile_graph as native_compile_graph

    return native_compile_graph(document, block_catalog=block_catalog)
