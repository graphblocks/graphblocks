from __future__ import annotations

from collections.abc import Mapping
import math
import re
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import time
from types import MappingProxyType
from typing import Any, Callable, Literal, Protocol

from .compiler import compile_graph
from .evaluation import ModelVisibleToolRef
from .leases import InMemoryLeasePool
from .run_store import InMemoryRunStore
from .tools import (
    BlockToolImplementation,
    GraphToolImplementation,
    McpToolImplementation,
    OpenApiToolImplementation,
    RemoteToolImplementation,
    ToolBinding,
    ToolCatalog,
    ToolDefinition,
    ToolResolutionScope,
)

JournalKind = Literal[
    "run_started",
    "node_started",
    "node_retry",
    "node_succeeded",
    "node_failed",
    "run_succeeded",
    "run_failed",
    "run_cancelled",
]
BlockCallable = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]
MAX_U64 = (1 << 64) - 1


class JournalLike(Protocol):
    @property
    def records(self) -> list[JournalRecord]:
        ...

    @property
    def terminal_kind(self) -> JournalKind | None:
        ...

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        ...

    def append_terminal(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        ...


JournalFactory = Callable[[str], JournalLike]


class JournalStateError(RuntimeError):
    pass


def parse_duration_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    for suffix, multiplier in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * multiplier
            except ValueError:
                return None
    try:
        return float(text)
    except ValueError:
        return None


def _configured_retry_attempts(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 1)
    return 1


def _freeze_json_like(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json_like(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_like(nested) for nested in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json_like(nested) for nested in value)
    return value


def _mutable_json_like(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json_like(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json_like(nested) for nested in value]
    if isinstance(value, list):
        return [_mutable_json_like(nested) for nested in value]
    return value


@dataclass(slots=True)
class CancellationToken:
    cancelled: bool = False
    reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        if self.cancelled:
            return
        self.cancelled = True
        self.reason = reason


@dataclass(frozen=True, slots=True)
class JournalRecord:
    sequence: int
    kind: JournalKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_json_like(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": _mutable_json_like(self.payload),
        }


@dataclass(slots=True)
class ExecutionJournal:
    run_id: str
    records: list[JournalRecord] = field(default_factory=list)
    terminal_kind: JournalKind | None = None

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        if self.terminal_kind is not None:
            raise JournalStateError(f"cannot append {kind} after terminal {self.terminal_kind}")
        record = JournalRecord(len(self.records) + 1, kind, payload)
        self.records.append(record)
        return record

    def append_terminal(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        if self.terminal_kind is not None:
            raise JournalStateError(f"terminal already recorded as {self.terminal_kind}")
        record = self.append(kind, payload)
        self.terminal_kind = kind
        return record


@dataclass(slots=True)
class SQLiteExecutionJournal:
    path: Path | str
    run_id: str
    connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS journal_records (
              run_id TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              kind TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              terminal INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (run_id, sequence)
            )
            """
        )
        self.connection.commit()

    @property
    def terminal_kind(self) -> JournalKind | None:
        row = self.connection.execute(
            """
            SELECT kind FROM journal_records
            WHERE run_id = ? AND terminal = 1
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (self.run_id,),
        ).fetchone()
        return None if row is None else row["kind"]

    @property
    def records(self) -> list[JournalRecord]:
        rows = self.connection.execute(
            """
            SELECT sequence, kind, payload_json FROM journal_records
            WHERE run_id = ?
            ORDER BY sequence
            """,
            (self.run_id,),
        ).fetchall()
        return [
            JournalRecord(int(row["sequence"]), row["kind"], json.loads(str(row["payload_json"])))
            for row in rows
        ]

    def append(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        terminal_kind = self.terminal_kind
        if terminal_kind is not None:
            raise JournalStateError(f"cannot append {kind} after terminal {terminal_kind}")
        row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM journal_records WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        sequence = int(row[0])
        self.connection.execute(
            """
            INSERT INTO journal_records (run_id, sequence, kind, payload_json, terminal)
            VALUES (?, ?, ?, ?, 0)
            """,
            (self.run_id, sequence, kind, json.dumps(payload, sort_keys=True, separators=(",", ":"))),
        )
        self.connection.commit()
        return JournalRecord(sequence, kind, dict(payload))

    def append_terminal(self, kind: JournalKind, payload: dict[str, Any]) -> JournalRecord:
        terminal_kind = self.terminal_kind
        if terminal_kind is not None:
            raise JournalStateError(f"terminal already recorded as {terminal_kind}")
        record = self.append(kind, payload)
        self.connection.execute(
            """
            UPDATE journal_records
            SET terminal = 1
            WHERE run_id = ? AND sequence = ?
            """,
            (self.run_id, record.sequence),
        )
        self.connection.commit()
        return record

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: Literal["succeeded", "failed", "cancelled"]
    outputs: dict[str, Any]
    journal: JournalLike


@dataclass(slots=True)
class RuntimeRegistry:
    blocks: dict[str, BlockCallable] = field(default_factory=dict)

    def register(self, block_id: str, block: BlockCallable) -> None:
        self.blocks[block_id] = block

    def resolve(self, block_id: str) -> BlockCallable:
        return self.blocks[block_id]


@dataclass(slots=True)
class InProcessRuntime:
    registry: RuntimeRegistry
    run_store: InMemoryRunStore | None = None
    cancellation_token: CancellationToken | None = None
    journal_factory: JournalFactory | None = None
    lease_pool: InMemoryLeasePool | None = None

    def run(self, graph: dict[str, Any], inputs: dict[str, Any], run_id: str = "run-000001") -> RunResult:
        plan = compile_graph(graph)
        errors = [item for item in plan.diagnostics.diagnostics if item.severity == "error"]
        if errors:
            message = "; ".join(f"{item.code} {item.path}: {item.message}" for item in errors)
            raise ValueError(message)

        normalized = plan.normalized
        if self.run_store is not None:
            stored = self.run_store.create_run(plan.graph_hash, inputs)
            run_id = stored.run_id
            self.run_store.set_status(run_id, "running")
        spec = normalized.get("spec", {})
        nodes = spec.get("nodes", {})
        edges = spec.get("edges", [])
        journal = self.journal_factory(run_id) if self.journal_factory is not None else ExecutionJournal(run_id)
        journal.append("run_started", {"graphHash": plan.graph_hash})

        node_inputs: dict[str, dict[str, Any]] = {name: {} for name in nodes}
        node_outputs: dict[str, dict[str, Any]] = {}
        output_values: dict[str, Any] = {}
        remaining = set(nodes)
        context = {
            "run_id": run_id,
            "turn_id": "turn-000001",
            "conversation_id": "conversation-default",
            "cancellation_token": self.cancellation_token or CancellationToken(),
            "lease_pool": self.lease_pool,
            "run_store": self.run_store,
        }

        while remaining:
            token = context["cancellation_token"]
            if isinstance(token, CancellationToken) and token.cancelled:
                journal.append_terminal("run_cancelled", {"reason": token.reason})
                if self.run_store is not None:
                    self.run_store.set_status(run_id, "cancelled")
                if self.lease_pool is not None:
                    self.lease_pool.release_all(run_id)
                return RunResult(run_id, "cancelled", {}, journal)
            progressed = False
            for node_name in sorted(remaining):
                inbound = [
                    edge
                    for edge in edges
                    if isinstance(edge, dict)
                    and isinstance(edge.get("to"), str)
                    and edge["to"].split(".", 1)[0] == node_name
                ]
                ready = True
                resolved_inputs: dict[str, Any] = {}
                for edge in inbound:
                    source = edge["from"]
                    source_owner, _, source_path = source.partition(".")
                    if source_owner == "$input":
                        value: Any = inputs
                        if source_path:
                            for part in source_path.split("."):
                                if isinstance(value, dict) and part in value:
                                    value = value[part]
                                else:
                                    ready = False
                                    break
                        if not ready:
                            break
                    elif source_owner in node_outputs:
                        value = node_outputs[source_owner]
                        if source_path:
                            for part in source_path.split("."):
                                if isinstance(value, dict) and part in value:
                                    value = value[part]
                                else:
                                    ready = False
                                    break
                        if not ready:
                            break
                    else:
                        ready = False
                        break

                    _, _, target_path = edge["to"].partition(".")
                    if not target_path:
                        ready = False
                        break
                    current = resolved_inputs
                    parts = target_path.split(".")
                    for part in parts[:-1]:
                        next_value = current.setdefault(part, {})
                        if not isinstance(next_value, dict):
                            ready = False
                            break
                        current = next_value
                    if not ready:
                        break
                    current[parts[-1]] = value

                if not ready:
                    continue

                node = nodes[node_name]
                block_id = str(node["block"])
                flow = node.get("flow", {})
                retry = flow.get("retry", {}) if isinstance(flow, dict) else {}
                timeout_seconds = parse_duration_seconds(flow.get("timeout")) if isinstance(flow, dict) else None
                max_attempts = 1
                idempotency_key = None
                if isinstance(retry, dict):
                    max_attempts = _configured_retry_attempts(
                        retry.get("maxAttempts", retry.get("max_attempts", 1))
                    )
                    idempotency_key = retry.get("idempotencyKey") or retry.get("idempotency_key")
                else:
                    max_attempts = _configured_retry_attempts(retry)
                result: dict[str, Any] | None = None
                for attempt in range(1, max_attempts + 1):
                    started_payload: dict[str, Any] = {"node": node_name, "block": block_id, "attempt": attempt}
                    if idempotency_key is not None:
                        started_payload["idempotencyKey"] = str(idempotency_key)
                    journal.append("node_started", started_payload)
                    try:
                        block = self.registry.resolve(block_id)
                        merged_inputs = {**node_inputs[node_name], **resolved_inputs}
                        started_at = time.monotonic()
                        deadline = None if timeout_seconds is None else started_at + timeout_seconds
                        attempt_context = {
                            **context,
                            "node": node_name,
                            "attempt": attempt,
                            "deadline_monotonic": deadline,
                        }
                        if idempotency_key is not None:
                            attempt_context["idempotency_key"] = str(idempotency_key)
                            attempt_context["idempotencyKey"] = str(idempotency_key)
                        attempt_result = block(
                            merged_inputs,
                            node.get("config", {}),
                            attempt_context,
                        )
                        if timeout_seconds is not None and time.monotonic() > started_at + timeout_seconds:
                            raise TimeoutError(f"node {node_name!r} exceeded timeout {flow.get('timeout')}")
                        if not isinstance(attempt_result, dict):
                            raise TypeError("block returned non-mapping output")
                        result = attempt_result
                        break
                    except Exception as exc:
                        token = context["cancellation_token"]
                        if isinstance(token, CancellationToken) and token.cancelled:
                            journal.append_terminal(
                                "run_cancelled",
                                {"reason": token.reason, "node": node_name, "attempt": attempt},
                            )
                            if self.run_store is not None:
                                self.run_store.set_status(run_id, "cancelled")
                            if self.lease_pool is not None:
                                self.lease_pool.release_all(run_id)
                            return RunResult(run_id, "cancelled", output_values, journal)
                        if attempt < max_attempts:
                            retry_payload: dict[str, Any] = {
                                "node": node_name,
                                "block": block_id,
                                "attempt": attempt,
                                "error": str(exc),
                            }
                            if idempotency_key is not None:
                                retry_payload["idempotencyKey"] = str(idempotency_key)
                            journal.append(
                                "node_retry",
                                retry_payload,
                            )
                            continue
                        journal.append("node_failed", {"node": node_name, "error": str(exc), "attempt": attempt})
                        journal.append_terminal("run_failed", {"node": node_name, "error": str(exc)})
                        if self.run_store is not None:
                            self.run_store.set_status(run_id, "failed")
                        if self.lease_pool is not None:
                            self.lease_pool.release_all(run_id)
                        return RunResult(run_id, "failed", output_values, journal)

                node_outputs[node_name] = result
                for edge in edges:
                    if not (
                        isinstance(edge, dict)
                        and isinstance(edge.get("from"), str)
                        and isinstance(edge.get("to"), str)
                        and edge["from"].split(".", 1)[0] == node_name
                        and edge["to"].startswith("$output.")
                    ):
                        continue
                    value = result
                    source_path = edge["from"].partition(".")[2]
                    if source_path:
                        for part in source_path.split("."):
                            value = value[part]
                    target_path = edge["to"].partition(".")[2]
                    current = output_values
                    parts = target_path.split(".")
                    for part in parts[:-1]:
                        nested = current.setdefault(part, {})
                        if not isinstance(nested, dict):
                            raise RuntimeError(f"output path conflict at {edge['to']}")
                        current = nested
                    current[parts[-1]] = value
                journal.append("node_succeeded", {"node": node_name, "outputs": sorted(result)})
                remaining.remove(node_name)
                progressed = True
                break

            if not progressed:
                unresolved = ", ".join(sorted(remaining))
                journal.append_terminal("run_failed", {"error": f"unresolved dependencies: {unresolved}"})
                if self.run_store is not None:
                    self.run_store.set_status(run_id, "failed")
                if self.lease_pool is not None:
                    self.lease_pool.release_all(run_id)
                return RunResult(run_id, "failed", output_values, journal)

        journal.append_terminal("run_succeeded", {"outputs": output_values})
        if self.run_store is not None:
            self.run_store.set_status(run_id, "succeeded")
        if self.lease_pool is not None:
            self.lease_pool.release_all(run_id)
        return RunResult(run_id, "succeeded", output_values, journal)


def stdlib_registry() -> RuntimeRegistry:
    registry = RuntimeRegistry()

    def begin_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(inputs.get("conversationId") or config.get("conversationId") or context["conversation_id"])
        return {"transaction": {"conversationId": conversation_id, "turnId": context["turn_id"]}}

    def prompt_render(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        template = str(config.get("template", "{message.text}"))

        def replace(match: re.Match[str]) -> str:
            value: Any = inputs
            for part in match.group(1).split("."):
                if isinstance(value, dict):
                    value = value[part]
                else:
                    value = getattr(value, part)
            return str(value)

        return {"prompt": re.sub(r"\{([A-Za-z0-9_.]+)\}", replace, template)}

    def scripted_generate(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        prompt = str(inputs.get("prompt", ""))
        script = config.get("script", {})
        if isinstance(script, dict) and prompt in script:
            text = str(script[prompt])
        else:
            text = str(config.get("response", prompt))
        return {"response": text}

    def resolve_tools(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        definitions = []
        definition_configs = config.get("definitions", [])
        if not isinstance(definition_configs, list | tuple):
            raise TypeError("tools.resolve@1 config.definitions must be a sequence")
        for index, item in enumerate(definition_configs):
            if not isinstance(item, dict):
                raise TypeError("tools.resolve@1 config.definitions entries must be mappings")
            definitions.append(
                ToolDefinition(
                    name=_required_string(item, "name", "name", f"config.definitions[{index}].name"),
                    description=_string_with_default(
                        item,
                        "description",
                        "description",
                        "",
                        f"config.definitions[{index}].description",
                    ),
                    input_schema=_required_string(
                        item,
                        "inputSchema",
                        "input_schema",
                        f"config.definitions[{index}].inputSchema",
                    ),
                    output_schema=_optional_string(
                        item,
                        "outputSchema",
                        "output_schema",
                        f"config.definitions[{index}].outputSchema",
                    ),
                    tags=_string_collection(item.get("tags", ()), f"config.definitions[{index}].tags"),
                    version=_optional_string(item, "version", "version", f"config.definitions[{index}].version"),
                )
            )

        bindings = []
        binding_configs = config.get("bindings", [])
        if not isinstance(binding_configs, list | tuple):
            raise TypeError("tools.resolve@1 config.bindings must be a sequence")
        for index, item in enumerate(binding_configs):
            if not isinstance(item, dict):
                raise TypeError("tools.resolve@1 config.bindings entries must be mappings")
            implementation_config = item.get("implementation")
            if not isinstance(implementation_config, dict):
                raise TypeError("tools.resolve@1 binding implementation must be a mapping")
            kind = _required_string(
                implementation_config,
                "kind",
                "kind",
                f"config.bindings[{index}].implementation.kind",
            )
            if kind == "block":
                implementation = BlockToolImplementation(
                    block=_required_string(
                        implementation_config,
                        "block",
                        "block",
                        f"config.bindings[{index}].implementation.block",
                    ),
                    input_mapping=_string_mapping(
                        implementation_config,
                        "inputMapping",
                        "input_mapping",
                        f"config.bindings[{index}].implementation.inputMapping",
                    ),
                    output_mapping=_string_mapping(
                        implementation_config,
                        "outputMapping",
                        "output_mapping",
                        f"config.bindings[{index}].implementation.outputMapping",
                    ),
                )
            elif kind == "graph":
                implementation = GraphToolImplementation(
                    graph=_required_string(
                        implementation_config,
                        "graph",
                        "graph",
                        f"config.bindings[{index}].implementation.graph",
                    ),
                    input_mapping=_string_mapping(
                        implementation_config,
                        "inputMapping",
                        "input_mapping",
                        f"config.bindings[{index}].implementation.inputMapping",
                    ),
                    output_mapping=_string_mapping(
                        implementation_config,
                        "outputMapping",
                        "output_mapping",
                        f"config.bindings[{index}].implementation.outputMapping",
                    ),
                )
            elif kind == "remote":
                implementation = RemoteToolImplementation(
                    connection=_required_string(
                        implementation_config,
                        "connection",
                        "connection",
                        f"config.bindings[{index}].implementation.connection",
                    ),
                    operation=_required_string(
                        implementation_config,
                        "operation",
                        "operation",
                        f"config.bindings[{index}].implementation.operation",
                    ),
                )
            elif kind == "mcp":
                implementation = McpToolImplementation(
                    server=_required_string(
                        implementation_config,
                        "server",
                        "server",
                        f"config.bindings[{index}].implementation.server",
                    ),
                    remote_name=_required_string(
                        implementation_config,
                        "remoteName",
                        "remote_name",
                        f"config.bindings[{index}].implementation.remoteName",
                    ),
                )
            elif kind == "openapi":
                implementation = OpenApiToolImplementation(
                    connection=_required_string(
                        implementation_config,
                        "connection",
                        "connection",
                        f"config.bindings[{index}].implementation.connection",
                    ),
                    operation_id=_required_string(
                        implementation_config,
                        "operationId",
                        "operation_id",
                        f"config.bindings[{index}].implementation.operationId",
                    ),
                )
            else:
                raise TypeError(f"tools.resolve@1 unsupported implementation kind {kind!r}")
            timeout_ms = item.get("timeoutMs", item.get("timeout_ms"))
            if timeout_ms is not None and (
                not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0
            ):
                raise TypeError(
                    f"tools.resolve@1 config.bindings[{index}].timeoutMs must be a non-negative integer"
                )
            bindings.append(
                ToolBinding(
                    binding_id=_required_string(
                        item,
                        "bindingId",
                        "binding_id",
                        f"config.bindings[{index}].bindingId",
                    ),
                    tool_name=_required_string(
                        item,
                        "toolName",
                        "tool_name",
                        f"config.bindings[{index}].toolName",
                    ),
                    implementation=implementation,
                    effects=_string_collection(item.get("effects", ()), f"config.bindings[{index}].effects"),
                    approval=_string_with_default(
                        item,
                        "approval",
                        "approval",
                        "policy",
                        f"config.bindings[{index}].approval",
                    ),
                    idempotency=_string_with_default(
                        item,
                        "idempotency",
                        "idempotency",
                        "optional",
                        f"config.bindings[{index}].idempotency",
                    ),
                    cancellation=_string_with_default(
                        item,
                        "cancellation",
                        "cancellation",
                        "cooperative",
                        f"config.bindings[{index}].cancellation",
                    ),
                    result_mode=_string_with_default(
                        item,
                        "resultMode",
                        "result_mode",
                        "value",
                        f"config.bindings[{index}].resultMode",
                    ),
                    timeout_ms=timeout_ms,
                    retry_policy_ref=_optional_string(
                        item,
                        "retryPolicyRef",
                        "retry_policy_ref",
                        f"config.bindings[{index}].retryPolicyRef",
                    ),
                    policy_profile_ref=_optional_string(
                        item,
                        "policyProfileRef",
                        "policy_profile_ref",
                        f"config.bindings[{index}].policyProfileRef",
                    ),
                    execution_class=_optional_string(
                        item,
                        "executionClass",
                        "execution_class",
                        f"config.bindings[{index}].executionClass",
                    ),
                )
            )

        scope_config = config.get("scope", {})
        if not isinstance(scope_config, dict):
            raise TypeError("tools.resolve@1 config.scope must be a mapping")
        scope = ToolResolutionScope(
            application_tools=_string_set(scope_config, "applicationTools", "application_tools"),
            graph_tools=_string_set(scope_config, "graphTools", "graph_tools"),
            principal_tools=_string_set(scope_config, "principalTools", "principal_tools"),
            tenant_policy_tools=_string_set(scope_config, "tenantPolicyTools", "tenant_policy_tools"),
            conversation_policy_tools=_string_set(
                scope_config,
                "conversationPolicyTools",
                "conversation_policy_tools",
            ),
            data_classification_tools=_string_set(
                scope_config,
                "dataClassificationTools",
                "data_classification_tools",
            ),
            deployment_tools=_string_set(scope_config, "deploymentTools", "deployment_tools"),
            budget_tools=_string_set(scope_config, "budgetTools", "budget_tools"),
        )
        policy_snapshot = inputs.get("policySnapshot")
        effective_policy_snapshot_id = str(config.get("effectivePolicySnapshotId") or "policy-snapshot-local")
        if isinstance(policy_snapshot, dict):
            effective_policy_snapshot_id = str(
                policy_snapshot.get("snapshot_id")
                or policy_snapshot.get("snapshotId")
                or effective_policy_snapshot_id
            )
        resolved = ToolCatalog(tuple(definitions), tuple(bindings)).resolve(
            scope,
            effective_policy_snapshot_id=effective_policy_snapshot_id,
        )
        return {
            "tools": [
                {
                    "resolved_tool_id": tool.resolved_tool_id,
                    "definition": tool.definition.model_contract(),
                    "binding": tool.binding.binding_contract(),
                    "definition_digest": tool.definition_digest,
                    "binding_digest": tool.binding_digest,
                    "effective_policy_snapshot_id": tool.effective_policy_snapshot_id,
                    "allowed_for_principal": tool.allowed_for_principal,
                    "valid_until": tool.valid_until,
                }
                for tool in resolved
            ]
        }

    def scripted_agent_run(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        tools = inputs.get("tools", [])
        if not isinstance(tools, list):
            raise TypeError("agent.run@1 input 'tools' must be a list")
        model_visible_tools: list[dict[str, Any]] = []
        provenance_tools: list[ModelVisibleToolRef] = []
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise TypeError(f"agent.run@1 input 'tools[{index}]' must be a mapping")
            definition = tool.get("definition")
            if not isinstance(definition, dict):
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition' must be a mapping")
            tool_name = definition.get("name")
            if not isinstance(tool_name, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition.name' must be a string")
            if not tool_name.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition.name' must not be empty")
            resolved_tool_id = tool.get("resolved_tool_id", tool.get("resolvedToolId"))
            if not isinstance(resolved_tool_id, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].resolved_tool_id' must be a string")
            if not resolved_tool_id.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].resolved_tool_id' must not be empty")
            definition_digest = tool.get("definition_digest", tool.get("definitionDigest"))
            if not isinstance(definition_digest, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition_digest' must be a string")
            if not definition_digest.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].definition_digest' must not be empty")
            binding_digest = tool.get("binding_digest", tool.get("bindingDigest"))
            if not isinstance(binding_digest, str):
                raise TypeError(f"agent.run@1 input 'tools[{index}].binding_digest' must be a string")
            if not binding_digest.strip():
                raise TypeError(f"agent.run@1 input 'tools[{index}].binding_digest' must not be empty")
            effective_policy_snapshot_id = tool.get(
                "effective_policy_snapshot_id",
                tool.get("effectivePolicySnapshotId"),
            )
            if not isinstance(effective_policy_snapshot_id, str):
                raise TypeError(
                    f"agent.run@1 input 'tools[{index}].effective_policy_snapshot_id' must be a string"
                )
            if not effective_policy_snapshot_id.strip():
                raise TypeError(
                    f"agent.run@1 input 'tools[{index}].effective_policy_snapshot_id' must not be empty"
                )
            allowed_for_principal = tool.get("allowed_for_principal", tool.get("allowedForPrincipal"))
            if not isinstance(allowed_for_principal, bool):
                raise TypeError(f"agent.run@1 input 'tools[{index}].allowed_for_principal' must be a boolean")
            if not allowed_for_principal:
                raise PermissionError(f"agent.run@1 input 'tools[{index}]' is not allowed for principal")
            valid_until = tool.get("valid_until", tool.get("validUntil"))
            model_visible_tools.append(
                {
                    "toolName": tool_name,
                    "resolvedToolId": resolved_tool_id,
                    "definitionDigest": definition_digest,
                    "bindingDigest": binding_digest,
                    "effectivePolicySnapshotId": effective_policy_snapshot_id,
                    "allowedForPrincipal": allowed_for_principal,
                    "validUntil": valid_until,
                }
            )
            provenance_tools.append(
                ModelVisibleToolRef(
                    tool_name=tool_name,
                    resolved_tool_id=resolved_tool_id,
                    definition_digest=definition_digest,
                    binding_digest=binding_digest,
                    effective_policy_snapshot_id=effective_policy_snapshot_id,
                    allowed_for_principal=allowed_for_principal,
                    valid_until=str(valid_until) if valid_until is not None else None,
                )
            )
        model_visible_tools.sort(
            key=lambda tool: (
                str(tool["toolName"]),
                str(tool["resolvedToolId"]),
            )
        )
        run_store = context.get("run_store")
        if run_store is not None:
            run_store.record_model_visible_tools(str(context["run_id"]), provenance_tools)
        messages = inputs.get("messages", [])
        if not isinstance(messages, list):
            raise TypeError("agent.run@1 input 'messages' must be a list")
        if "response" in config:
            text = str(config["response"])
            finish_reason = "scripted"
        elif messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                text = str(last_message.get("content", last_message.get("text", "")))
            else:
                text = str(last_message)
            finish_reason = "echo"
        else:
            text = ""
            finish_reason = "empty"
        output_policy = config.get("outputPolicy", config.get("output_policy"))
        output_policy = output_policy if isinstance(output_policy, dict) else {}
        output_policy_profile_ref = output_policy.get("profileRef", output_policy.get("profile_ref"))
        if not isinstance(output_policy_profile_ref, str) or not output_policy_profile_ref.strip():
            output_policy_profile_ref = None
        candidate = {
            "text": text,
            "finishReason": finish_reason,
            "toolCount": len(tools),
            "modelVisibleTools": model_visible_tools,
        }
        if output_policy_profile_ref is not None:
            candidate["outputPolicyProfileRef"] = output_policy_profile_ref
        return {
            "candidate": candidate
        }

    def commit_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        transaction = inputs["transaction"]
        if isinstance(transaction, dict) and transaction.get("status") == "policy_stopped":
            raise RuntimeError("conversation.commit_turn@1 cannot commit policy-stopped turn")
        candidate = inputs["candidate"]
        text = candidate["text"] if isinstance(candidate, dict) and "text" in candidate else str(candidate)
        return {
            "answer": {
                "conversationId": transaction["conversationId"],
                "text": text,
                "turnId": transaction["turnId"],
            }
        }

    def policy_stop_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        transaction = inputs["transaction"]
        if not isinstance(transaction, dict):
            raise TypeError("conversation.policy_stop_turn@1 requires transaction mapping")
        stopped = {
            "conversationId": transaction["conversationId"],
            "turnId": transaction["turnId"],
            "status": "policy_stopped",
            "draftDisposition": str(config.get("draftDisposition", "retract")),
            "committedMessageIds": [],
        }
        return {"transaction": stopped, "turn": stopped}

    def control_map(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        items = inputs.get("items", [])
        if not isinstance(items, list):
            raise TypeError("control.map@2 input 'items' must be a list")
        block_id = config["block"]
        input_name = str(config.get("inputName", "item"))
        output_name = config.get("outputName")
        block_config = config.get("config", {})
        if not isinstance(block_config, dict):
            raise TypeError("control.map@2 config.config must be a mapping")
        block = registry.resolve(str(block_id))
        outcomes: list[dict[str, Any]] = []
        values: list[Any] = []
        for index, item in enumerate(items):
            try:
                result = block({input_name: item}, block_config, {**context, "map_index": index})
                if not isinstance(result, dict):
                    raise TypeError("mapped block returned non-mapping output")
                value = result if output_name is None else result[str(output_name)]
                values.append(value)
                outcomes.append({"status": "succeeded", "value": value})
            except Exception as exc:
                if config.get("onError") != "collect":
                    raise
                outcomes.append({"status": "failed", "error": str(exc)})
        if config.get("onError") == "collect":
            return {"outcomes": outcomes, "values": values}
        return {"values": values}

    def control_select(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        cases = inputs.get("cases", {})
        if not isinstance(cases, dict):
            raise TypeError("control.select@1 input 'cases' must be a mapping")
        order = config.get("order")
        if order is None:
            order = list(cases)
        if not isinstance(order, list):
            raise TypeError("control.select@1 config.order must be a list")
        for key in order:
            if key in cases:
                return {"value": cases[key], "selected": key}
        if "default" in config:
            return {"value": config["default"], "selected": "default"}
        raise KeyError("control.select@1 found no present case")

    def async_start_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation_id = _required_async_string(config, "operationId", "operation_id", "operationId")
        run_id = _required_async_string(config, "runId", "run_id", "runId")
        node_id = _required_async_string(config, "nodeId", "node_id", "nodeId")
        attempt_id = _required_async_string(config, "attemptId", "attempt_id", "attemptId")
        kind = _required_async_string(config, "kind", "kind", "kind")
        resume_token_hash = _required_async_string(
            config,
            "resumeTokenHash",
            "resume_token_hash",
            "resumeTokenHash",
        )
        idempotency_key = _required_async_string(
            config,
            "idempotencyKey",
            "idempotency_key",
            "idempotencyKey",
        )
        expected_schema = _required_async_string(config, "expectedSchema", "expected_schema", "expectedSchema")
        created_at_unix_ms = _required_async_u64(config, "createdAtUnixMs", "created_at_unix_ms", "createdAtUnixMs")
        operation: dict[str, Any] = {
            "operation_id": operation_id,
            "run_id": run_id,
            "node_id": node_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "provider_operation_id": None,
            "state": "created",
            "resume_token_hash": resume_token_hash,
            "idempotency_key": idempotency_key,
            "expected_schema": expected_schema,
            "created_at_unix_ms": created_at_unix_ms,
            "submitted_at_unix_ms": None,
            "expires_at_unix_ms": None,
            "infinite_wait_policy": None,
            "completed_at_unix_ms": None,
        }
        provider_operation_id = _optional_async_string(
            config,
            "providerOperationId",
            "provider_operation_id",
            "providerOperationId",
        )
        if provider_operation_id is not None:
            operation["provider_operation_id"] = provider_operation_id
            submitted_at_unix_ms = _required_async_u64(
                config,
                "submittedAtUnixMs",
                "submitted_at_unix_ms",
                "submittedAtUnixMs",
            )
            if submitted_at_unix_ms < created_at_unix_ms:
                raise ValueError("async.start_operation@1 invalid operation: submitted_at precedes created_at")
            operation["submitted_at_unix_ms"] = submitted_at_unix_ms
            operation["state"] = "submitted"
        expires_at_unix_ms = _optional_async_u64(config, "expiresAtUnixMs", "expires_at_unix_ms", "expiresAtUnixMs")
        if expires_at_unix_ms is None:
            timeout_ms = _optional_duration_ms(config, ("timeoutMs", "timeout_ms", "timeout"), "timeout")
            if timeout_ms is not None:
                if created_at_unix_ms > MAX_U64 - timeout_ms:
                    raise ValueError("async.start_operation@1 timeout exceeds timestamp range")
                expires_at_unix_ms = created_at_unix_ms + timeout_ms
        infinite_wait_policy = _optional_async_string(
            config,
            "infiniteWaitPolicy",
            "infinite_wait_policy",
            "infiniteWaitPolicy",
        )
        if infinite_wait_policy is not None:
            operation["infinite_wait_policy"] = infinite_wait_policy
        if expires_at_unix_ms is not None:
            submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
            if not isinstance(submitted_at_unix_ms, int) or isinstance(submitted_at_unix_ms, bool):
                raise ValueError("async.start_operation@1 invalid operation: non-created operations require submitted_at")
            if (
                expires_at_unix_ms <= submitted_at_unix_ms
            ):
                raise ValueError("async.start_operation@1 invalid operation: expires_at must be after submitted_at")
            operation["expires_at_unix_ms"] = expires_at_unix_ms
            operation["state"] = "waiting_callback"
        elif infinite_wait_policy is not None:
            submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
            if not isinstance(submitted_at_unix_ms, int) or isinstance(submitted_at_unix_ms, bool):
                raise ValueError("async.start_operation@1 invalid operation: non-created operations require submitted_at")
            operation["state"] = "waiting_callback"
        if "subject" in inputs:
            operation["subject"] = inputs["subject"]
        return {"operation": operation}

    def async_await_callback(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = _required_async_operation_input(inputs, "async.await_callback@1")
        if operation.get("state") != "waiting_callback":
            raise RuntimeError(
                f"async.await_callback@1 operation must be waiting_callback, got {operation.get('state')!r}"
            )
        on_timeout = str(config.get("onTimeout", config.get("on_timeout", "fail")))
        if on_timeout not in {"fail", "cancel", "expire"}:
            raise ValueError("async.await_callback@1 onTimeout must be one of fail, cancel, or expire")
        checkpoint = config.get("checkpoint", True)
        if not isinstance(checkpoint, bool):
            raise ValueError("async.await_callback@1 checkpoint must be a boolean")
        wait: dict[str, Any] = {
            "state": "waiting_callback",
            "operation": operation,
            "checkpoint": checkpoint,
            "onTimeout": on_timeout,
        }
        timeout_ms = _optional_duration_ms(config, ("timeoutMs", "timeout_ms", "timeout"), "timeout")
        if timeout_ms is not None:
            wait["timeoutMs"] = timeout_ms
        infinite_wait_policy = _optional_async_string(
            config,
            "infiniteWaitPolicy",
            "infinite_wait_policy",
            "infiniteWaitPolicy",
        )
        if infinite_wait_policy is not None:
            wait["infiniteWaitPolicy"] = infinite_wait_policy
        return {"wait": wait}

    def async_poll_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = dict(_required_async_operation_input(inputs, "async.poll_operation@1"))
        interval_ms = _optional_duration_ms(config, ("intervalMs", "interval_ms", "interval"), "interval") or 30_000
        max_interval_ms = (
            _optional_duration_ms(config, ("maxIntervalMs", "max_interval_ms", "maxInterval", "max_interval"), "maxInterval")
            or interval_ms
        )
        if max_interval_ms < interval_ms:
            raise ValueError("async.poll_operation@1 maxInterval must not be less than interval")
        timeout_ms = _optional_duration_ms(config, ("timeoutMs", "timeout_ms", "timeout"), "timeout")
        infinite_wait_policy = _optional_async_string(
            config,
            "infiniteWaitPolicy",
            "infinite_wait_policy",
            "infiniteWaitPolicy",
        )
        if timeout_ms is None and infinite_wait_policy is None:
            raise ValueError("async.poll_operation@1 requires timeoutMs")
        operation["state"] = "polling"
        poll = {
            "state": "polling",
            "operation": operation,
            "intervalMs": interval_ms,
            "maxIntervalMs": max_interval_ms,
        }
        if timeout_ms is not None:
            poll["timeoutMs"] = timeout_ms
        if infinite_wait_policy is not None:
            poll["infiniteWaitPolicy"] = infinite_wait_policy
        return {"poll": poll}

    def async_complete_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = _required_async_operation_input(inputs, "async.complete_operation@1")
        return {
            "result": _async_operation_result(
                str(operation["operation_id"]),
                "completed",
                output=inputs.get("output"),
                external_effects=_async_external_effects(config, "async.complete_operation@1"),
                completed_at_unix_ms=None,
            )
        }

    def async_cancel_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = _required_async_operation_input(inputs, "async.cancel_operation@1")
        completed_at_unix_ms = _optional_async_u64(
            config,
            "cancelledAtUnixMs",
            "cancelled_at_unix_ms",
            "cancelledAtUnixMs",
        )
        _validate_async_terminal_timestamp(operation, completed_at_unix_ms, "async.cancel_operation@1")
        return {
            "result": _async_operation_result(
                str(operation["operation_id"]),
                "cancelled",
                external_effects=_async_external_effects(config, "async.cancel_operation@1"),
                completed_at_unix_ms=completed_at_unix_ms,
            )
        }

    def async_expire_operation(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        operation = _required_async_operation_input(inputs, "async.expire_operation@1")
        completed_at_unix_ms = _optional_async_u64(
            config,
            "expiredAtUnixMs",
            "expired_at_unix_ms",
            "expiredAtUnixMs",
        )
        _validate_async_terminal_timestamp(operation, completed_at_unix_ms, "async.expire_operation@1")
        return {
            "result": _async_operation_result(
                str(operation["operation_id"]),
                "expired",
                external_effects=_async_external_effects(config, "async.expire_operation@1"),
                completed_at_unix_ms=completed_at_unix_ms,
            )
        }

    def _config_value(config: Mapping[str, Any], camel_key: str, snake_key: str) -> tuple[bool, Any]:
        if camel_key in config:
            return True, config[camel_key]
        if snake_key in config:
            return True, config[snake_key]
        return False, None

    def _validate_config_string(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"tools.resolve@1 {label} must be a string")
        if not value.strip():
            raise TypeError(f"tools.resolve@1 {label} must not be empty")
        return value

    def _required_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"tools.resolve@1 {label} is required")
        return _validate_config_string(value, label)

    def _optional_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        return _validate_config_string(value, label)

    def _string_with_default(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        default: str,
        label: str,
    ) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            return default
        return _validate_config_string(value, label)

    def _string_collection(value: Any, label: str) -> frozenset[str]:
        if not isinstance(value, list | tuple | set | frozenset):
            raise TypeError(f"tools.resolve@1 {label} must be a sequence")
        if any(not isinstance(item, str) for item in value):
            raise TypeError(f"tools.resolve@1 {label} entries must be strings")
        if any(not item.strip() for item in value):
            raise TypeError(f"tools.resolve@1 {label} entries must not be empty")
        return frozenset(value)

    def _string_mapping(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        label: str,
    ) -> dict[str, str]:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"tools.resolve@1 {label} must be a mapping")
        mapping = dict(value)
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in mapping.items()):
            raise TypeError(f"tools.resolve@1 {label} entries must be strings")
        return mapping

    def _string_set(config: dict[str, Any], camel_key: str, snake_key: str) -> frozenset[str] | None:
        value = config.get(camel_key, config.get(snake_key))
        if value is None:
            return None
        return _string_collection(value, f"scope {camel_key}")

    def _required_async_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"async.start_operation@1 config.{label} is required")
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"async.start_operation@1 config.{label} must be a non-empty string")
        return value

    def _optional_async_string(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> str | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"async operation config.{label} must be a non-empty string")
        return value

    def _required_async_u64(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> int:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"async.start_operation@1 config.{label} is required")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_U64:
            raise TypeError(f"async operation config.{label} must be an unsigned 64-bit integer")
        return value

    def _optional_async_u64(config: Mapping[str, Any], camel_key: str, snake_key: str, label: str) -> int | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_U64:
            raise TypeError(f"async operation config.{label} must be an unsigned 64-bit integer")
        return value

    def _optional_duration_ms(config: Mapping[str, Any], keys: tuple[str, ...], label: str) -> int | None:
        value = None
        for key in keys:
            if key in config:
                value = config[key]
                break
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"async operation config.{label} must be a positive duration")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError(f"async operation config.{label} must be a positive duration")
            if value > MAX_U64:
                raise ValueError(f"async operation config.{label} must be an unsigned 64-bit duration")
            return value
        seconds = parse_duration_seconds(value)
        if seconds is None or seconds <= 0:
            raise ValueError(f"async operation config.{label} must be a positive duration")
        duration_ms = seconds * 1000
        if not math.isfinite(duration_ms) or duration_ms > MAX_U64:
            raise ValueError(f"async operation config.{label} must be an unsigned 64-bit duration")
        return int(duration_ms)

    def _required_async_operation_input(inputs: Mapping[str, Any], block_label: str) -> dict[str, Any]:
        operation = inputs.get("operation")
        if not isinstance(operation, dict):
            raise TypeError(f"{block_label} requires operation input")
        if not isinstance(operation.get("operation_id"), str) or not str(operation.get("operation_id")).strip():
            raise TypeError(f"{block_label} input operation.operation_id must be a non-empty string")
        return operation

    def _validate_async_terminal_timestamp(
        operation: Mapping[str, Any],
        completed_at_unix_ms: int | None,
        block_label: str,
    ) -> None:
        if completed_at_unix_ms is None:
            return
        if completed_at_unix_ms == 0:
            raise ValueError(f"{block_label} terminal timestamp must be positive")
        submitted_at_unix_ms = operation.get("submitted_at_unix_ms")
        if isinstance(submitted_at_unix_ms, int) and not isinstance(submitted_at_unix_ms, bool):
            if completed_at_unix_ms < submitted_at_unix_ms:
                raise ValueError(f"{block_label} terminal timestamp must not be earlier than submitted_at_unix_ms")

    def _async_operation_result(
        operation_id: str,
        status: str,
        *,
        output: Any = None,
        external_effects: list[dict[str, Any]] | None = None,
        completed_at_unix_ms: int | None,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "status": status,
            "output": output,
            "artifacts": [],
            "diagnostics": [],
            "metrics": [],
            "checks": [],
            "usage": [],
            "external_effects": [] if external_effects is None else external_effects,
            "completed_at_unix_ms": completed_at_unix_ms,
        }

    def _async_external_effects(config: Mapping[str, Any], block_label: str) -> list[dict[str, Any]]:
        raw_effects = config.get("externalEffects", config.get("external_effects", []))
        if not isinstance(raw_effects, list | tuple):
            raise TypeError(f"{block_label} config.externalEffects must be a sequence")
        effects = []
        for index, raw_effect in enumerate(raw_effects):
            if not isinstance(raw_effect, Mapping):
                raise TypeError(f"{block_label} config.externalEffects[{index}] must be a mapping")
            effect = {
                "effect_id": _required_effect_string(raw_effect, "effectId", "effect_id", "effectId", block_label),
                "target": _required_effect_string(raw_effect, "target", "target", "target", block_label),
                "operation": _required_effect_string(raw_effect, "operation", "operation", "operation", block_label),
                "outcome": _required_effect_string(raw_effect, "outcome", "outcome", "outcome", block_label),
                "idempotency_key": None,
                "provider_effect_id": None,
            }
            if effect["outcome"] not in {"no_external_effect", "committed", "not_committed", "unknown"}:
                raise ValueError(f"{block_label} config.externalEffects[{index}].outcome is unsupported")
            idempotency_key = _optional_effect_string(
                raw_effect,
                "idempotencyKey",
                "idempotency_key",
                "idempotencyKey",
                block_label,
            )
            if idempotency_key is not None:
                effect["idempotency_key"] = idempotency_key
            provider_effect_id = _optional_effect_string(
                raw_effect,
                "providerEffectId",
                "provider_effect_id",
                "providerEffectId",
                block_label,
            )
            if provider_effect_id is not None:
                if effect["outcome"] != "committed":
                    raise ValueError(f"{block_label} provider identity but no committed external effect")
                effect["provider_effect_id"] = provider_effect_id
            effects.append(effect)
        return effects

    def _required_effect_string(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        label: str,
        block_label: str,
    ) -> str:
        found, value = _config_value(config, camel_key, snake_key)
        if not found:
            raise TypeError(f"{block_label} config.externalEffects.{label} is required")
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{block_label} config.externalEffects.{label} must be a non-empty string")
        return value

    def _optional_effect_string(
        config: Mapping[str, Any],
        camel_key: str,
        snake_key: str,
        label: str,
        block_label: str,
    ) -> str | None:
        found, value = _config_value(config, camel_key, snake_key)
        if not found or value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{block_label} config.externalEffects.{label} must be a non-empty string")
        return value

    registry.register("conversation.begin_turn@1", begin_turn)
    registry.register("prompt.render@1", prompt_render)
    registry.register("model.generate@1", scripted_generate)
    registry.register("tools.resolve@1", resolve_tools)
    registry.register("agent.run@1", scripted_agent_run)
    registry.register("conversation.commit_turn@1", commit_turn)
    registry.register("conversation.policy_stop_turn@1", policy_stop_turn)
    registry.register("control.map@2", control_map)
    registry.register("control.select@1", control_select)
    registry.register("async.start_operation@1", async_start_operation)
    registry.register("async.await_callback@1", async_await_callback)
    registry.register("async.poll_operation@1", async_poll_operation)
    registry.register("async.complete_operation@1", async_complete_operation)
    registry.register("async.cancel_operation@1", async_cancel_operation)
    registry.register("async.expire_operation@1", async_expire_operation)
    return registry
