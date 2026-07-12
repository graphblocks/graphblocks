from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Callable

import yaml

from graphblocks.canonical import canonical_dumps, canonical_hash
from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog, load_plugin_manifest
from graphblocks.runtime import InProcessRuntime, RuntimeRegistry
from graphblocks.schema import SchemaManifest
from graphblocks.worker import (
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerProtocolMessage,
    validate_worker_result,
)


ROOT = Path(__file__).resolve().parents[1]

ACCEPTANCE_MOCK_BOUNDARIES = {
    "enterprise-rag": ("retrieval adapters", "citation validator", "abstention policy"),
    "document-ingestion": ("document parsers", "ACL-aware index"),
    "bounded-research-orchestrator": ("worker pool", "budget ledger", "task leases"),
    "verified-rtl-workspace-trial": ("EDA lease pool", "reviewer", "workspace store"),
    "kubernetes-canary": ("release signer", "canary metrics", "deployment target"),
    "telemetry-outage-correctness": ("OTel exporter", "Langfuse exporter", "telemetry outbox"),
    "realtime-voice-agent": ("realtime provider", "playback transport"),
    "coding-agent-background-callbacks": ("CI callback", "secret resolver", "webhook transport"),
}


class NetworkAccessBlocked:
    def __call__(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("example integration attempted real network access")


@dataclass(slots=True)
class FixtureBlock:
    block_id: str
    node_specs: Mapping[str, Mapping[str, object]]
    calls: list[dict[str, object]]

    def __call__(
        self,
        inputs: dict[str, Any],
        _config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        node_name = str(context["node"])
        if node_name not in self.node_specs:
            raise RuntimeError(f"mock fixture is missing node {node_name!r}")
        node_spec = self.node_specs[node_name]
        expected_inputs = node_spec.get("expectedInputs", {})
        if not isinstance(expected_inputs, Mapping):
            raise TypeError(f"mock fixture node {node_name!r} expectedInputs must be a mapping")

        comparisons: list[tuple[str, object, object]] = [("$", expected_inputs, inputs)]
        while comparisons:
            path, expected, actual = comparisons.pop()
            if isinstance(expected, Mapping):
                if not isinstance(actual, Mapping):
                    raise AssertionError(f"{node_name} input {path} must be a mapping")
                for key, value in expected.items():
                    if key not in actual:
                        raise AssertionError(f"{node_name} input {path}.{key} is missing")
                    comparisons.append((f"{path}.{key}", value, actual[key]))
            elif expected != actual:
                raise AssertionError(
                    f"{node_name} input {path} mismatch: expected {expected!r}, got {actual!r}"
                )

        outputs = node_spec.get("outputs", {})
        if not isinstance(outputs, Mapping):
            raise TypeError(f"mock fixture node {node_name!r} outputs must be a mapping")
        result = deepcopy(dict(outputs))
        call: dict[str, object] = {
            "node": node_name,
            "block": self.block_id,
            "service": str(node_spec.get("service", "in-process")),
            "inputDigest": canonical_hash(inputs),
        }

        if "scriptedResponse" in node_spec:
            from graphblocks.integrations.scripted import ScriptedModelProvider

            response_text = node_spec["scriptedResponse"]
            response_field = node_spec.get("responseField")
            if not isinstance(response_text, str) or not isinstance(response_field, str):
                raise TypeError(
                    f"mock fixture node {node_name!r} scripted response requires string responseField"
                )
            prompt = canonical_dumps(inputs)
            provider = ScriptedModelProvider(
                scripts={prompt: response_text},
                model="example-integration",
                provider_id=str(node_spec.get("service", "scripted-llm")),
            )
            response = provider.generate(
                prompt,
                response_id=f"{node_name}-response",
                metadata={"run_id": context["run_id"], "node": node_name},
            )
            result[response_field] = response.text
            call["providerResponseId"] = response.response_id
            call["usage"] = dict(response.usage)

        self.calls.append(call)
        return result


@dataclass(slots=True)
class PythonWorkerTransport:
    invoke: Callable[[WorkerInvokeRequest], WorkerInvokeResult]

    def __call__(self, message: WorkerProtocolMessage) -> WorkerProtocolMessage:
        request = message.payload
        if not isinstance(request, WorkerInvokeRequest):
            raise TypeError("Python worker transport requires an invoke_request message")
        result = self.invoke(request)
        if not isinstance(result, WorkerInvokeResult):
            raise TypeError("Python worker callable must return WorkerInvokeResult")
        return WorkerProtocolMessage.invoke_result(
            f"{message.message_id}-result",
            message.sequence + 1,
            result,
            causation_id=message.message_id,
        )


@dataclass(slots=True)
class RustWorkerTransport:
    executable: Path

    def __call__(self, message: WorkerProtocolMessage) -> WorkerProtocolMessage:
        result = subprocess.run(
            [str(self.executable)],
            input=json.dumps(message.to_wire(), sort_keys=True),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Rust worker failed with "
                f"exit code {result.returncode}: {result.stderr.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Rust worker did not return one JSON protocol message") from error
        if not isinstance(payload, dict):
            raise TypeError("Rust worker protocol response must be a mapping")
        return WorkerProtocolMessage.from_wire(payload)


@dataclass(slots=True)
class WorkerBlockAdapter:
    block_id: str
    implementation: str
    service: str
    transport: Callable[[WorkerProtocolMessage], WorkerProtocolMessage]
    calls: list[dict[str, object]]

    def __call__(
        self,
        inputs: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        node_name = str(context["node"])
        attempt = int(context["attempt"])
        invocation_id = f"{context['run_id']}:{node_name}:{attempt}"
        request = WorkerInvokeRequest(
            invocation_id=invocation_id,
            run_id=str(context["run_id"]),
            node_id=node_name,
            node_attempt_id=f"{node_name}-attempt-{attempt}",
            lease_epoch=1,
            block=self.block_id,
            context=WorkerInvocationContext(
                "example-release",
                "example-revision",
                attributes={"implementation": self.implementation},
            ),
            inputs=deepcopy(inputs),
            config=deepcopy(config),
        )
        request_message = WorkerProtocolMessage.invoke_request(
            f"{invocation_id}-request",
            1,
            request,
        )
        result_message = self.transport(request_message)
        result = result_message.payload
        if not isinstance(result, WorkerInvokeResult):
            raise TypeError("worker transport must return an invoke_result message")
        if result_message.correlation_id != invocation_id:
            raise AssertionError("worker result correlation id does not match invocation id")
        if result_message.causation_id != request_message.message_id:
            raise AssertionError("worker result causation id does not match request message id")
        validate_worker_result(request, result)
        self.calls.append(
            {
                "node": node_name,
                "block": self.block_id,
                "service": self.service,
                "implementation": self.implementation,
                "inputDigest": canonical_hash(inputs),
                "requestDigest": request_message.content_digest(),
                "resultDigest": result_message.content_digest(),
            }
        )
        return deepcopy(result.outputs)


def run_integration(example_path: Path) -> dict[str, object]:
    example_path = example_path.resolve()
    integration_path = example_path.with_name("integration.yaml")
    integration = yaml.safe_load(integration_path.read_text(encoding="utf-8"))
    if not isinstance(integration, Mapping) or integration.get("kind") != "ExampleIntegration":
        raise ValueError(f"{integration_path} must contain an ExampleIntegration resource")
    spec = integration.get("spec")
    if not isinstance(spec, Mapping):
        raise ValueError(f"{integration_path} spec must be a mapping")

    for package_src in sorted((ROOT / "packages").glob("*/src")):
        package_path = str(package_src)
        if package_path not in sys.path:
            sys.path.insert(0, package_path)

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    blocker = NetworkAccessBlocked()
    socket.socket.connect = blocker  # type: ignore[method-assign]
    socket.socket.connect_ex = blocker  # type: ignore[method-assign]
    socket.create_connection = blocker  # type: ignore[assignment]
    checks: list[str] = []
    mocked_boundaries: set[str] = set()
    mock_calls: list[dict[str, object]] = []
    executed_blocks: set[str] = set()
    worker_calls: list[dict[str, object]] = []

    try:
        example_documents = list(yaml.safe_load_all(example_path.read_text(encoding="utf-8")))
        embedded_graphs = {
            str(document["metadata"]["name"])
            for document in example_documents
            if isinstance(document, Mapping)
            and document.get("kind") == "Graph"
            and isinstance(document.get("metadata"), Mapping)
        }
        for document in example_documents:
            if not isinstance(document, Mapping):
                continue
            document_spec = document.get("spec")
            if not isinstance(document_spec, Mapping):
                continue
            graph_references: list[str] = []
            if document.get("kind") == "Application":
                application_graphs = document_spec.get("graphs", {})
                if isinstance(application_graphs, Mapping):
                    graph_references.extend(str(value) for value in application_graphs.values())
                routes = document_spec.get("routes", ())
                if isinstance(routes, list):
                    graph_references.extend(
                        str(route["graph"])
                        for route in routes
                        if isinstance(route, Mapping) and "graph" in route
                    )
            nodes = document_spec.get("nodes", {})
            if isinstance(nodes, Mapping):
                for node in nodes.values():
                    if not isinstance(node, Mapping):
                        continue
                    config = node.get("config")
                    if isinstance(config, Mapping) and "graph" in config:
                        graph_references.append(str(config["graph"]))
                    if isinstance(config, Mapping):
                        callback = config.get("callback")
                        if isinstance(callback, Mapping) and isinstance(callback.get("schema"), str):
                            schema_path = example_path.parent / callback["schema"]
                            if not schema_path.is_file() and schema_path.suffix == "":
                                schema_path = schema_path.with_suffix(".yaml")
                            if not schema_path.is_file():
                                raise AssertionError(f"example callback schema is missing: {schema_path}")
                            callback_schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
                            if (
                                not isinstance(callback_schema, Mapping)
                                or callback_schema.get("$id") != callback["schema"]
                            ):
                                raise AssertionError(
                                    f"example callback schema identity does not match {callback['schema']}"
                                )
            for graph_reference in graph_references:
                if graph_reference not in embedded_graphs:
                    raise AssertionError(
                        f"example graph reference {graph_reference!r} does not name an embedded Graph"
                    )
            if document.get("kind") == "GraphDeployment":
                targets = document_spec.get("targets", {})
                if isinstance(targets, Mapping):
                    for target_name, target in targets.items():
                        if not isinstance(target, Mapping) or "packageLock" not in target:
                            continue
                        lock_path = example_path.parent / str(target["packageLock"])
                        if not lock_path.is_file():
                            raise AssertionError(f"example package lock is missing: {lock_path}")
                        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
                        if lock.get("target") != target_name or not lock.get("packages"):
                            raise AssertionError(
                                f"example package lock does not bind target {target_name!r}: {lock_path}"
                            )
        checks.append("references:resolved")

        acceptance_application = spec.get("acceptanceApplication")
        if acceptance_application is not None:
            if not isinstance(acceptance_application, str):
                raise TypeError("integration acceptanceApplication must be a string")
            from graphblocks_testing import AcceptanceGateRunner, AcceptanceManifest

            manifest = AcceptanceManifest.from_document(
                yaml.safe_load((ROOT / "acceptance" / "applications.yaml").read_text(encoding="utf-8"))
            )
            application = manifest.by_id(acceptance_application)
            expected_scenario_path = example_path.relative_to(ROOT).as_posix()
            if application.scenario_path != expected_scenario_path:
                raise AssertionError(
                    f"acceptance application {acceptance_application!r} does not use {expected_scenario_path}"
                )
            acceptance_report = AcceptanceGateRunner().run_application(application, root=ROOT)
            if not acceptance_report.ok:
                failures = [
                    f"{result.gate}: {', '.join(item.message for item in result.diagnostics)}"
                    for result in acceptance_report.results
                    if result.status != "passed"
                ]
                raise AssertionError("; ".join(failures))
            semantic_results = [
                result
                for result in acceptance_report.results
                if not result.gate.startswith("graphblocks ")
            ]
            if not semantic_results:
                raise AssertionError("example acceptance integration requires a semantic gate")
            checks.extend(f"acceptance:{result.gate}" for result in acceptance_report.results)
            mocked_boundaries.update(ACCEPTANCE_MOCK_BOUNDARIES[acceptance_application])

        mock_graph = spec.get("mockGraph")
        if mock_graph is not None:
            if not isinstance(mock_graph, Mapping):
                raise TypeError("integration mockGraph must be a mapping")
            graph_name = mock_graph.get("graph")
            graphs = [
                document
                for document in example_documents
                if isinstance(document, Mapping)
                and document.get("kind") == "Graph"
                and isinstance(document.get("metadata"), Mapping)
                and document["metadata"].get("name") == graph_name
            ]
            if len(graphs) != 1:
                raise AssertionError(f"mock graph {graph_name!r} must identify exactly one Graph")
            graph = deepcopy(dict(graphs[0]))
            graph_nodes = graph.get("spec", {}).get("nodes", {})
            fixture_nodes = mock_graph.get("nodes")
            if not isinstance(graph_nodes, Mapping) or not isinstance(fixture_nodes, Mapping):
                raise TypeError("mock graph nodes must be mappings")
            if set(graph_nodes) != set(fixture_nodes):
                raise AssertionError(
                    f"mock graph fixtures must cover every node; expected {sorted(graph_nodes)}, "
                    f"got {sorted(fixture_nodes)}"
                )

            registry = RuntimeRegistry()
            block_ids = {str(node["block"]) for node in graph_nodes.values()}
            for block_id in block_ids:
                registry.register(block_id, FixtureBlock(block_id, fixture_nodes, mock_calls))
            inputs = mock_graph.get("inputs", {})
            if not isinstance(inputs, Mapping):
                raise TypeError("mock graph inputs must be a mapping")
            runtime_result = InProcessRuntime(registry).run(
                graph,
                deepcopy(dict(inputs)),
                run_id=f"example-{example_path.parent.name}",
            )
            if runtime_result.status != "succeeded":
                raise AssertionError(
                    f"mock graph failed: {runtime_result.journal.records[-1].payload}"
                )
            expected_order = mock_graph.get("expectedCallOrder")
            actual_order = [str(call["node"]) for call in mock_calls]
            if actual_order != expected_order:
                raise AssertionError(
                    f"mock call order mismatch: expected {expected_order!r}, got {actual_order!r}"
                )
            expected_output = mock_graph.get("expectedOutput")
            if runtime_result.outputs != expected_output:
                raise AssertionError(
                    f"mock graph output mismatch: expected {expected_output!r}, "
                    f"got {runtime_result.outputs!r}"
                )
            if runtime_result.journal.terminal_kind != "run_succeeded":
                raise AssertionError("mock graph journal must end in run_succeeded")
            checks.extend(("mock-graph:resolved-inputs", "mock-graph:final-output", "mock-graph:journal"))
            mocked_boundaries.update(str(call["service"]) for call in mock_calls)

        worker_graph = spec.get("workerGraph")
        if worker_graph is not None:
            if not isinstance(worker_graph, Mapping):
                raise TypeError("integration workerGraph must be a mapping")
            graph_name = worker_graph.get("graph")
            graphs = [
                document
                for document in example_documents
                if isinstance(document, Mapping)
                and document.get("kind") == "Graph"
                and isinstance(document.get("metadata"), Mapping)
                and document["metadata"].get("name") == graph_name
            ]
            if len(graphs) != 1:
                raise AssertionError(f"worker graph {graph_name!r} must identify exactly one Graph")
            graph = deepcopy(dict(graphs[0]))
            graph_nodes = graph.get("spec", {}).get("nodes", {})
            worker_nodes = worker_graph.get("nodes")
            if not isinstance(graph_nodes, Mapping) or not isinstance(worker_nodes, Mapping):
                raise TypeError("worker graph nodes must be mappings")
            if set(graph_nodes) != set(worker_nodes):
                raise AssertionError(
                    f"worker graph implementations must cover every node; expected {sorted(graph_nodes)}, "
                    f"got {sorted(worker_nodes)}"
                )

            raw_plugin_path = worker_graph.get("pluginManifest")
            if not isinstance(raw_plugin_path, str):
                raise TypeError("worker graph pluginManifest must be a relative path")
            plugin_path = (example_path.parent / raw_plugin_path).resolve()
            try:
                plugin_path.relative_to(example_path.parent)
            except ValueError as error:
                raise ValueError("worker graph pluginManifest escapes the example directory") from error
            plugin_manifest = load_plugin_manifest(plugin_path)
            block_catalog = BlockCatalog.from_manifests([plugin_manifest])
            plan = compile_graph(graph, block_catalog=block_catalog)
            if not plan.ok:
                errors = [
                    f"{item.code} {item.path}: {item.message}"
                    for item in plan.diagnostics.diagnostics
                    if item.severity == "error"
                ]
                raise AssertionError("worker graph did not compile with its plugin: " + "; ".join(errors))
            checks.append("plugin:validated")

            raw_schema_path = worker_graph.get("schemaDirectory")
            if not isinstance(raw_schema_path, str):
                raise TypeError("worker graph schemaDirectory must be a relative path")
            schema_path = (example_path.parent / raw_schema_path).resolve()
            try:
                schema_path.relative_to(example_path.parent)
            except ValueError as error:
                raise ValueError("worker graph schemaDirectory escapes the example directory") from error
            schema_manifest = SchemaManifest.from_directory(schema_path)
            schema_ids = {entry.schema_id for entry in schema_manifest.entries}
            referenced_schema_ids = {
                str(port["type"])
                for block in plugin_manifest.blocks
                for direction in ("inputs", "outputs")
                for port in block.get(direction, [])
                if isinstance(port, Mapping)
                and isinstance(port.get("type"), str)
                and str(port["type"]).startswith("examples.graphblocks.ai/")
            }
            missing_schema_ids = referenced_schema_ids - schema_ids
            if missing_schema_ids:
                raise AssertionError(
                    f"worker graph schemas are missing {sorted(missing_schema_ids)}"
                )
            checks.append("schemas:manifest")

            manifest_implementations: dict[str, str] = {}
            for block in plugin_manifest.blocks:
                block_type = block.get("typeId") or block.get("type_id") or block.get("block")
                version = block.get("version")
                if isinstance(block_type, str) and "@" in block_type and version is None:
                    block_type, version = block_type.rsplit("@", 1)
                implementation = block.get("implementation") or block.get("implementationId")
                if isinstance(block_type, str) and version is not None and isinstance(implementation, str):
                    manifest_implementations[f"{block_type}@{version}"] = implementation

            registry = RuntimeRegistry()
            for node_name, raw_node in graph_nodes.items():
                implementation_spec = worker_nodes[node_name]
                if not isinstance(raw_node, Mapping) or not isinstance(implementation_spec, Mapping):
                    raise TypeError("worker graph node implementations must be mappings")
                block_id = str(raw_node.get("block"))
                implementation = implementation_spec.get("implementation")
                if manifest_implementations.get(block_id) != implementation:
                    raise AssertionError(
                        f"worker node {node_name!r} implementation does not match the plugin manifest"
                    )
                service = implementation_spec.get("service")
                if not isinstance(implementation, str) or not isinstance(service, str):
                    raise TypeError("worker implementation and service must be strings")

                kind = implementation_spec.get("kind")
                if kind == "python":
                    raw_module_path = implementation_spec.get("modulePath")
                    callable_name = implementation_spec.get("callable")
                    if not isinstance(raw_module_path, str) or not isinstance(callable_name, str):
                        raise TypeError("Python worker requires modulePath and callable strings")
                    module_path = (example_path.parent / raw_module_path).resolve()
                    try:
                        module_path.relative_to(example_path.parent)
                    except ValueError as error:
                        raise ValueError("Python worker module escapes the example directory") from error
                    module_spec = importlib.util.spec_from_file_location(
                        f"graphblocks_example_{example_path.parent.name}_{node_name}",
                        module_path,
                    )
                    if module_spec is None or module_spec.loader is None:
                        raise RuntimeError(f"cannot load Python worker module {module_path}")
                    module = importlib.util.module_from_spec(module_spec)
                    module_spec.loader.exec_module(module)
                    invoke = getattr(module, callable_name, None)
                    if not callable(invoke):
                        raise TypeError(f"Python worker callable {callable_name!r} was not found")
                    transport: Callable[[WorkerProtocolMessage], WorkerProtocolMessage] = (
                        PythonWorkerTransport(invoke)
                    )
                    checks.append("worker:python")
                elif kind == "rust":
                    raw_manifest_path = implementation_spec.get("manifestPath")
                    binary_name = implementation_spec.get("binary")
                    if not isinstance(raw_manifest_path, str) or not isinstance(binary_name, str):
                        raise TypeError("Rust worker requires manifestPath and binary strings")
                    rust_manifest_path = (example_path.parent / raw_manifest_path).resolve()
                    try:
                        rust_manifest_path.relative_to(example_path.parent)
                    except ValueError as error:
                        raise ValueError("Rust worker manifest escapes the example directory") from error
                    configured_target_dir = os.environ.get("GRAPHBLOCKS_EXAMPLE_RUST_TARGET_DIR")
                    target_dir = (
                        Path(configured_target_dir).resolve()
                        if configured_target_dir
                        else Path(tempfile.gettempdir()) / "graphblocks-example-rust-target"
                    )
                    build = subprocess.run(
                        [
                            "cargo",
                            "build",
                            "--quiet",
                            "--locked",
                            "--manifest-path",
                            str(rust_manifest_path),
                            "--target-dir",
                            str(target_dir),
                        ],
                        cwd=example_path.parent,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if build.returncode != 0:
                        raise RuntimeError(
                            "Rust worker build failed with "
                            f"exit code {build.returncode}: {build.stderr.strip()}"
                        )
                    executable = target_dir / "debug" / (
                        f"{binary_name}.exe" if os.name == "nt" else binary_name
                    )
                    if not executable.is_file():
                        raise AssertionError(f"Rust worker binary is missing: {executable}")
                    transport = RustWorkerTransport(executable)
                    checks.append("worker:rust")
                else:
                    raise ValueError(f"unsupported worker kind {kind!r}")

                registry.register(
                    block_id,
                    WorkerBlockAdapter(
                        block_id,
                        implementation,
                        service,
                        transport,
                        worker_calls,
                    ),
                )
                executed_blocks.add(block_id)

            inputs = worker_graph.get("inputs", {})
            if not isinstance(inputs, Mapping):
                raise TypeError("worker graph inputs must be a mapping")
            runtime_result = InProcessRuntime(registry).run(
                graph,
                deepcopy(dict(inputs)),
                run_id=f"example-{example_path.parent.name}",
            )
            if runtime_result.status != "succeeded":
                raise AssertionError(
                    f"worker graph failed: {runtime_result.journal.records[-1].payload}"
                )
            expected_order = worker_graph.get("expectedCallOrder")
            actual_order = [str(call["node"]) for call in worker_calls]
            if actual_order != expected_order:
                raise AssertionError(
                    f"worker call order mismatch: expected {expected_order!r}, got {actual_order!r}"
                )
            expected_output = worker_graph.get("expectedOutput")
            if runtime_result.outputs != expected_output:
                raise AssertionError(
                    f"worker graph output mismatch: expected {expected_output!r}, "
                    f"got {runtime_result.outputs!r}"
                )
            if runtime_result.journal.terminal_kind != "run_succeeded":
                raise AssertionError("worker graph journal must end in run_succeeded")
            checks.extend(
                (
                    "worker:result-fences",
                    "worker-graph:resolved-inputs",
                    "worker-graph:final-output",
                    "worker-graph:journal",
                )
            )

        policy_profiles = spec.get("policyProfiles")
        if policy_profiles is not None:
            if not isinstance(policy_profiles, Mapping):
                raise TypeError("integration policyProfiles must be a mapping")
            from graphblocks.exhaustion import ContinuationEnvelope, ExhaustionController, ExhaustionPolicy
            from graphblocks.output_policy import OutputCutoff
            from graphblocks.integrations.scripted import ScriptedModelProvider

            profiles = {
                document["metadata"]["name"]: document["spec"]
                for document in example_documents
                if isinstance(document, Mapping) and document.get("kind") == "PolicyProfile"
            }
            finish_spec = profiles[str(policy_profiles["finishCurrentTurn"])]
            hard_stop_spec = profiles[str(policy_profiles["hardStop"])]
            if (
                finish_spec["exhaustion"]["output"]["durableResult"]
                != "commit_with_exhaustion_notice"
            ):
                raise AssertionError("finish-current-turn must commit with an exhaustion notice")
            if hard_stop_spec["exhaustion"]["output"]["durableResult"] != "retract":
                raise AssertionError("hard-stop must retract the durable draft")
            provider = ScriptedModelProvider(
                scripts={"finish admitted turn": "A bounded final answer."},
                model="example-integration",
                provider_id="scripted-llm",
            )
            chunks = tuple(provider.stream("finish admitted turn", response_id="policy-response", chunk_size=8))
            if "".join(chunk.text_delta for chunk in chunks) != "A bounded final answer.":
                raise AssertionError("finish-current-turn mock model stream was incomplete")

            finish_policy = ExhaustionPolicy.from_preset(
                finish_spec["exhaustion"]["preset"],
                unit=finish_spec["exhaustion"]["unit"],
                continuation=ContinuationEnvelope(
                    max_additional_steps=finish_spec["exhaustion"]["continuation"]["maxAdditionalSteps"]
                ),
            )
            finish_controller = ExhaustionController(
                finish_policy,
                atomic_unit_id="turn-demo",
                admission_epoch=1,
            )
            if not finish_controller.admit("already_admitted_child_work", work_epoch=1).allowed:
                raise AssertionError("finish-current-turn must allow already admitted work")
            if finish_controller.admit("optional_task", work_epoch=2).allowed:
                raise AssertionError("finish-current-turn must reject optional new work")

            hard_stop_policy = ExhaustionPolicy.from_preset(
                hard_stop_spec["exhaustion"]["preset"],
                unit=hard_stop_spec["exhaustion"]["unit"],
            )
            hard_stop_controller = ExhaustionController(
                hard_stop_policy,
                atomic_unit_id="provider-call-demo",
                admission_epoch=1,
            )
            if not hard_stop_controller.admit("cleanup", work_epoch=1).allowed:
                raise AssertionError("hard-stop must retain cleanup authority")
            if hard_stop_controller.admit("current_provider_call", work_epoch=1).allowed:
                raise AssertionError("hard-stop must reject continued provider work")
            cutoff = OutputCutoff(
                stream_id="stream-demo",
                response_id="policy-response",
                last_generated_sequence=2,
                last_client_delivered_sequence=2,
                terminal_reason="budget_exhausted",
                draft_disposition="retract",
                durable_result="incomplete",
                occurred_at="2026-07-10T00:00:00Z",
            )
            if cutoff.accepts_sequence(3):
                raise AssertionError("hard-stop output cutoff admitted a late model chunk")
            checks.extend(
                (
                    "policy:scripted-model-stream",
                    "policy:finish-current-turn",
                    "policy:hard-stop",
                )
            )
            mocked_boundaries.update(("scripted-llm", "usage budget", "output stream"))

        if not checks:
            raise AssertionError("example integration did not execute any checks")
        evidence = {
            "example": example_path.parent.name,
            "checks": checks,
            "mockedBoundaries": sorted(mocked_boundaries),
            "mockCalls": mock_calls,
            "executedBlocks": sorted(executed_blocks),
            "workerCalls": worker_calls,
        }
        return {
            "ok": True,
            **evidence,
            "evidenceDigest": canonical_hash(evidence),
        }
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create_connection
