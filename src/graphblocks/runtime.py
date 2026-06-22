from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .compiler import compile_graph

JournalKind = Literal[
    "run_started",
    "node_started",
    "node_succeeded",
    "node_failed",
    "run_succeeded",
    "run_failed",
]
BlockCallable = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]


class JournalStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JournalRecord:
    sequence: int
    kind: JournalKind
    payload: dict[str, Any]


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


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: Literal["succeeded", "failed"]
    outputs: dict[str, Any]
    journal: ExecutionJournal


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

    def run(self, graph: dict[str, Any], inputs: dict[str, Any], run_id: str = "run-000001") -> RunResult:
        plan = compile_graph(graph)
        errors = [item for item in plan.diagnostics.diagnostics if item.severity == "error"]
        if errors:
            message = "; ".join(f"{item.code} {item.path}: {item.message}" for item in errors)
            raise ValueError(message)

        normalized = plan.normalized
        spec = normalized.get("spec", {})
        nodes = spec.get("nodes", {})
        edges = spec.get("edges", [])
        journal = ExecutionJournal(run_id)
        journal.append("run_started", {"graphHash": plan.graph_hash})

        node_inputs: dict[str, dict[str, Any]] = {name: {} for name in nodes}
        node_outputs: dict[str, dict[str, Any]] = {}
        output_values: dict[str, Any] = {}
        remaining = set(nodes)
        context = {
            "run_id": run_id,
            "turn_id": "turn-000001",
            "conversation_id": "conversation-default",
        }

        while remaining:
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
                journal.append("node_started", {"node": node_name, "block": block_id})
                try:
                    block = self.registry.resolve(block_id)
                    merged_inputs = {**node_inputs[node_name], **resolved_inputs}
                    result = block(merged_inputs, node.get("config", {}), {**context, "node": node_name})
                except Exception as exc:
                    journal.append("node_failed", {"node": node_name, "error": str(exc)})
                    journal.append_terminal("run_failed", {"node": node_name, "error": str(exc)})
                    return RunResult(run_id, "failed", output_values, journal)

                if not isinstance(result, dict):
                    journal.append("node_failed", {"node": node_name, "error": "block returned non-mapping output"})
                    journal.append_terminal("run_failed", {"node": node_name, "error": "block returned non-mapping output"})
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
                return RunResult(run_id, "failed", output_values, journal)

        journal.append_terminal("run_succeeded", {"outputs": output_values})
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

    def commit_turn(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        transaction = inputs["transaction"]
        candidate = inputs["candidate"]
        text = candidate["text"] if isinstance(candidate, dict) and "text" in candidate else str(candidate)
        return {
            "answer": {
                "conversationId": transaction["conversationId"],
                "text": text,
                "turnId": transaction["turnId"],
            }
        }

    registry.register("conversation.begin_turn@1", begin_turn)
    registry.register("prompt.render@1", prompt_render)
    registry.register("model.generate@1", scripted_generate)
    registry.register("conversation.commit_turn@1", commit_turn)
    return registry

