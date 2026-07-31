from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import inspect
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
import yaml

from graphblocks import BlockCatalog
from graphblocks import runtime as runtime_module
from graphblocks.plugins import (
    builtin_block_catalog,
    builtin_block_implementations,
)
from graphblocks.runtime import (
    ExecutionJournal,
    InProcessRuntime,
    JournalRecord,
    LocalExecutionJournal,
    LocalJournalRecord,
    LocalRunResult,
    LocalRuntime,
    RuntimeRegistry,
    SQLiteExecutionJournal,
    core_stdlib_registry,
    stdlib_registry,
)
from graphblocks.stdlib_governance import (
    GOVERNANCE_BLOCKS,
    GOVERNANCE_IMPLEMENTATIONS,
)
from graphblocks.stdlib_rag import RAG_BLOCKS, RAG_IMPLEMENTATIONS
from graphblocks.stdlib_runtime_handlers import core_stdlib_implementations


ROOT = Path(__file__).parents[1]


EXPECTED_CORE_STDLIB_BLOCKS = {
    "control.map@2",
    "control.select@1",
    "model.generate@1",
    "prompt.render@1",
}


_CYCLIC_LOCAL_JSON: dict[str, Any] = {}
_CYCLIC_LOCAL_JSON["self"] = _CYCLIC_LOCAL_JSON
_INVALID_LOCAL_JSON_OBJECTS = (
    pytest.param({"value": object()}, id="object"),
    pytest.param({"value": b"\x00\x01"}, id="bytes"),
    pytest.param({"value": {1, 2, 3}}, id="set"),
    pytest.param(_CYCLIC_LOCAL_JSON, id="cycle"),
    pytest.param({1: "value"}, id="non-string-key"),
    pytest.param({"value": float("nan")}, id="nan"),
    pytest.param({"value": float("inf")}, id="positive-infinity"),
    pytest.param({"value": float("-inf")}, id="negative-infinity"),
)


def _single_node_graph(block_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-contract"},
        "spec": {"nodes": {"block": {"block": block_id}}},
    }


def _catalog_for(block_id: str, outputs: list[dict[str, Any]]) -> BlockCatalog:
    type_id, version = block_id.rsplit("@", 1)
    return BlockCatalog.from_blocks(
        [{"typeId": type_id, "version": int(version), "outputs": outputs}]
    )


def _terminal_local_journal(
    run_id: str = "stable-local-journal",
) -> LocalExecutionJournal:
    journal = LocalExecutionJournal(run_id)
    journal.append_terminal("run_succeeded", {})
    return journal


def test_builtin_catalog_and_python_stdlib_have_exact_port_contract_parity() -> None:
    manifest = yaml.safe_load(
        (
            ROOT
            / "src"
            / "graphblocks"
            / "data"
            / "builtin-plugin.yaml"
        ).read_text(encoding="utf-8")
    )
    blocks = manifest["spec"]["blocks"]
    expected_ports = {
        f"{block['typeId']}@{block['version']}": (
            tuple(port["name"] for port in block.get("inputs", [])),
            tuple(port["name"] for port in block.get("outputs", [])),
        )
        for block in blocks
    }
    catalog = builtin_block_catalog()
    registry = stdlib_registry()

    assert set(catalog.descriptors) == set(registry.blocks) == set(expected_ports)
    for block_id, (expected_inputs, expected_outputs) in expected_ports.items():
        descriptor = catalog.get(block_id)
        assert descriptor is not None
        assert tuple(port.name for port in descriptor.inputs) == expected_inputs
        assert tuple(port.name for port in descriptor.outputs) == expected_outputs


def test_builtin_manifest_and_python_handlers_are_exactly_complete() -> None:
    manifest_bindings = builtin_block_implementations()
    registry = RuntimeRegistry(block_catalog=builtin_block_catalog())
    implementation_handlers = {
        **core_stdlib_implementations(registry.resolve),
        **RAG_IMPLEMENTATIONS,
        **GOVERNANCE_IMPLEMENTATIONS,
    }

    assert set(manifest_bindings) == set(registry.block_catalog.descriptors)
    assert set(manifest_bindings.values()) == set(implementation_handlers)
    for block_id, handler in {
        **RAG_BLOCKS,
        **GOVERNANCE_BLOCKS,
    }.items():
        assert implementation_handlers[manifest_bindings[block_id]] is handler


def test_stdlib_registry_fails_closed_when_a_manifest_handler_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graphblocks.stdlib_runtime_handlers as handler_module

    original = handler_module.core_stdlib_implementations

    def missing_prompt_handler(
        resolve: Any,
    ) -> dict[str, Any]:
        handlers = dict(original(resolve))
        handlers.pop("graphblocks.stdlib.prompt.render")
        return handlers

    monkeypatch.setattr(
        handler_module,
        "core_stdlib_implementations",
        missing_prompt_handler,
    )

    with pytest.raises(
        RuntimeError,
        match="graphblocks.stdlib.prompt.render",
    ):
        stdlib_registry()


def test_stdlib_registry_is_a_bounded_manifest_dispatcher() -> None:
    source = inspect.getsource(runtime_module._stdlib_registry)
    function = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
    )
    nested_functions = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.FunctionDef) and node is not function
    ]
    block_id_literals = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"[^\s@]+@[1-9][0-9]*", node.value)
    ]

    assert function.end_lineno is not None
    assert function.end_lineno - function.lineno + 1 <= 100
    assert nested_functions == []
    assert block_id_literals == []


def test_stdlib_handler_functions_have_bounded_ownership() -> None:
    source = (
        ROOT / "src" / "graphblocks" / "stdlib_runtime_handlers.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    function_lengths = {
        node.name: node.end_lineno - node.lineno + 1
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.end_lineno is not None
    }

    assert function_lengths
    assert max(function_lengths.values()) <= 350


def test_stdlib_inventories_are_cleanly_generated() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "tools/generate_stdlib_inventory.py",
            "--check",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_generated_stdlib_tck_inventory_matches_resolved_profiles() -> None:
    inventory = json.loads(
        (ROOT / "tck" / "stdlib" / "inventory.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_bindings = builtin_block_implementations()

    assert inventory["inventoryVersion"] == 1
    assert set(inventory["profiles"]) == {"preview", "stable"}
    for profile in ("preview", "stable"):
        catalog = builtin_block_catalog(profile=profile)
        blocks = inventory["profiles"][profile]["blocks"]
        assert [block["descriptor"] for block in blocks] == catalog.to_blocks()
        assert {
            block["blockId"]: block["implementation"]
            for block in blocks
        } == {
            block_id: manifest_bindings[block_id]
            for block_id in catalog.descriptors
        }

    preview_blocks = {
        block["blockId"]: block
        for block in inventory["profiles"]["preview"]["blocks"]
    }
    stable_blocks = {
        block["blockId"]: block
        for block in inventory["profiles"]["stable"]["blocks"]
    }
    assert "graph" in preview_blocks["control.map@2"]["descriptor"][
        "configSchema"
    ]["properties"]
    assert "graph" not in stable_blocks["control.map@2"]["descriptor"][
        "configSchema"
    ]["properties"]


def test_stable_core_stdlib_excludes_preview_profile_blocks() -> None:
    core_registry = core_stdlib_registry()
    preview_registry = stdlib_registry()

    assert set(core_registry.blocks) == EXPECTED_CORE_STDLIB_BLOCKS
    assert set(core_registry.block_catalog.descriptors) == EXPECTED_CORE_STDLIB_BLOCKS
    core_map_schema = core_registry.block_catalog.descriptors[
        "control.map@2"
    ].config_schema
    preview_map_schema = preview_registry.block_catalog.descriptors[
        "control.map@2"
    ].config_schema
    assert "graph" not in core_map_schema["properties"]
    assert "graph" in preview_map_schema["properties"]
    assert {
        "conversation.begin_turn@1",
        "async.await_callback@1",
        "retrieve.execute_plan@1",
        "review.request@1",
        "agent.run@1",
        "tools.resolve@1",
    } <= set(preview_registry.blocks) - set(core_registry.blocks)
    with pytest.raises(ValueError, match="not declared in the block catalog"):
        core_registry.resolve("control.map@2")(
            {"items": [{}]},
            {"block": "conversation.begin_turn@1"},
            {},
        )


def test_control_map_resolves_replaced_handler_at_execution_time() -> None:
    registry = core_stdlib_registry()

    def replacement(
        inputs: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return {"prompt": f"replacement:{inputs['message']['text']}"}

    registry.replace("prompt.render@1", replacement)

    result = registry.resolve("control.map@2")(
        {"items": [{"text": "one"}, {"text": "two"}]},
        {
            "block": "prompt.render@1",
            "inputName": "message",
            "outputName": "prompt",
        },
        {},
    )

    assert result == {"values": ["replacement:one", "replacement:two"]}


def test_stable_local_runtime_returns_only_terminal_c1_result() -> None:
    graph = yaml.safe_load(
        (ROOT / "compatibility/fixtures/cli-success.yaml").read_text(
            encoding="utf-8"
        )
    )

    result = LocalRuntime(core_stdlib_registry()).run(
        graph,
        {"message": {"text": "hello"}},
        run_id="stable-local-run",
    )

    assert isinstance(result, LocalRunResult)
    assert result.status == "succeeded"
    assert result.outputs == {"prompt": "Echo hello"}
    assert [record.kind for record in result.journal.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]
    assert not hasattr(result, "checkpoint")


def test_stable_local_journal_rejects_preview_callback_events() -> None:
    journal = LocalExecutionJournal("stable-local-journal")

    with pytest.raises(ValueError, match="unsupported local journal kind"):
        journal.append("external_callback_received", {})  # type: ignore[arg-type]


def test_stable_local_journal_requires_terminal_append_and_seals_afterward() -> None:
    journal = LocalExecutionJournal("stable-local-journal")

    with pytest.raises(RuntimeError, match="must be recorded with append_terminal"):
        journal.append("run_succeeded", {})

    assert journal.records == ()
    assert journal.terminal_kind is None

    journal.append_terminal("run_failed", {"error": "failed"})

    with pytest.raises(RuntimeError, match="after terminal run_failed"):
        journal.append("node_started", {"node": "too-late"})
    with pytest.raises(RuntimeError, match="terminal already recorded as run_failed"):
        journal.append_terminal("run_succeeded", {})


def test_stable_local_result_and_journal_preserve_terminal_invariants() -> None:
    journal = LocalExecutionJournal("stable-local-journal")
    journal.append("run_started", {})
    journal.append_terminal("run_succeeded", {"outputs": {"items": [1]}})
    result = LocalRunResult(
        run_id="stable-local-journal",
        status="succeeded",
        outputs={"items": [1]},
        journal=journal,
    )

    assert isinstance(journal.records, tuple)
    assert result.outputs == {"items": (1,)}
    with pytest.raises(FrozenInstanceError):
        journal.terminal_kind = "run_failed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.outputs["items"] = []  # type: ignore[index]

    with pytest.raises(ValueError, match="status must match"):
        LocalRunResult(
            run_id="stable-local-journal",
            status="failed",
            outputs={},
            journal=journal,
        )
    with pytest.raises(ValueError, match="invalid local result status"):
        LocalRunResult(
            run_id="stable-local-journal",
            status="waiting_callback",  # type: ignore[arg-type]
            outputs={},
            journal=journal,
        )


@pytest.mark.parametrize("payload", _INVALID_LOCAL_JSON_OBJECTS)
def test_stable_local_journal_rejects_non_json_payloads(
    payload: dict[object, Any],
) -> None:
    with pytest.raises(ValueError, match="local journal payload must be valid strict JSON"):
        LocalJournalRecord(
            sequence=1,
            kind="run_started",
            payload=payload,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("outputs", _INVALID_LOCAL_JSON_OBJECTS)
def test_stable_local_result_rejects_non_json_outputs(
    outputs: dict[object, Any],
) -> None:
    with pytest.raises(ValueError, match="local result outputs must be valid strict JSON"):
        LocalRunResult(
            run_id="stable-local-journal",
            status="succeeded",
            outputs=outputs,  # type: ignore[arg-type]
            journal=_terminal_local_journal(),
        )


@pytest.mark.parametrize(
    "items",
    [
        pytest.param(({"values": (1, 2)},), id="tuple"),
        pytest.param([{"values": [1, 2]}], id="list"),
    ],
)
def test_local_and_persistent_journals_share_canonical_json_normalization(
    items: object,
) -> None:
    payload = {"nested": {"items": items}}
    persistent = JournalRecord(1, "run_started", payload)
    local = LocalJournalRecord(1, "run_started", payload)
    expected = {"nested": {"items": [{"values": [1, 2]}]}}

    assert persistent.to_dict()["payload"] == expected
    assert local.to_dict()["payload"] == expected
    assert persistent.payload == local.payload


@pytest.mark.parametrize(
    "items",
    [
        pytest.param(({"values": (1, 2)},), id="tuple"),
        pytest.param([{"values": [1, 2]}], id="list"),
    ],
)
def test_stable_local_result_uses_canonical_json_normalization(
    items: object,
) -> None:
    result = LocalRunResult(
        run_id="stable-local-journal",
        status="succeeded",
        outputs={"nested": {"items": items}},
        journal=_terminal_local_journal(),
    )

    assert result.outputs == {
        "nested": {"items": [{"values": [1, 2]}]}
    }


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="space"),
        pytest.param("\t", id="tab"),
        pytest.param("\n", id="newline"),
        pytest.param(" stable-local-journal", id="leading-space"),
        pytest.param("stable-local-journal ", id="trailing-space"),
        pytest.param(object(), id="non-string"),
    ],
)
def test_journal_backends_and_local_result_reject_noncanonical_run_ids(
    run_id: object,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exact nonempty string"):
        ExecutionJournal(run_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact nonempty string"):
        LocalExecutionJournal(run_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact nonempty string"):
        SQLiteExecutionJournal(
            tmp_path / "journal.sqlite3",
            run_id,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="exact nonempty string"):
        LocalRunResult(
            run_id=run_id,  # type: ignore[arg-type]
            status="succeeded",
            outputs={},
            journal=_terminal_local_journal(),
        )


def test_conversation_runtime_aliases_use_canonical_internal_types() -> None:
    catalog = builtin_block_catalog()
    begin = catalog.get("conversation.begin_turn@1")
    agent = catalog.get("agent.run@1")
    commit = catalog.get("conversation.commit_turn@1")
    assert begin is not None
    assert agent is not None
    assert commit is not None

    assert {port.name: port.type_ref for port in begin.inputs} == {
        "conversationId": "graphblocks.ai/ConversationId@1",
        "conversation": "graphblocks.conversation/ConversationRef@1",
        "message": "graphblocks.conversation/Message@1",
    }
    assert {port.name: port.type_ref for port in begin.outputs} == {
        "transaction": "graphblocks.ai/ConversationTransaction@1",
        "snapshot": "graphblocks.ai/ConversationSnapshot@1",
        "conversation": "graphblocks.ai/ConversationSnapshot@1",
        "turn": "graphblocks.ai/ConversationTransaction@1",
    }
    assert {port.name: port.type_ref for port in agent.outputs}["message"] == (
        "graphblocks.ai/TurnCandidate@1"
    )
    assert {port.name: port.type_ref for port in commit.inputs}["response"] == (
        "graphblocks.ai/TurnCandidate@1"
    )


def test_acceptance_multi_turn_chat_executes_stdlib_alias_chain() -> None:
    documents = yaml.safe_load_all(
        (ROOT / "acceptance/scenarios/multi-turn-chat.yaml").read_text(encoding="utf-8")
    )
    graph = next(document for document in documents if document.get("kind") == "Graph")

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {
            "conversation": {"conversationId": "conversation-42", "messages": []},
            "message": {"role": "user", "text": "hello"},
        },
    )

    assert result.status == "succeeded"
    succeeded = {
        record.payload["node"]: tuple(record.payload["outputs"])
        for record in result.journal.records
        if record.kind == "node_succeeded"
    }
    assert succeeded == {
        "beginTurn": ("conversation", "snapshot", "transaction", "turn"),
        "respond": ("candidate", "message", "result"),
        "commitTurn": ("answer", "result"),
    }


def test_structured_generate_projects_example_specific_optional_outputs() -> None:
    block = stdlib_registry().resolve("model.structured_generate@1")

    result = block(
        {},
        {
            "outputSchema": "graphblocks.evaluation/InterviewScoreSet@1",
            "response": {"questions": ["q1"], "scores": [{"score": 1.0}]},
        },
        {},
    )

    assert result["questions"] == ["q1"]
    assert result["scores"] == [{"score": 1.0}]


def test_runtime_registry_rejects_undeclared_and_duplicate_blocks() -> None:
    block_id = "example.echo@1"
    block = lambda inputs, config, context: {"value": inputs.get("value")}  # noqa: E731

    with pytest.raises(ValueError, match="not declared in the block catalog"):
        RuntimeRegistry().register(block_id, block)
    with pytest.raises(ValueError, match="not declared in the block catalog"):
        RuntimeRegistry(blocks={block_id: block})

    registry = RuntimeRegistry(
        block_catalog=_catalog_for(block_id, [{"name": "value", "type": "Any"}])
    )
    registry.register(block_id, block)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(block_id, block)


def test_untyped_runtime_requires_explicit_opt_in() -> None:
    block_id = "example.untyped@1"
    registry = RuntimeRegistry(allow_untyped=True)
    registry.register(block_id, lambda inputs, config, context: {"value": 1})

    result = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert result.status == "succeeded"


def test_stdlib_untyped_opt_in_keeps_known_contracts_and_allows_custom_blocks() -> None:
    block_id = "example.extension@1"
    registry = stdlib_registry(allow_untyped=True)
    registry.register(block_id, lambda inputs, config, context: {"value": 1})

    result = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert result.status == "succeeded"


@pytest.mark.parametrize("catalog_allows_unknown", [False, True])
def test_strict_empty_catalog_rejects_graph_before_execution(
    catalog_allows_unknown: bool,
) -> None:
    registry = RuntimeRegistry(
        block_catalog=BlockCatalog({}, allow_unknown_blocks=catalog_allows_unknown)
    )
    with pytest.raises(ValueError, match="GB1022"):
        InProcessRuntime(registry).run(
            _single_node_graph("example.undeclared@1"),
            {},
        )


@pytest.mark.parametrize(
    ("outputs", "result", "message"),
    [
        ([], {"extra": 1}, "returned undeclared output\\(s\\): extra"),
        (
            [{"name": "required", "type": "Any"}],
            {},
            "omitted required output\\(s\\): required",
        ),
    ],
)
@pytest.mark.parametrize("allow_untyped", [False, True])
def test_catalog_backed_runtime_enforces_output_contract(
    outputs: list[dict[str, Any]],
    result: dict[str, Any],
    message: str,
    allow_untyped: bool,
) -> None:
    block_id = "example.output_contract@1"
    registry = RuntimeRegistry(
        block_catalog=_catalog_for(block_id, outputs),
        allow_untyped=allow_untyped,
    )
    registry.register(block_id, lambda inputs, config, context: result)

    run = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert run.status == "failed"
    failure = next(record for record in run.journal.records if record.kind == "node_failed")
    assert re.search(message, str(failure.payload["error"]))


def test_runtime_requires_conditionally_required_initial_output() -> None:
    block_id = "example.conditional_output@1"
    registry = RuntimeRegistry(
        block_catalog=_catalog_for(
            block_id,
            [
                {
                    "name": "initialEvidence",
                    "required": False,
                    "requiredWhen": {"phase": "initial"},
                }
            ],
        )
    )
    registry.register(block_id, lambda inputs, config, context: {})

    run = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert run.status == "failed"
    failure = next(
        record for record in run.journal.records if record.kind == "node_failed"
    )
    assert failure.payload["error"] == (
        "example.conditional_output@1 omitted required output(s): initialEvidence"
    )
