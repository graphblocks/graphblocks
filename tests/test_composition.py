from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from graphblocks.canonical import canonical_hash, normalize_graph
from graphblocks.cli import main
from graphblocks.compiler import compile_graph
from graphblocks.composition import CompositionError, compose_documents


TEXT_SCHEMA = "graphblocks.ai/Text@1"
COMPOSITION_API_VERSION = "graphblocks.ai/composition/v1alpha1"


def _fragment(
    *,
    name: str = "render-prompt",
    input_schema: str = TEXT_SCHEMA,
    output_schema: str = TEXT_SCHEMA,
) -> dict[str, object]:
    return {
        "apiVersion": COMPOSITION_API_VERSION,
        "kind": "GraphFragment",
        "metadata": {"name": name},
        "spec": {
            "interface": {
                "inputs": {"message": input_schema},
                "outputs": {"prompt": output_schema},
            },
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Composed {message.text}"},
                }
            },
            "edges": [
                {"from": "$input.message", "to": "render.message"},
                {"from": "render.prompt", "to": "$output.prompt"},
            ],
        },
    }


def _binding(name: str = "local-model") -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha1",
        "kind": "Binding",
        "metadata": {"name": name},
        "spec": {"resources": {}},
    }


def _composed_graph(
    *,
    fragment_path: str = "fragment.yaml",
    imports: dict[str, object] | None = None,
    slot_input_schema: str = TEXT_SCHEMA,
    slot_output_schema: str = TEXT_SCHEMA,
    extra_nodes: dict[str, object] | None = None,
) -> dict[str, object]:
    graph_imports = imports or {"prompt": {"path": fragment_path}}
    nodes: dict[str, object] = {
        "answer": {
            "slot": "answer-slot",
            "inputs": {"message": "$input.message"},
            "outputs": {"prompt": "$output.prompt"},
        }
    }
    nodes.update(extra_nodes or {})
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "composed-prompt"},
        "spec": {
            "interface": {
                "inputs": {"message": TEXT_SCHEMA},
                "outputs": {"prompt": TEXT_SCHEMA},
            },
            "composition": {
                "apiVersion": COMPOSITION_API_VERSION,
                "imports": graph_imports,
                "slots": {
                    "answer-slot": {
                        "interface": {
                            "inputs": {"message": slot_input_schema},
                            "outputs": {"prompt": slot_output_schema},
                        },
                        "fill": {"fragment": "prompt/render-prompt"},
                    }
                },
            },
            "nodes": nodes,
        },
    }


def _monolithic_graph() -> dict[str, object]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "composed-prompt"},
        "spec": {
            "interface": {
                "inputs": {"message": TEXT_SCHEMA},
                "outputs": {"prompt": TEXT_SCHEMA},
            },
            "nodes": {
                "answer__render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Composed {message.text}"},
                }
            },
            "edges": [
                {"from": "$input.message", "to": "answer__render.message"},
                {"from": "answer__render.prompt", "to": "$output.prompt"},
            ],
        },
    }


def _write_yaml(path: Path, *documents: dict[str, object]) -> None:
    path.write_text(
        yaml.safe_dump_all(documents, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _expanded_graph(path: Path) -> tuple[object, dict[str, object]]:
    result = compose_documents(path)
    graphs = [document for document in result.documents if document.get("kind") == "Graph"]
    assert len(graphs) == 1
    return result, graphs[0]


def test_composition_expands_fragment_to_same_graph_hash_as_monolith(tmp_path: Path) -> None:
    fragment_path = tmp_path / "fragment.yaml"
    graph_path = tmp_path / "graph.yaml"
    fragment = _fragment()
    source_graph = _composed_graph()
    _write_yaml(fragment_path, fragment)
    _write_yaml(graph_path, source_graph)

    result, expanded = _expanded_graph(graph_path)

    assert "composition" not in expanded["spec"]
    assert expanded["spec"]["nodes"] == _monolithic_graph()["spec"]["nodes"]
    assert canonical_hash(normalize_graph(expanded)) == canonical_hash(
        normalize_graph(_monolithic_graph())
    )
    assert source_graph["spec"]["nodes"]["answer"]["slot"] == "answer-slot"
    assert fragment["spec"]["nodes"]["render"]["block"] == "prompt.render@1"
    assert len(result.report.instances) == 1


def test_compiler_rejects_unexpanded_composition_and_slot() -> None:
    plan = compile_graph(_composed_graph())

    assert [
        diagnostic.code
        for diagnostic in plan.diagnostics.diagnostics
        if diagnostic.severity == "error"
    ] == ["UnexpandedComposition", "UnexpandedComposition"]


def test_composition_rewrites_placeholder_shorthand_at_fragment_boundary(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    _, expanded = _expanded_graph(graph_path)
    normalized = normalize_graph(expanded)

    assert normalized["spec"]["edges"] == [
        {"from": "$input.message", "to": "answer__render.message"},
        {"from": "answer__render.prompt", "to": "$output.prompt"},
    ]
    assert "answer" not in normalized["spec"]["nodes"]
    assert "answer__render" in normalized["spec"]["nodes"]


def test_composition_connects_one_slot_instance_to_another(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph = _composed_graph()
    graph["spec"]["nodes"] = {
        "first": {
            "slot": "answer-slot",
            "inputs": {"message": "$input.message"},
        },
        "second": {
            "slot": "answer-slot",
            "inputs": {"message": "first.prompt"},
            "outputs": {"prompt": "$output.prompt"},
        },
    }
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, graph)

    _, expanded = _expanded_graph(graph_path)

    assert normalize_graph(expanded)["spec"]["edges"] == [
        {"from": "$input.message", "to": "first__render.message"},
        {"from": "first__render.prompt", "to": "second__render.message"},
        {"from": "second__render.prompt", "to": "$output.prompt"},
    ]


def test_composition_rewrites_a_slot_output_to_its_own_input(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph = _composed_graph()
    graph["spec"]["nodes"] = {"answer": {"slot": "answer-slot"}}
    graph["spec"]["edges"] = [
        {"from": "answer.prompt", "to": "answer.message"},
    ]
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, graph)

    _, expanded = _expanded_graph(graph_path)

    assert normalize_graph(expanded)["spec"]["edges"] == [
        {"from": "answer__render.prompt", "to": "answer__render.message"},
    ]


def test_composition_keeps_imported_binding_and_removes_fragment_document(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment(), _binding())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    result = compose_documents(graph_path)

    resource_ids = {
        (document["kind"], document["metadata"]["name"])
        for document in result.documents
    }
    assert resource_ids == {
        ("Graph", "composed-prompt"),
        ("Binding", "local-model"),
    }
    assert all(document["kind"] != "GraphFragment" for document in result.documents)


def test_composition_rejects_graph_fragment_in_entry_stream(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _monolithic_graph(), _fragment())

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert captured.value.code == "CompositionUnsupportedKind"


def test_composition_rejects_fragment_only_entry(tmp_path: Path) -> None:
    fragment_path = tmp_path / "fragment.yaml"
    _write_yaml(fragment_path, _fragment())

    with pytest.raises(CompositionError) as captured:
        compose_documents(fragment_path)

    assert captured.value.code == "CompositionUnsupportedKind"


def test_composition_output_is_deterministic_across_mapping_order(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    _write_yaml(tmp_path / "bindings.yaml", _binding())
    graph_path = tmp_path / "graph.yaml"
    first_graph = _composed_graph(
        imports={
            "prompt": {"path": "fragment.yaml"},
            "bindings": {"path": "bindings.yaml"},
        }
    )
    _write_yaml(graph_path, first_graph)
    first = compose_documents(graph_path)

    second_graph = deepcopy(first_graph)
    composition = second_graph["spec"].pop("composition")
    second_graph["spec"] = {
        "composition": {
            "slots": composition["slots"],
            "imports": {
                "bindings": {"path": "bindings.yaml"},
                "prompt": {"path": "fragment.yaml"},
            },
            "apiVersion": composition["apiVersion"],
        },
        **second_graph["spec"],
    }
    _write_yaml(graph_path, second_graph)
    second = compose_documents(graph_path)

    assert first.documents == second.documents


def test_composition_report_is_deterministic_for_identical_sources(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    first = compose_documents(graph_path).report
    second = compose_documents(graph_path).report

    assert first.composition_digest == second.composition_digest
    assert first.sources == second.sources
    assert first.instances == second.instances


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "https://example.com/fragment.yaml",
        "http://example.com/fragment.yaml",
        "file:fragment.yaml",
    ],
    ids=["https-url", "http-url", "file-uri"],
)
def test_composition_rejects_url_imports(tmp_path: Path, unsafe_path: str) -> None:
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph(fragment_path=unsafe_path))

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionInvalidImport"


def test_composition_rejects_absolute_import_path(tmp_path: Path) -> None:
    fragment_path = tmp_path / "fragment.yaml"
    _write_yaml(fragment_path, _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph(fragment_path=str(fragment_path.resolve())))

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionInvalidImport"


def test_composition_rejects_parent_directory_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_yaml(tmp_path / "outside.yaml", _fragment())
    graph_path = project / "graph.yaml"
    _write_yaml(graph_path, _composed_graph(fragment_path="../outside.yaml"))

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionInvalidImport"


def test_composition_rejects_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_yaml(tmp_path / "outside.yaml", _fragment())
    (project / "fragment.yaml").symlink_to(tmp_path / "outside.yaml")
    graph_path = project / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionSymlinkRejected"


def test_composition_rejects_symlink_in_root_ancestor(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    project = real_root / "project"
    project.mkdir(parents=True)
    _write_yaml(project / "fragment.yaml", _fragment())
    _write_yaml(project / "graph.yaml", _composed_graph())
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(CompositionError) as captured:
        compose_documents(
            linked_root / "project" / "graph.yaml",
            root=linked_root / "project",
        )

    assert captured.value.code == "CompositionSymlinkRejected"


def test_composition_rejects_import_cycle(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph(fragment_path="graph.yaml"))

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionImportCycle"


@pytest.mark.parametrize(
    ("yaml_text", "expected_code"),
    [
        (
            "apiVersion: graphblocks.ai/v1alpha3\n"
            "kind: Graph\nmetadata: {name: duplicate}\n"
            "spec: {nodes: {}}\nspec: {nodes: {}}\n",
            "CompositionDuplicateKey",
        ),
        ("value: !include fragment.yaml\n", "CompositionInvalidYaml"),
        ("value: 2026-07-13T00:00:00Z\n", "CompositionInvalidYaml"),
        ("value: .nan\n", "CompositionInvalidYaml"),
        ("value: &recursive [*recursive]\n", "CompositionInvalidYaml"),
    ],
    ids=["duplicate-key", "unknown-tag", "timestamp", "nan", "recursive-alias"],
)
def test_composition_rejects_noncanonical_or_unsafe_yaml(
    tmp_path: Path,
    yaml_text: str,
    expected_code: str,
) -> None:
    graph_path = tmp_path / "graph.yaml"
    graph_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert captured.value.code == expected_code


def test_composition_rejects_yaml_beyond_the_depth_limit(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.yaml"
    graph_path.write_text("value: " + ("[" * 160) + "0" + ("]" * 160), encoding="utf-8")

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert captured.value.code == "CompositionLimitExceeded"


def test_composition_rejects_unused_malformed_fragment(tmp_path: Path) -> None:
    malformed = _fragment()
    malformed["spec"]["edges"] = [{"from": "render.prompt"}]
    _write_yaml(tmp_path / "fragment.yaml", malformed)
    graph = _monolithic_graph()
    graph["spec"]["composition"] = {
        "apiVersion": COMPOSITION_API_VERSION,
        "imports": {"unused": {"path": "fragment.yaml"}},
        "slots": {},
    }
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, graph)

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert captured.value.code == "CompositionInvalidWiring"


def test_composition_rejects_malformed_imported_binding(tmp_path: Path) -> None:
    binding = _binding()
    binding["metadata"] = {}
    _write_yaml(tmp_path / "bindings.yaml", binding)
    graph = _monolithic_graph()
    graph["spec"]["composition"] = {
        "apiVersion": COMPOSITION_API_VERSION,
        "imports": {"bindings": {"path": "bindings.yaml"}},
        "slots": {},
    }
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, graph)

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)

    assert captured.value.code == "CompositionUnsupportedKind"


@pytest.mark.parametrize(
    ("slot_input_schema", "slot_output_schema"),
    [
        ("graphblocks.ai/Query@1", TEXT_SCHEMA),
        (TEXT_SCHEMA, "graphblocks.ai/RenderedPrompt@1"),
    ],
    ids=["input", "output"],
)
def test_composition_rejects_slot_fragment_interface_mismatch(
    tmp_path: Path,
    slot_input_schema: str,
    slot_output_schema: str,
) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(
        graph_path,
        _composed_graph(
            slot_input_schema=slot_input_schema,
            slot_output_schema=slot_output_schema,
        ),
    )

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionInterfaceMismatch"


def test_composition_rejects_synthesized_node_collision(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(
        graph_path,
        _composed_graph(
            extra_nodes={"answer__render": {"block": "text.literal@1"}},
        ),
    )

    with pytest.raises(CompositionError) as captured:
        compose_documents(graph_path)
    assert captured.value.code == "CompositionNodeCollision"


def test_cli_validate_plan_and_run_use_expanded_graph(tmp_path: Path, capsys) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    assert main(["validate", str(graph_path)]) == 0
    assert capsys.readouterr().out.strip() == "OK"

    assert main(["plan", str(graph_path), "--expand"]) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["ok"] is True
    assert "composition" not in plan_payload["graph"]["spec"]
    assert plan_payload["hash"] == canonical_hash(normalize_graph(_monolithic_graph()))

    assert main(
        [
            "run",
            str(graph_path),
            "--input-json",
            '{"message":{"text":"hello"}}',
        ]
    ) == 0
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["status"] == "succeeded"
    assert run_payload["outputs"] == {"prompt": "Composed hello"}


def test_cli_validate_reports_composition_errors_as_json(tmp_path: Path, capsys) -> None:
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(
        graph_path,
        _composed_graph(fragment_path="https://example.com/fragment.yaml"),
    )

    assert main(["validate", str(graph_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["diagnostics"][0]["code"] == "CompositionInvalidImport"
    assert payload["diagnostics"][0]["source"] == "graph.yaml"


def test_cli_compose_materializes_a_standalone_yaml_stream(tmp_path: Path, capsys) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment(), _binding())
    graph_path = tmp_path / "graph.yaml"
    output_path = tmp_path / "expanded.yaml"
    _write_yaml(graph_path, _composed_graph())

    assert main(["compose", str(graph_path), "--output", str(output_path)]) == 0
    capsys.readouterr()

    documents = list(yaml.safe_load_all(output_path.read_text(encoding="utf-8")))
    graph = next(document for document in documents if document["kind"] == "Graph")
    assert "composition" not in graph["spec"]
    assert "answer__render" in graph["spec"]["nodes"]
    assert all(document["kind"] != "GraphFragment" for document in documents)
    assert main(["validate", str(output_path)]) == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_cli_composition_root_controls_trust_and_report_paths(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    graphs = project / "graphs"
    fragments = graphs / "fragments"
    fragments.mkdir(parents=True)
    _write_yaml(fragments / "fragment.yaml", _fragment())
    graph_path = graphs / "graph.yaml"
    _write_yaml(graph_path, _composed_graph(fragment_path="fragments/fragment.yaml"))
    report_path = tmp_path / "report.json"

    assert (
        main(
            [
                "compose",
                str(graph_path),
                "--root",
                str(project),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [source["path"] for source in report["sources"]] == [
        "graphs/fragments/fragment.yaml",
        "graphs/graph.yaml",
    ]
    assert all(not Path(source["path"]).is_absolute() for source in report["sources"])
    assert (
        main(
            [
                "validate",
                str(graph_path),
                "--composition-root",
                str(project),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "OK"


def test_cli_compose_reports_output_write_failure(tmp_path: Path, capsys) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    assert (
        main(
            [
                "compose",
                str(graph_path),
                "--output",
                str(tmp_path / "missing" / "expanded.yaml"),
            ]
        )
        == 1
    )
    assert "CompositionOutputError" in capsys.readouterr().out


def test_cli_lock_uses_the_expanded_graph_hash(tmp_path: Path, capsys) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())

    assert main(["lock", str(graph_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["graph"]["graphHash"] == canonical_hash(
        normalize_graph(_monolithic_graph())
    )


def test_cli_native_runtime_receives_only_the_expanded_graph(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    _write_yaml(tmp_path / "fragment.yaml", _fragment())
    graph_path = tmp_path / "graph.yaml"
    _write_yaml(graph_path, _composed_graph())
    received_graphs: list[dict[str, object]] = []

    def run_stdlib_graph_json(graph_json: str, _inputs_json: str) -> str:
        received_graphs.append(json.loads(graph_json))
        return json.dumps(
            {
                "runId": "composed-native",
                "status": "succeeded",
                "outputs": {"prompt": "Composed hello"},
                "journal": [{"kind": "run_succeeded"}],
            }
        )

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            native_extension_available=lambda: True,
            run_stdlib_graph_json=run_stdlib_graph_json,
        ),
    )

    assert main(["run", str(graph_path), "--runtime", "native"]) == 0
    capsys.readouterr()

    assert len(received_graphs) == 1
    assert "composition" not in received_graphs[0]["spec"]
    assert "answer__render" in received_graphs[0]["spec"]["nodes"]
