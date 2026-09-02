"""Server protocol vocabulary and validation helpers.

Extracted verbatim from ``graphblocks.server`` (which re-imports every name,
so the ``graphblocks.server`` facade is unchanged): the transport/health type
aliases, the ``VALID_*``/``MAX_*``/``DEFAULT_*`` protocol constants, the server
operation registry, and the request-validation / JSON freeze-thaw / event
visibility helpers that everything above the transport layer shares.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import math
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
import unicodedata
from urllib.parse import quote, unquote

from ._canonical_reference import canonical_dumps, canonical_hash
from ._json import reject_duplicate_json_keys as _reject_duplicate_json_keys
from .clocks import system_audit_wall_timestamp
from .policy import PrincipalRef
from .url_validation import validate_webhook_url

if TYPE_CHECKING:
    from ._server_transport import ServerRequest

ServerTransport = Literal["http", "sse", "websocket"]
ServerHealthStatus = Literal["healthy", "degraded", "unhealthy"]
VALID_SERVER_TRANSPORTS = frozenset({"http", "sse", "websocket"})
VALID_SERVER_HEALTH_STATUSES = frozenset({"healthy", "degraded", "unhealthy"})
VALID_CALLBACK_SUBSCRIPTION_SCOPES = frozenset({
    "run",
    "conversation",
    "project",
    "tenant",
    "deployment",
})
VALID_CALLBACK_SUBSCRIPTION_STATUSES = frozenset({"active", "paused", "expired", "revoked"})
VALID_CALLBACK_FAILURE_POLICIES = frozenset({
    "best_effort",
    "retry_then_dead_letter",
    "pause_run_on_failure",
    "fail_run_on_failure",
})
MANDATORY_CALLBACK_FAILURE_POLICIES = frozenset({"pause_run_on_failure", "fail_run_on_failure"})
VALID_CALLBACK_DELIVERY_KINDS = frozenset({
    "webhook",
    "websocket",
    "sse",
    "push_notification",
    "email",
    "local_callback",
})
ORDER_CAPABLE_CALLBACK_TARGETS = frozenset({"webhook", "websocket", "sse"})
VALID_EVENT_VISIBILITIES = frozenset({"client", "operator", "internal", "audit_only"})
VALID_ATTACH_CAPABILITIES = frozenset({
    "assistant_drafts",
    "retractions",
    "artifact_preview",
    "patch_preview",
    "approval",
    "review",
    "budget_extension",
    "background_notifications",
    "interrupt_resume",
})
VALID_WEBHOOK_SIGNING_ALGORITHMS = frozenset({"hmac-sha256", "ed25519"})
VALID_SERVER_CALLBACK_DELIVERY_STATUSES = frozenset({
    "pending",
    "delivered",
    "acknowledged",
    "failed",
    "dead_lettered",
    "cancelled",
})
SERVER_EVENT_SEVERITY_RANKS = {
    "debug": 10,
    "info": 20,
    "notice": 30,
    "warning": 40,
    "warn": 40,
    "error": 50,
    "critical": 60,
    "fatal": 60,
}
_SERVER_BEARER_CHALLENGE = 'Bearer realm="graphblocks"'
SERVER_TERMINAL_EVENT_KINDS = frozenset({
    "RunSucceeded",
    "RunFailed",
    "RunCancelled",
    "RunPolicyStopped",
    "RunCompleted",
    "RunExpired",
})
SERVER_TERMINAL_RUN_STATES = frozenset({
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "policy_stopped",
})
SERVER_OPTIONAL_CALLBACK_DIAGNOSTIC_EVENT_KINDS = frozenset({
    "ExternalCallbackRejected",
    "LateExternalCallbackReceived",
})
MAX_SERVER_REQUEST_JSON_DEPTH = 64
MAX_RUN_CURSOR_SEQUENCE = (1 << 64) - 1
MAX_SERVER_IDENTIFIER_BYTES = 4_096
MAX_SERVER_REASON_BYTES = 4_096
MAX_SERVER_TIMESTAMP_BYTES = 128
MAX_SERVER_FREE_TEXT_BYTES = 128 * 1024
MAX_SERVER_PATH_BYTES = 128 * 1024
DEFAULT_MAX_SERVER_REQUEST_BODY_BYTES = 1024 * 1024
DEFAULT_MAX_ASYNC_CALLBACK_REQUEST_BODY_BYTES = 512 * 1024
_SERVER_REQUEST_BODY_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_SERVER_EVENT_PAGE_EVENTS = 100
DEFAULT_MAX_SERVER_EVENT_PAGE_BYTES = 1024 * 1024
DEFAULT_MAX_SERVER_RUN_LIST_PAGE_RUNS = 100
MAX_SERVER_RUN_LIST_CURSOR_BYTES = 4_096
DEFAULT_MAX_ACCEPTED_RUN_RUNTIME_STATE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_IN_MEMORY_RUNS = 10_000
DEFAULT_MAX_IN_MEMORY_RUNS_PER_TENANT = 1_000
DEFAULT_MAX_RETIRED_RUN_TOMBSTONES = 10_000
DEFAULT_MAX_RUN_CONTROLS_PER_RUN = 256
DEFAULT_MAX_RUN_CONTROL_HISTORY_BYTES_PER_RUN = 1024 * 1024
DEFAULT_MAX_DETACHMENTS_PER_RUN = 256
DEFAULT_MAX_DETACHMENT_HISTORY_BYTES_PER_RUN = 256 * 1024
DEFAULT_MAX_SUBSCRIPTIONS_PER_RUN = 256
DEFAULT_MAX_SUBSCRIPTION_HISTORY_BYTES_PER_RUN = 1024 * 1024
DEFAULT_MAX_EVENT_ACKS_PER_SUBSCRIPTION = 4_096
DEFAULT_MAX_EVENT_ACK_HISTORY_BYTES_PER_SUBSCRIPTION = 1024 * 1024
DEFAULT_MAX_CALLBACK_OPERATION_HISTORIES = 4_096
DEFAULT_MAX_CALLBACK_SUBMISSION_HISTORY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ASYNC_CALLBACK_REJECTION_HISTORY_BYTES = 4 * 1024 * 1024
_DURABLE_ACCEPTED_RUN_REQUIRED_REASON_CODE = (
    "server.durable_accepted_run_required"
)
_DURABLE_ACCEPTED_RUN_REQUIRED_ERROR = (
    "restart-durable run continuation requires "
    "DurableAcceptedRunServerApp; set "
    "allow_process_local_accepted_runs_dev=True only for process-local "
    "development"
)
DEFAULT_MAX_OPTIONAL_CALLBACK_DIAGNOSTIC_EVENTS_PER_RUN = 256
DEFAULT_MAX_OPTIONAL_CALLBACK_DIAGNOSTIC_HISTORY_BYTES_PER_RUN = 1024 * 1024
DEFAULT_MAX_CALLBACK_REGISTRATIONS = 4_096
DEFAULT_MAX_CALLBACK_REGISTRATION_HISTORY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CALLBACK_DELIVERY_CONTROLS_PER_DELIVERY = 256
DEFAULT_MAX_CALLBACK_DELIVERY_CONTROL_HISTORY_BYTES_PER_DELIVERY = (
    1024 * 1024
)
DEFAULT_MAX_AUTH_AUDIT_EVENTS = 1_024
DEFAULT_MAX_SERVER_ERROR_AUDIT_EVENTS = 1_024
DEFAULT_TERMINAL_RUN_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_TERMINAL_RUN_COLLECTION_INTERVAL_SECONDS = 60
DEFAULT_TERMINAL_RUN_COLLECTION_BATCH_SIZE = 100
MAX_SERVER_CALLBACK_DELIVERY_IDENTIFIER_BYTES = 4_096
MAX_SERVER_CALLBACK_DELIVERY_ERROR_BYTES = 16_384
MAX_SERVER_CALLBACK_DELIVERY_TIMESTAMP_BYTES = 128
MAX_SERVER_AUTH_AUDIT_REQUEST_ID_BYTES = 256
MAX_SERVER_AUTH_AUDIT_FAILURE_TYPE_BYTES = 256
MAX_SERVER_ERROR_CODE_BYTES = 256
MAX_SERVER_ERROR_CORRELATION_ID_BYTES = 256
MAX_SERVER_ERROR_FAILURE_DETAIL_BYTES = 4_096


def _validate_server_lifecycle_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("server lifecycle timeout must be a finite non-negative number")
    try:
        timeout_seconds = float(timeout)
    except OverflowError as error:
        raise ValueError(
            "server lifecycle timeout must be a finite non-negative number"
        ) from error
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("server lifecycle timeout must be a finite non-negative number")
    return timeout_seconds


_ServerOperationHandlerKey = Literal[
    "system",
    "run_lifecycle",
    "subscriptions",
    "callback_registration",
    "callback_delivery_control",
    "async_callback_ingress",
    "application_events",
    "application_stream",
    "execution",
]


@dataclass(frozen=True, slots=True)
class _ServerOperationSpec:
    handler_key: _ServerOperationHandlerKey
    resource_parameter: str | None = None
    resource_kind: str | None = None
    auth_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.auth_required, bool):
            raise ValueError(
                "server operation auth_required must be a boolean"
            )
        if (self.resource_parameter is None) != (self.resource_kind is None):
            raise ValueError(
                "server operation resource parameter and kind must be "
                "declared together"
            )


_SERVER_OPERATION_REGISTRY = MappingProxyType(
    {
        "health": _ServerOperationSpec(
            "system",
            auth_required=False,
        ),
        "list_runs": _ServerOperationSpec("run_lifecycle"),
        "invoke_graph": _ServerOperationSpec("execution"),
        "get_run_status": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "delete_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "attach_to_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "detach_from_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "subscribe_events": _ServerOperationSpec(
            "subscriptions",
            "run_id",
            "run",
        ),
        "ack_event": _ServerOperationSpec(
            "subscriptions",
            "subscription_id",
            "event_subscription",
        ),
        "unsubscribe_events": _ServerOperationSpec(
            "subscriptions",
            "subscription_id",
            "event_subscription",
        ),
        "cancel_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "pause_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "resume_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "expire_run": _ServerOperationSpec(
            "run_lifecycle",
            "run_id",
            "run",
        ),
        "register_callback": _ServerOperationSpec(
            "callback_registration"
        ),
        "revoke_callback": _ServerOperationSpec(
            "callback_registration",
            "subscription_id",
            "callback_subscription",
        ),
        "redrive_callback_delivery": _ServerOperationSpec(
            "callback_delivery_control",
            "delivery_id",
            "callback_delivery",
        ),
        "move_callback_to_dead_letter": _ServerOperationSpec(
            "callback_delivery_control",
            "delivery_id",
            "callback_delivery",
        ),
        "submit_async_callback": _ServerOperationSpec(
            "async_callback_ingress",
            "operation_id",
            "callback_operation",
        ),
        "application_events": _ServerOperationSpec(
            "application_events",
            "run_id",
            "run",
        ),
        "application_stream": _ServerOperationSpec(
            "application_stream",
            "run_id",
            "run",
        ),
    }
)
_SERVER_AUTHORIZATION_RESOURCE_POLICIES = MappingProxyType(
    {
        operation: (
            spec.resource_parameter,
            spec.resource_kind,
        )
        for operation, spec in _SERVER_OPERATION_REGISTRY.items()
        if spec.resource_parameter is not None
        and spec.resource_kind is not None
    }
)


def _utc_now_iso() -> str:
    return system_audit_wall_timestamp()


def _canonical_json_size_bytes(value: object) -> int:
    return len(canonical_dumps(value).encode("utf-8"))


def _compact_server_field_name(field_name: str) -> str:
    return "".join(
        character.lower() for character in field_name if character.isalnum()
    )


def _server_field_uses_timestamp_policy(field_name: str) -> bool:
    return (
        field_name.endswith("_at")
        or field_name.endswith("At")
        or _compact_server_field_name(field_name) in {"now", "timestamp"}
    )


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    text = str.__str__(value)
    if unicodedata.normalize("NFC", text) != text:
        raise ValueError(
            f"{owner} {field_name} must use NFC Unicode normalization"
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in text
    ):
        raise ValueError(
            f"{owner} {field_name} must not contain control or directional characters"
        )
    compact_field_name = _compact_server_field_name(field_name)
    if _server_field_uses_timestamp_policy(field_name):
        max_bytes = MAX_SERVER_TIMESTAMP_BYTES
    elif field_name == "path":
        max_bytes = MAX_SERVER_PATH_BYTES
    elif compact_field_name == "reason":
        max_bytes = MAX_SERVER_REASON_BYTES
    else:
        max_bytes = MAX_SERVER_FREE_TEXT_BYTES
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(
            f"{owner} {field_name} must be at most "
            f"{max_bytes} UTF-8 bytes"
        )
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{owner} {field_name} must not be empty")
    return stripped


def _server_field_uses_identifier_policy(field_name: str) -> bool:
    compact = _compact_server_field_name(field_name)
    return compact.endswith(("id", "ids", "code", "codes", "digest")) or (
        compact == "idempotencykey"
    )


def _validate_server_identifier(
    owner: str,
    field_name: str,
    value: object,
    *,
    max_bytes: int = MAX_SERVER_IDENTIFIER_BYTES,
) -> str:
    text = _validate_non_empty_string(owner, field_name, value)
    if value != text:
        raise ValueError(
            f"{owner} {field_name} must not contain surrounding whitespace"
        )
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(
            f"{owner} {field_name} must be at most "
            f"{max_bytes} UTF-8 bytes"
        )
    if any(
        not character.isascii() or not 0x21 <= ord(character) <= 0x7E
        for character in text
    ):
        raise ValueError(
            f"{owner} {field_name} must contain only printable ASCII "
            "identifier characters"
        )
    return text


def _validate_exact_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if _server_field_uses_identifier_policy(field_name):
        return _validate_server_identifier(owner, field_name, value)
    text = _validate_non_empty_string(owner, field_name, value)
    if value != text:
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return text


def _validate_route_path(owner: str, value: object) -> str:
    path = _validate_non_empty_string(owner, "path", value)
    if not path.startswith("/"):
        raise ValueError(f"{owner} path must start with '/'")
    return path


def _validate_transport(value: object) -> ServerTransport:
    transport = _validate_non_empty_string("server", "transport", value)
    if transport not in VALID_SERVER_TRANSPORTS:
        raise ValueError("server transport must be one of http, sse, or websocket")
    return transport  # type: ignore[return-value]


def _validate_iso_datetime(owner: str, field_name: str, value: object) -> str:
    timestamp = _validate_exact_non_empty_string(owner, field_name, value)
    if len(timestamp.encode("utf-8")) > MAX_SERVER_TIMESTAMP_BYTES:
        raise ValueError(
            f"{owner} {field_name} must be at most "
            f"{MAX_SERVER_TIMESTAMP_BYTES} UTF-8 bytes"
        )
    if len(timestamp) <= 19 or timestamp[10] != "T":
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    suffix_start = 19
    if timestamp[suffix_start] == ".":
        suffix_start += 1
        fraction_start = suffix_start
        while suffix_start < len(timestamp) and timestamp[suffix_start].isdigit():
            suffix_start += 1
        if suffix_start == fraction_start:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    timezone_suffix = timestamp[suffix_start:]
    if timestamp.endswith("Z"):
        normalized = f"{timestamp[:-1]}+00:00"
    elif (
        len(timezone_suffix) != 6
        or timezone_suffix[0] not in {"+", "-"}
        or timezone_suffix[3] != ":"
        or not timezone_suffix[1:3].isdigit()
        or not timezone_suffix[4:6].isdigit()
        or int(timezone_suffix[1:3]) > 23
        or int(timezone_suffix[4:6]) > 59
    ):
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    else:
        normalized = timestamp
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    return timestamp


def _validate_run_cursor(owner: str, field_name: str, run_id: str, value: object) -> str:
    cursor = _validate_exact_non_empty_string(owner, field_name, value)
    prefix = f"{run_id}:"
    if not cursor.startswith(prefix):
        raise ValueError(f"{owner} {field_name} must belong to run {run_id!r}")
    sequence_text = cursor[len(prefix) :]
    if not sequence_text.isascii() or not sequence_text.isdecimal():
        raise ValueError(
            f"{owner} {field_name} must use '<run_id>:<sequence>' with a non-negative integer sequence"
        )
    if int(sequence_text) > MAX_RUN_CURSOR_SEQUENCE:
        raise ValueError(
            f"{owner} {field_name} sequence must be at most {MAX_RUN_CURSOR_SEQUENCE}"
        )
    return cursor


def _run_list_scope(
    principal: PrincipalRef | None,
) -> tuple[str, str | None, str | None]:
    if principal is None:
        return ("owner", None, None)
    if "operator" in principal.roles:
        return ("tenant", principal.tenant_id, None)
    return ("owner", principal.tenant_id, principal.principal_id)


def _run_list_scope_digest(
    scope: tuple[str, str | None, str | None],
) -> str:
    scope_kind, tenant_id, owner_principal_id = scope
    return canonical_hash(
        {
            "kind": scope_kind,
            "tenantId": tenant_id,
            "ownerPrincipalId": owner_principal_id,
        }
    )


def _encode_run_list_cursor(
    scope: tuple[str, str | None, str | None],
    run_id: str,
) -> str:
    return (
        f"v1|{_run_list_scope_digest(scope)}|"
        f"{quote(run_id, safe='')}"
    )


def _decode_run_list_cursor(
    value: object,
    scope: tuple[str, str | None, str | None],
) -> str:
    cursor = _validate_exact_non_empty_string(
        "server run list",
        "cursor",
        value,
    )
    if len(cursor.encode("utf-8")) > MAX_SERVER_RUN_LIST_CURSOR_BYTES:
        raise ValueError(
            "server run list cursor exceeds the maximum encoded size"
        )
    version, separator, remainder = cursor.partition("|")
    scope_digest, second_separator, encoded_run_id = remainder.partition("|")
    if (
        version != "v1"
        or not separator
        or not second_separator
        or scope_digest != _run_list_scope_digest(scope)
        or not encoded_run_id
    ):
        raise ValueError("server run list cursor is invalid for this scope")
    run_id = unquote(encoded_run_id)
    if not run_id or quote(run_id, safe="") != encoded_run_id:
        raise ValueError("server run list cursor is malformed")
    return run_id


def _server_request_json_body(request: ServerRequest, owner: str) -> object:
    try:
        encoded = request.body.decode("utf-8") or "{}"
        depth = 0
        in_string = False
        escaped = False
        for character in encoded:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > MAX_SERVER_REQUEST_JSON_DEPTH:
                    raise ValueError("JSON nesting exceeds the server request limit")
            elif character in "]}":
                depth -= 1

        decoded = json.loads(
            encoded,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        pending: list[tuple[object, int]] = [(decoded, 0)]
        while pending:
            value, value_depth = pending.pop()
            if isinstance(value, dict):
                container_depth = value_depth + 1
                if container_depth > MAX_SERVER_REQUEST_JSON_DEPTH:
                    raise ValueError("JSON nesting exceeds the server request limit")
                pending.extend((item, container_depth) for item in value.values())
            elif isinstance(value, list):
                container_depth = value_depth + 1
                if container_depth > MAX_SERVER_REQUEST_JSON_DEPTH:
                    raise ValueError("JSON nesting exceeds the server request limit")
                pending.extend((item, container_depth) for item in value)
        canonical_dumps(decoded)
        return decoded
    except (RecursionError, TypeError, ValueError) as error:
        raise ValueError(f"{owner} body must be valid JSON") from error


def _validate_callback_subscription_scope(value: object) -> str:
    scope = _validate_non_empty_string("server callback registration", "scope", value)
    if value != scope or scope not in VALID_CALLBACK_SUBSCRIPTION_SCOPES:
        raise ValueError(
            "server callback registration scope must be one of run, conversation, project, tenant, or deployment"
        )
    return scope


def _validate_callback_failure_policy(value: object) -> str:
    failure_policy = _validate_exact_non_empty_string("server subscription", "failure_policy", value)
    if failure_policy not in VALID_CALLBACK_FAILURE_POLICIES:
        raise ValueError(
            "server subscription failure_policy must be one of best_effort, retry_then_dead_letter, pause_run_on_failure, or fail_run_on_failure"
        )
    return failure_policy


def _validate_callback_subscription_status(owner: str, value: object) -> str:
    status = _validate_exact_non_empty_string(owner, "status", value)
    if status not in VALID_CALLBACK_SUBSCRIPTION_STATUSES:
        raise ValueError(f"{owner} status must be one of active, paused, expired, or revoked")
    return status


def _has_callback_dead_letter_config(config: Mapping[str, object], delivery: Mapping[str, object]) -> bool:
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
        isinstance(dead_letter, Mapping)
        or (isinstance(dead_letter, str) and bool(dead_letter.strip()))
        or isinstance(fallback, Mapping)
        or (isinstance(fallback, str) and bool(fallback.strip()))
    )


def _validate_mandatory_callback_policy(
    owner: str,
    config: Mapping[str, object],
    delivery: Mapping[str, object],
    failure_policy: str,
) -> None:
    mandatory = config.get("mandatory") is True or delivery.get("mandatory") is True
    if mandatory and failure_policy == "best_effort" and not _has_callback_dead_letter_config(config, delivery):
        raise ValueError(
            f"{owner} mandatory delivery requires retry, dead-letter, pause-run, or fail-run failure policy"
        )
    explicitly_retrying = (
        failure_policy == "retry_then_dead_letter"
        and ("failurePolicy" in config or "failure_policy" in config)
    )
    if explicitly_retrying and not _has_callback_dead_letter_config(config, delivery):
        raise ValueError(f"{owner} retrying callback failure policy requires dead-letter or fallback behavior")
    if failure_policy in MANDATORY_CALLBACK_FAILURE_POLICIES and not _has_callback_dead_letter_config(config, delivery):
        raise ValueError(f"{owner} mandatory callback failure policy requires dead-letter or fallback behavior")


def _validate_callback_not_authoritative(owner: str, config: Mapping[str, object]) -> None:
    authoritative_for = _server_alias_value(
        config,
        owner,
        "authoritative_for",
        "authoritativeFor",
    )
    source_of_truth = _server_alias_value(
        config,
        owner,
        "source_of_truth",
        "sourceOfTruth",
    )
    if source_of_truth is True or authoritative_for:
        raise ValueError(f"{owner} callback delivery must not be used as the source of truth")


def _webhook_url_is_unsafe(url: str) -> bool:
    return not validate_webhook_url(
        url,
        allowed_schemes=frozenset({"https"}),
    ).allowed


def _validate_callback_delivery_target(owner: str, delivery: Mapping[str, object]) -> None:
    raw_delivery_kind = delivery.get("kind", "")
    delivery_kind = _validate_non_empty_string(owner, "delivery.kind", raw_delivery_kind)
    if raw_delivery_kind != delivery_kind or delivery_kind not in VALID_CALLBACK_DELIVERY_KINDS:
        raise ValueError(
            f"{owner} delivery.kind must be one of webhook, websocket, sse, push_notification, email, or local_callback"
        )
    ordering = delivery.get("ordering")
    if (
        isinstance(ordering, Mapping)
        and ordering.get("mode") == "ordered"
        and delivery_kind not in ORDER_CAPABLE_CALLBACK_TARGETS
    ):
        raise ValueError(f"{owner} delivery.ordering requests ordered delivery on an unsupported target")
    if delivery_kind != "webhook":
        return
    raw_method = delivery.get("method", "POST")
    method = _validate_non_empty_string(owner, "delivery.method", raw_method)
    if raw_method != method or method != "POST":
        raise ValueError(f"{owner} delivery.method must be POST for webhook delivery")
    raw_url = delivery.get("url", "")
    url = _validate_non_empty_string(owner, "delivery.url", raw_url)
    if raw_url != url or _webhook_url_is_unsafe(url):
        raise ValueError(f"{owner} delivery.url is unsafe or forbidden by default egress policy")
    signing = delivery.get("signing")
    if not isinstance(signing, Mapping):
        raise ValueError(f"{owner} delivery.signing must be a mapping for webhook delivery")
    raw_algorithm = signing.get("algorithm", "")
    algorithm = _validate_non_empty_string(owner, "delivery.signing.algorithm", raw_algorithm)
    if raw_algorithm != algorithm or algorithm not in VALID_WEBHOOK_SIGNING_ALGORITHMS:
        raise ValueError(f"{owner} delivery.signing.algorithm must be one of hmac-sha256 or ed25519")
    _validate_non_empty_string(
        owner,
        "delivery.signing.secret_ref",
        _server_alias_value(
            signing,
            owner,
            "secret_ref",
            "secretRef",
            "",
        ),
    )


def _validate_string_mapping(
    owner: str,
    field_name: str,
    value: object,
    *,
    lowercase_keys: bool = False,
) -> MappingProxyType[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner} {field_name} must be a mapping")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        key_text = _validate_exact_non_empty_string(owner, f"{field_name} key", key)
        if not isinstance(item, str):
            raise ValueError(f"{owner} {field_name} values must be strings")
        if field_name == "path_params":
            item = _validate_server_identifier(
                owner,
                f"{field_name} value",
                item,
            )
        normalized_key = key_text.lower() if lowercase_keys else key_text
        if normalized_key in normalized:
            raise ValueError(f"{owner} {field_name} contains duplicate key {normalized_key!r}")
        normalized[normalized_key] = item
    return MappingProxyType(normalized)


def _validate_http_headers(owner: str, value: object) -> MappingProxyType[str, str]:
    headers = _validate_string_mapping(owner, "headers", value, lowercase_keys=True)
    token_punctuation = frozenset("!#$%&'*+-.^_`|~")
    for key, item in headers.items():
        if not all(
            character.isascii()
            and (character.isalnum() or character in token_punctuation)
            for character in key
        ):
            raise ValueError(f"{owner} headers key must be an HTTP token")
        if any(
            (ord(character) < 32 and character != "\t") or ord(character) == 127
            for character in item
        ):
            raise ValueError(f"{owner} headers values must not contain control characters")
    return headers


def _validate_http_message_head_framing(
    owner: str,
    headers: Mapping[str, str],
) -> int | None:
    content_length = headers.get("content-length")
    if content_length is not None and "transfer-encoding" in headers:
        raise ValueError(
            f"{owner} must not combine content-length and transfer-encoding"
        )
    if content_length is None:
        return None
    if not content_length.isascii() or not content_length.isdecimal():
        raise ValueError(f"{owner} content-length must use ASCII decimal digits")
    return int(content_length)


def _validate_http_message_framing(
    owner: str,
    headers: Mapping[str, str],
    body: bytes,
) -> None:
    content_length = _validate_http_message_head_framing(owner, headers)
    if content_length is not None and content_length != len(body):
        raise ValueError(f"{owner} content-length must match body length")


def _validate_string_sequence(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{owner} {field_name} must be a sequence")
    normalized = tuple(
        _validate_non_empty_string(
            owner,
            field_name,
            item,
        )
        for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{owner} {field_name} must not contain duplicates")
    return tuple(sorted(normalized))


def _freeze_json_value(
    owner: str,
    field_name: str,
    value: object,
    *,
    _depth: int = 0,
    _active_containers: set[int] | None = None,
) -> object:
    if _depth > MAX_SERVER_REQUEST_JSON_DEPTH:
        raise ValueError(f"{owner} {field_name} nesting exceeds the server JSON limit")
    if _active_containers is None:
        _active_containers = set()
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError(f"{owner} {field_name} must not be recursive")
        _active_containers.add(container_id)
        frozen: dict[str, object] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{owner} {field_name} keys must be non-empty strings")
                frozen[key] = _freeze_json_value(
                    owner,
                    f"{field_name}.{key}",
                    item,
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
            return MappingProxyType(frozen)
        finally:
            _active_containers.remove(container_id)
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in _active_containers:
            raise ValueError(f"{owner} {field_name} must not be recursive")
        _active_containers.add(container_id)
        try:
            return tuple(
                _freeze_json_value(
                    owner,
                    field_name,
                    item,
                    _depth=_depth + 1,
                    _active_containers=_active_containers,
                )
                for item in value
            )
        finally:
            _active_containers.remove(container_id)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{owner} {field_name} must be finite")
        return value
    raise ValueError(f"{owner} {field_name} must be a JSON value")


def _validate_server_event_filter(owner: str, event_filter: Mapping[str, object]) -> None:
    for raw_values, field_name in (
        (event_filter.get("types"), "types"),
        (
            _server_alias_value(
                event_filter,
                owner,
                "node_ids",
                "nodeIds",
            ),
            "node_ids",
        ),
        (
            _server_alias_value(
                event_filter,
                owner,
                "operation_ids",
                "operationIds",
            ),
            "operation_ids",
        ),
    ):
        if raw_values is not None:
            _validate_string_sequence(owner, f"event_filter.{field_name}", raw_values)
            if any(value != value.strip() for value in raw_values):  # type: ignore[union-attr]
                raise ValueError(
                    f"{owner} event_filter.{field_name} values must not contain surrounding whitespace"
                )

    visibility = event_filter.get("visibility")
    if visibility is not None:
        visibility_values = _validate_string_sequence(owner, "event_filter.visibility", visibility)
        if any(value != value.strip() for value in visibility) or any(
            value not in VALID_EVENT_VISIBILITIES for value in visibility_values
        ):
            raise ValueError(
                f"{owner} event_filter.visibility must contain only client, operator, internal, or audit_only"
            )

    severity_min = _server_alias_value(
        event_filter,
        owner,
        "severity_min",
        "severityMin",
    )
    if severity_min is not None:
        severity_min_text = _validate_non_empty_string(
            owner,
            "event_filter.severity_min",
            severity_min,
        )
        if severity_min != severity_min_text or severity_min_text not in SERVER_EVENT_SEVERITY_RANKS:
            raise ValueError(f"{owner} event_filter.severity_min is invalid")

    include_terminal_events = _server_alias_value(
        event_filter,
        owner,
        "include_terminal_events",
        "includeTerminalEvents",
    )
    if include_terminal_events is not None and not isinstance(include_terminal_events, bool):
        raise ValueError(f"{owner} event_filter.include_terminal_events must be a boolean")


def _authorized_callback_visibility(principal: PrincipalRef | None) -> tuple[str, ...]:
    allowed = ["client"]
    roles = set(principal.roles if principal is not None else ())
    if "operator" in roles:
        allowed.append("operator")
    if "internal" in roles:
        allowed.append("internal")
    if "audit" in roles or "auditor" in roles:
        allowed.append("audit_only")
    return tuple(allowed)


def _event_visibility(event: Mapping[str, object]) -> str | None:
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    for source in (metadata, event, payload):
        visibility = source.get("visibility")
        if visibility is None:
            continue
        if isinstance(visibility, str) and visibility in VALID_EVENT_VISIBILITIES:
            return visibility
        return None
    return "client"


def _event_visible_to_principal(event: Mapping[str, object], principal: PrincipalRef | None) -> bool:
    visibility = _event_visibility(event)
    return visibility is not None and visibility in _authorized_callback_visibility(principal)


def _constrain_event_filter_visibility(
    event_filter: Mapping[str, object],
    principal: PrincipalRef | None,
) -> Mapping[str, object]:
    allowed_visibility = _authorized_callback_visibility(principal)
    visibility = event_filter.get("visibility")
    if visibility is None:
        constrained_visibility = allowed_visibility
    else:
        requested_visibility = _validate_string_sequence(
            "server event subscription",
            "event_filter.visibility",
            visibility,
        )
        allowed = set(allowed_visibility)
        constrained_visibility = tuple(value for value in requested_visibility if value in allowed)
    constrained = dict(event_filter)
    constrained["visibility"] = list(constrained_visibility)
    return MappingProxyType(constrained)


def _principal_response_payload(owner: PrincipalRef) -> dict[str, object]:
    return {
        "principalId": owner.principal_id,
        "tenantId": owner.tenant_id,
        "groups": list(owner.groups),
        "roles": list(owner.roles),
        "attributes": _thaw_json_value(
            _freeze_json_value(
                "server principal",
                "attributes",
                owner.attributes,
            )
        ),
    }


def _principal_matches_owner(principal: PrincipalRef | None, owner: PrincipalRef) -> bool:
    if principal is None:
        return False
    return principal.principal_id == owner.principal_id and principal.tenant_id == owner.tenant_id


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _response_json_object(value: object) -> dict[str, object]:
    thawed = _thaw_json_value(value)
    if not isinstance(thawed, dict):
        raise ValueError("server response value must thaw to a JSON object")
    return thawed


def _server_alias_value(
    body: Mapping[str, object],
    owner: str,
    snake: str,
    camel: str,
    default: object = None,
) -> object:
    if snake in body and camel in body:
        raise ValueError(
            f"{owner} {snake} must not contain multiple field aliases"
        )
    return body.get(snake, body.get(camel, default))
