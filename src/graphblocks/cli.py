from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from dataclasses import asdict
from pathlib import Path

import yaml

from . import __version__
from .compiler import compile_graph
from .deployment import (
    GraphRelease,
    GraphReleaseGraph,
    GraphReleaseMutableReferencesError,
    ImageRef,
    KnowledgeBinding,
    PromptLock,
)
from .diagnostics import Diagnostic
from .loader import load_documents
from .migration import migrate_document
from .packages import build_package_lock, doctor_package_catalog, load_package_catalog, package_rows
from .policy import (
    PolicyBundle,
    PolicyObligation,
    PolicyRequest,
    PolicyRule,
    PolicyTestCase,
    PolicyTestExpectation,
    PrincipalRef,
    ResourceRef,
    StaticPolicyEvaluator,
    run_policy_tests,
)
from .plugins import BlockCatalog, discover_plugins, load_plugin_manifest, validate_plugin_manifest
from .runtime import InProcessRuntime, stdlib_registry
from .run_store import SQLiteRunStore

STRUCTURAL_KINDS = {
    "Application",
    "Binding",
    "ConformanceProfileSet",
    "GraphDeployment",
    "GraphRelease",
    "ObservabilityProfile",
    "PluginManifest",
    "PolicyProfile",
}


def _field(mapping: Mapping[str, object], *names: str, default: object = None) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _tuple_field(mapping: Mapping[str, object], *names: str) -> tuple[str, ...]:
    value = _field(mapping, *names, default=())
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _documents_from_path(path: Path) -> list[dict[str, object]]:
    if not path.is_dir():
        return load_documents(path)
    documents: list[dict[str, object]] = []
    for candidate in sorted([*path.glob("*.yaml"), *path.glob("*.yml")]):
        documents.extend(load_documents(candidate))
    return documents


def _resource_ref_from_mapping(mapping: Mapping[str, object]) -> ResourceRef:
    return ResourceRef(
        resource_id=str(_field(mapping, "resourceId", "resource_id", "id")),
        resource_kind=_field(mapping, "resourceKind", "resource_kind", "kind"),
        tenant_id=_field(mapping, "tenantId", "tenant_id"),
        attributes=dict(_field(mapping, "attributes", default={}) or {}),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphblocks")
    parser.add_argument("--version", action="store_true", help="show package version")
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate", help="validate GraphBlocks YAML documents")
    validate_parser.add_argument("path", type=Path)
    validate_parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    validate_parser.add_argument("--plugin-path", action="append", default=[], help="static plugin manifest file or directory")

    plan_parser = subparsers.add_parser("plan", help="compile a GraphSpec into normalized plan JSON")
    plan_parser.add_argument("path", type=Path)
    plan_parser.add_argument("--plugin-path", action="append", default=[], help="static plugin manifest file or directory")
    plan_parser.add_argument("--expand", action="store_true", help="include normalized graph")
    plan_parser.add_argument("--show-bindings", action="store_true", help="include Binding documents from the same file")
    plan_parser.add_argument("--show-packages", action="store_true", help="include inferred semantic block requirements")
    plan_parser.add_argument("--target", default="local-python", help="execution target label for diagnostics")

    run_parser = subparsers.add_parser("run", help="execute a GraphSpec with the deterministic in-process runtime")
    run_parser.add_argument("path", type=Path)
    run_parser.add_argument("--input-json", default="{}", help="JSON object used as graph input values")

    migrate_parser = subparsers.add_parser("migrate", help="read legacy alpha documents and emit current YAML")
    migrate_parser.add_argument("path", type=Path)

    plugins_parser = subparsers.add_parser("plugins", help="inspect static plugin manifests")
    plugins_subparsers = plugins_parser.add_subparsers(dest="plugins_command")
    plugins_list_parser = plugins_subparsers.add_parser("list", help="list discovered plugins")
    plugins_list_parser.add_argument("--path", action="append", default=[], help="manifest file or directory to scan")
    plugins_list_parser.add_argument("--no-installed", action="store_true", help="skip installed distribution scan")
    plugins_list_parser.add_argument("--json", action="store_true", help="emit JSON")
    plugins_inspect_parser = plugins_subparsers.add_parser("inspect", help="show one discovered plugin manifest")
    plugins_inspect_parser.add_argument("plugin_id")
    plugins_inspect_parser.add_argument("--path", action="append", default=[], help="manifest file or directory to scan")
    plugins_inspect_parser.add_argument("--no-installed", action="store_true", help="skip installed distribution scan")
    plugins_validate_parser = plugins_subparsers.add_parser("validate", help="validate one static plugin manifest")
    plugins_validate_parser.add_argument("path", type=Path)
    plugins_validate_parser.add_argument("--json", action="store_true", help="emit JSON")

    packages_parser = subparsers.add_parser("packages", help="inspect the official package catalog")
    packages_subparsers = packages_parser.add_subparsers(dest="packages_command")
    packages_list_parser = packages_subparsers.add_parser("list", help="list package catalog entries")
    packages_list_parser.add_argument("--catalog", type=Path, help="override package-catalog.yaml")
    packages_list_parser.add_argument("--json", action="store_true", help="emit JSON")
    packages_doctor_parser = packages_subparsers.add_parser("doctor", help="validate package catalog closure")
    packages_doctor_parser.add_argument("--catalog", type=Path, help="override package-catalog.yaml")
    packages_doctor_parser.add_argument("--json", action="store_true", help="emit JSON")

    policy_parser = subparsers.add_parser("policy", help="validate and test policy bundles")
    policy_subparsers = policy_parser.add_subparsers(dest="policy_command")
    policy_test_parser = policy_subparsers.add_parser("test", help="run static policy test cases")
    policy_test_parser.add_argument("policy", type=Path)
    policy_test_parser.add_argument("--cases", required=True, type=Path, help="case YAML file or directory")
    policy_test_parser.add_argument("--json", action="store_true", help="emit JSON")

    observe_parser = subparsers.add_parser("observe", help="inspect local runtime state")
    observe_subparsers = observe_parser.add_subparsers(dest="observe_command")
    observe_run_parser = observe_subparsers.add_parser("run", help="inspect one run from a SQLite run store")
    observe_run_parser.add_argument("run_id")
    observe_run_parser.add_argument("--store", required=True, type=Path, help="SQLite run store path")
    observe_run_parser.add_argument("--json", action="store_true", help="emit JSON")

    release_parser = subparsers.add_parser("release", help="verify immutable graph releases")
    release_subparsers = release_parser.add_subparsers(dest="release_command")
    release_verify_parser = release_subparsers.add_parser(
        "verify",
        help="verify a GraphRelease document and its production pins",
    )
    release_verify_parser.add_argument("path", type=Path)
    release_verify_parser.add_argument("--json", action="store_true", help="emit JSON")

    lock_parser = subparsers.add_parser("lock", help="create a semantic graph lockfile")
    lock_parser.add_argument("path", type=Path)
    lock_parser.add_argument("--output", type=Path, help="write lock JSON to this path instead of stdout")
    lock_parser.add_argument("--catalog", type=Path, help="override package-catalog.yaml")
    lock_parser.add_argument("--package", action="append", default=[], help="additional package distribution to lock")
    lock_parser.add_argument("--no-default", action="store_true", help="do not include the default metapackage closure")

    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.command == "validate":
        documents = load_documents(args.path)
        diagnostics: list[dict[str, str]] = []
        ok = True
        block_catalog = None
        if args.plugin_path:
            registry = discover_plugins(args.plugin_path, include_installed=True)
            block_catalog = BlockCatalog.from_manifests(registry.manifests)
            ok = ok and registry.ok
            diagnostics.extend(item.to_dict() | {"document": "plugins"} for item in registry.diagnostics.diagnostics)
        for index, document in enumerate(documents):
            if document.get("kind") == "Graph":
                plan = compile_graph(document, block_catalog=block_catalog)
                ok = ok and plan.ok
                diagnostics.extend(
                    {
                        **item.to_dict(),
                        "document": str(index),
                    }
                    for item in plan.diagnostics.diagnostics
                )
            elif document.get("kind") in STRUCTURAL_KINDS:
                for field in ("apiVersion", "kind", "metadata", "spec"):
                    if field not in document:
                        ok = False
                        diagnostics.append(
                            Diagnostic("GB0004", f"missing required field {field!r}", f"$[{index}].{field}").to_dict()
                            | {"document": str(index)}
                        )
            else:
                ok = False
                diagnostics.append(
                    Diagnostic("GB0001", f"unsupported document kind {document.get('kind')!r}", f"$[{index}].kind").to_dict()
                    | {"document": str(index)}
                )
        if args.json:
            print(json.dumps({"ok": ok, "diagnostics": diagnostics}, indent=2, sort_keys=True))
        else:
            if diagnostics:
                for item in diagnostics:
                    print(f"{item['severity']} {item['code']} {item['path']}: {item['message']}")
            else:
                print("OK")
        return 0 if ok else 1
    if args.command == "plan":
        documents = load_documents(args.path)
        graph_documents = [document for document in documents if document.get("kind") == "Graph"]
        if not graph_documents:
            print(f"{args.path}: no Graph document found")
            return 1
        block_catalog = None
        if args.plugin_path:
            registry = discover_plugins(args.plugin_path, include_installed=True)
            block_catalog = BlockCatalog.from_manifests(registry.manifests)
        plan = compile_graph(graph_documents[0], block_catalog=block_catalog)
        payload: dict[str, object] = {
            "target": args.target,
            "hash": plan.graph_hash,
            "ok": plan.ok,
            "diagnostics": plan.diagnostics.to_list(),
        }
        if args.expand:
            payload["graph"] = plan.normalized
        if args.show_bindings:
            payload["bindings"] = [document for document in documents if document.get("kind") == "Binding"]
        if args.show_packages:
            nodes = plan.normalized.get("spec", {}).get("nodes", {})
            payload["requirements"] = sorted(
                {
                    str(node.get("block")).split("@", 1)[0]
                    for node in nodes.values()
                    if isinstance(node, dict) and isinstance(node.get("block"), str)
                }
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if plan.ok else 1
    if args.command == "run":
        documents = load_documents(args.path)
        graph_documents = [document for document in documents if document.get("kind") == "Graph"]
        if not graph_documents:
            print(f"{args.path}: no Graph document found")
            return 1
        inputs = json.loads(args.input_json)
        if not isinstance(inputs, dict):
            print("--input-json must decode to a JSON object")
            return 1
        result = InProcessRuntime(stdlib_registry()).run(graph_documents[0], inputs)
        print(
            json.dumps(
                {
                    "runId": result.run_id,
                    "status": result.status,
                    "outputs": result.outputs,
                    "journal": [asdict(record) for record in result.journal.records],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.status == "succeeded" else 1
    if args.command == "migrate":
        documents = [migrate_document(document) for document in load_documents(args.path)]
        print(yaml.safe_dump_all(documents, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    if args.command == "lock":
        documents = load_documents(args.path)
        graph_documents = [document for document in documents if document.get("kind") == "Graph"]
        if not graph_documents:
            print(f"{args.path}: no Graph document found")
            return 1
        graph = graph_documents[0]
        plan = compile_graph(graph)
        package_lock = build_package_lock(
            load_package_catalog(args.catalog),
            requested=tuple(args.package),
            include_default=not args.no_default,
        )
        metadata = graph.get("metadata", {})
        graph_id = metadata.get("name") if isinstance(metadata, dict) else None
        payload = {
            "lockVersion": 1,
            "graph": {
                "id": graph_id,
                "graphHash": plan.graph_hash,
                "schemaVersion": graph.get("apiVersion"),
            },
            "runtime": {
                "protocol": 1,
                "distribution": "graphblocks-runtime",
                "version": None,
            },
            "packages": [
                {
                    "name": entry.distribution,
                    "versionConstraint": entry.version_constraint,
                    "import": entry.import_package,
                    "default": entry.default,
                    "layer": entry.layer,
                    "kind": entry.kind,
                    "stability": entry.stability,
                    "dependencies": list(entry.dependencies),
                    "forbiddenDependencies": list(entry.forbidden_dependencies),
                }
                for entry in package_lock.entries
            ],
            "excludedCategories": list(package_lock.excluded_categories),
            "diagnostics": plan.diagnostics.to_list(),
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0 if plan.ok else 1
    if args.command == "plugins":
        if args.plugins_command == "list":
            registry = discover_plugins(args.path, include_installed=not args.no_installed)
            payload = {"ok": registry.ok, "diagnostics": registry.diagnostics.to_list(), "plugins": registry.summaries()}
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for plugin in payload["plugins"]:
                    print(
                        f"{plugin['pluginId']} {plugin['version']} "
                        f"{plugin['maturity']} blocks={plugin['blocks']} source={plugin['source']}"
                    )
                for item in payload["diagnostics"]:
                    print(f"{item['severity']} {item['code']} {item['path']}: {item['message']}")
            return 0 if registry.ok else 1
        if args.plugins_command == "inspect":
            registry = discover_plugins(args.path, include_installed=not args.no_installed)
            for manifest in registry.manifests:
                if manifest.plugin_id == args.plugin_id:
                    print(json.dumps(manifest.raw, indent=2, sort_keys=True))
                    return 0 if registry.ok else 1
            print(f"plugin not found: {args.plugin_id}")
            return 1
        if args.plugins_command == "validate":
            manifest = load_plugin_manifest(args.path)
            diagnostics = validate_plugin_manifest(manifest.raw)
            payload = {"ok": diagnostics.ok, "diagnostics": diagnostics.to_list(), "plugin": manifest.summary()}
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                if diagnostics.diagnostics:
                    for item in diagnostics.diagnostics:
                        print(f"{item.severity} {item.code} {item.path}: {item.message}")
                else:
                    print("OK")
            return 0 if diagnostics.ok else 1
        plugins_parser.print_help()
        return 0
    if args.command == "packages":
        if args.packages_command == "list":
            rows = package_rows(load_package_catalog(args.catalog))
            if args.json:
                print(json.dumps({"packages": rows}, indent=2, sort_keys=True))
            else:
                for row in rows:
                    default = "default" if row["default"] else "optional"
                    print(
                        f"{row['distribution']} {default} phase={row['implementationPhase']} "
                        f"{row['kind']} {row['stability']}"
                    )
            return 0
        if args.packages_command == "doctor":
            diagnostics = doctor_package_catalog(load_package_catalog(args.catalog))
            if args.json:
                print(json.dumps({"ok": diagnostics.ok, "diagnostics": diagnostics.to_list()}, indent=2, sort_keys=True))
            elif diagnostics.diagnostics:
                for item in diagnostics.diagnostics:
                    print(f"{item.severity} {item.code} {item.path}: {item.message}")
            else:
                print("OK")
            return 0 if diagnostics.ok else 1
        packages_parser.print_help()
        return 0
    if args.command == "observe":
        if args.observe_command == "run":
            store = SQLiteRunStore(args.store)
            try:
                record = store.get_run(args.run_id)
            except KeyError:
                store.close()
                print(f"run not found: {args.run_id}")
                return 1
            store.close()
            payload = {
                "runId": record.run_id,
                "graphHash": record.graph_hash,
                "status": record.status,
                "stateRevision": record.state_revision,
                "inputs": record.inputs,
                "state": record.state,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"{record.run_id} {record.status} {record.graph_hash} stateRevision={record.state_revision}")
            return 0
        observe_parser.print_help()
        return 0
    if args.command == "release":
        if args.release_command == "verify":
            try:
                documents = [
                    document
                    for document in load_documents(args.path)
                    if document.get("kind") == "GraphRelease"
                ]
                if len(documents) != 1:
                    raise ValueError(f"expected one GraphRelease document, found {len(documents)}")
                document = documents[0]
                metadata = document.get("metadata", {})
                spec = document.get("spec", {})
                if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                    raise ValueError("GraphRelease documents require metadata and spec mappings")
                name = str(_field(metadata, "name", default="")).strip()
                version = str(_field(metadata, "version", default=_field(spec, "version", default=""))).strip()
                if not name:
                    raise ValueError("GraphRelease metadata.name is required")
                if not version:
                    raise ValueError("GraphRelease metadata.version is required")

                release = GraphRelease(name=name, version=version)
                bundle_data = _field(spec, "bundle", default={})
                if bundle_data is not None:
                    if not isinstance(bundle_data, Mapping):
                        raise ValueError("GraphRelease spec.bundle must be a mapping")
                    bundle_digest = _field(bundle_data, "digest")
                    bundle_ref = _field(bundle_data, "ref")
                    if bundle_digest is None and isinstance(bundle_ref, str) and "@sha256:" in bundle_ref:
                        bundle_digest = f"sha256:{bundle_ref.rsplit('@sha256:', 1)[1]}"
                    media_type = _field(bundle_data, "mediaType", "media_type")
                    if bundle_digest is not None or media_type is not None:
                        release = release.with_bundle(str(bundle_digest or ""), str(media_type or ""))

                application_data = _field(spec, "application")
                if isinstance(application_data, Mapping):
                    application_hash = _field(application_data, "hash", "applicationHash", "application_hash")
                    if application_hash is not None:
                        release = release.with_application_hash(str(application_hash))

                graphs_data = _field(spec, "graphs", default={})
                if not isinstance(graphs_data, Mapping):
                    raise ValueError("GraphRelease spec.graphs must be a mapping")
                for graph_name, graph_data in graphs_data.items():
                    if not isinstance(graph_data, Mapping):
                        raise ValueError(f"GraphRelease graph {graph_name!r} must be a mapping")
                    release = release.with_graph(
                        str(graph_name),
                        GraphReleaseGraph(
                            graph_hash=str(_field(graph_data, "graphHash", "graph_hash", default="")),
                            normalized_plan_hash=str(
                                _field(
                                    graph_data,
                                    "normalizedPlanHash",
                                    "normalized_plan_hash",
                                    default="",
                                )
                            ),
                        ),
                    )

                images_data = _field(spec, "images", default={})
                if not isinstance(images_data, Mapping):
                    raise ValueError("GraphRelease spec.images must be a mapping")
                for image_name, image_data in images_data.items():
                    if isinstance(image_data, Mapping):
                        image_value = _field(image_data, "image", "ref", default="")
                    else:
                        image_value = image_data
                    release = release.with_image(str(image_name), ImageRef(str(image_value)))

                prompts_data = _field(spec, "prompts", "promptLocks", "prompt_locks", default={})
                if not isinstance(prompts_data, Mapping):
                    raise ValueError("GraphRelease spec.prompts must be a mapping")
                for prompt_name, prompt_data in prompts_data.items():
                    if not isinstance(prompt_data, Mapping):
                        raise ValueError(f"GraphRelease prompt {prompt_name!r} must be a mapping")
                    resolved_name = str(_field(prompt_data, "name", default=prompt_name))
                    prompt_version = _field(prompt_data, "version")
                    prompt_label = _field(prompt_data, "label", "lockLabel", "lock_label")
                    if prompt_version is not None:
                        prompt_lock = PromptLock.versioned(resolved_name, str(prompt_version))
                    elif prompt_label is not None:
                        prompt_lock = PromptLock.label(resolved_name, str(prompt_label))
                    else:
                        raise ValueError(
                            f"GraphRelease prompt {prompt_name!r} requires version or label"
                        )
                    release = release.with_prompt_lock(str(prompt_name), prompt_lock)

                knowledge_data = _field(spec, "knowledge", default={})
                if not isinstance(knowledge_data, Mapping):
                    raise ValueError("GraphRelease spec.knowledge must be a mapping")
                top_level_revision = _field(knowledge_data, "indexRevision", "index_revision")
                if top_level_revision is not None:
                    release = release.with_knowledge(
                        KnowledgeBinding(
                            index_id=str(_field(knowledge_data, "indexId", "index_id", default="default")),
                            index_revision=str(top_level_revision),
                        )
                    )
                else:
                    for index_id, binding_data in knowledge_data.items():
                        if not isinstance(binding_data, Mapping):
                            raise ValueError(
                                f"GraphRelease knowledge binding {index_id!r} must be a mapping"
                            )
                        release = release.with_knowledge(
                            KnowledgeBinding(
                                index_id=str(_field(binding_data, "indexId", "index_id", default=index_id)),
                                index_revision=str(
                                    _field(
                                        binding_data,
                                        "indexRevision",
                                        "index_revision",
                                        default="",
                                    )
                                ),
                            )
                        )

                mutable_references: list[str] = []
                try:
                    release.validate_production_pins()
                except GraphReleaseMutableReferencesError as error:
                    mutable_references.extend(error.references)
                payload = {
                    "ok": not mutable_references,
                    "name": release.name,
                    "version": release.version,
                    "releaseDigest": release.content_digest(),
                    "mutableReferences": mutable_references,
                }
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                elif mutable_references:
                    print(f"FAIL {release.name} mutable references: {', '.join(mutable_references)}")
                else:
                    print(f"OK {release.name} {release.version} {release.content_digest()}")
                return 0 if payload["ok"] else 1
            except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
                if args.json:
                    print(json.dumps({"ok": False, "error": str(error)}, indent=2, sort_keys=True))
                else:
                    print(f"release verify error: {error}")
                return 1
        release_parser.print_help()
        return 0
    if args.command == "policy":
        if args.policy_command == "test":
            try:
                bundles: list[PolicyBundle] = []
                for document in _documents_from_path(args.policy):
                    if document.get("kind") != "PolicyBundle":
                        continue
                    metadata = document.get("metadata", {})
                    spec = document.get("spec", {})
                    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                        raise ValueError("PolicyBundle documents require metadata and spec mappings")
                    rules: list[PolicyRule] = []
                    for rule_data in spec.get("rules", []):
                        if not isinstance(rule_data, Mapping):
                            raise ValueError("PolicyBundle rules must be mappings")
                        obligations: list[PolicyObligation] = []
                        for obligation_data in rule_data.get("obligations", []):
                            if not isinstance(obligation_data, Mapping):
                                raise ValueError("PolicyRule obligations must be mappings")
                            obligations.append(
                                PolicyObligation(
                                    obligation_id=str(
                                        _field(obligation_data, "obligationId", "obligation_id", "id")
                                    ),
                                    obligation_type=str(
                                        _field(obligation_data, "obligationType", "obligation_type", "type")
                                    ),
                                    parameters=dict(_field(obligation_data, "parameters", default={}) or {}),
                                )
                            )
                        rules.append(
                            PolicyRule(
                                rule_id=str(_field(rule_data, "ruleId", "rule_id", "id")),
                                effect=str(_field(rule_data, "effect")),
                                actions=_tuple_field(rule_data, "actions"),
                                resource_selectors=_tuple_field(
                                    rule_data,
                                    "resourceSelectors",
                                    "resource_selectors",
                                ),
                                principal_selectors=_tuple_field(
                                    rule_data,
                                    "principalSelectors",
                                    "principal_selectors",
                                ),
                                obligations=tuple(obligations),
                                priority=int(_field(rule_data, "priority", default=0) or 0),
                            )
                        )
                    bundles.append(
                        PolicyBundle(
                            bundle_id=str(_field(spec, "bundleId", "bundle_id", default=metadata.get("name"))),
                            version=str(_field(spec, "version", default=metadata.get("version", "0.0.0"))),
                            rule_language=str(_field(spec, "ruleLanguage", "rule_language", default="static")),
                            rules=tuple(rules),
                            obligation_schema_versions=_tuple_field(
                                spec,
                                "obligationSchemaVersions",
                                "obligation_schema_versions",
                            ),
                            default_fail_modes=dict(
                                _field(spec, "defaultFailModes", "default_fail_modes", default={}) or {}
                            ),
                            signature_ref=_field(spec, "signatureRef", "signature_ref"),
                        )
                    )
                cases: list[PolicyTestCase] = []
                for document in _documents_from_path(args.cases):
                    if document.get("kind") != "PolicyTestCase":
                        continue
                    metadata = document.get("metadata", {})
                    spec = document.get("spec", {})
                    if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                        raise ValueError("PolicyTestCase documents require metadata and spec mappings")
                    request_data = _field(spec, "request")
                    expectation_data = _field(spec, "expect", "expectation", default={})
                    if not isinstance(request_data, Mapping) or not isinstance(expectation_data, Mapping):
                        raise ValueError("PolicyTestCase spec requires request and expect mappings")
                    resource_data = _field(request_data, "resource")
                    if not isinstance(resource_data, Mapping):
                        raise ValueError("PolicyTestCase request.resource must be a mapping")
                    principal_data = _field(request_data, "principal")
                    principal = None
                    if principal_data is not None:
                        if not isinstance(principal_data, Mapping):
                            raise ValueError("PolicyTestCase request.principal must be a mapping")
                        principal = PrincipalRef(
                            principal_id=str(_field(principal_data, "principalId", "principal_id", "id")),
                            tenant_id=_field(principal_data, "tenantId", "tenant_id"),
                            groups=_tuple_field(principal_data, "groups"),
                            roles=_tuple_field(principal_data, "roles"),
                            attributes=dict(_field(principal_data, "attributes", default={}) or {}),
                        )
                    tenant_data = _field(request_data, "tenant")
                    atomic_unit_data = _field(request_data, "atomicUnit", "atomic_unit")
                    request = PolicyRequest(
                        request_id=str(_field(request_data, "requestId", "request_id", "id")),
                        enforcement_point=str(_field(request_data, "enforcementPoint", "enforcement_point")),
                        action=str(_field(request_data, "action")),
                        resource=_resource_ref_from_mapping(resource_data),
                        occurred_at=str(_field(request_data, "occurredAt", "occurred_at")),
                        principal=principal,
                        tenant=_resource_ref_from_mapping(tenant_data) if isinstance(tenant_data, Mapping) else None,
                        release_id=_field(request_data, "releaseId", "release_id"),
                        deployment_revision_id=_field(
                            request_data,
                            "deploymentRevisionId",
                            "deployment_revision_id",
                        ),
                        run_id=_field(request_data, "runId", "run_id"),
                        atomic_unit=_resource_ref_from_mapping(atomic_unit_data)
                        if isinstance(atomic_unit_data, Mapping)
                        else None,
                        data_labels=_tuple_field(request_data, "dataLabels", "data_labels"),
                        requested_usage=tuple(_field(request_data, "requestedUsage", "requested_usage", default=()) or ()),
                        attributes=dict(_field(request_data, "attributes", default={}) or {}),
                        policy_snapshot_id=_field(request_data, "policySnapshotId", "policy_snapshot_id"),
                    )
                    expectation = PolicyTestExpectation(
                        effect=_field(expectation_data, "effect"),
                        reason_codes=_tuple_field(expectation_data, "reasonCodes", "reason_codes"),
                        policy_refs=_tuple_field(expectation_data, "policyRefs", "policy_refs"),
                        obligation_ids=_tuple_field(expectation_data, "obligationIds", "obligation_ids"),
                        enforcement_status=_field(
                            expectation_data,
                            "enforcementStatus",
                            "enforcement_status",
                        ),
                    )
                    enforced_obligation_ids = None
                    raw_enforced_obligation_ids = _field(
                        spec,
                        "enforcedObligationIds",
                        "enforced_obligation_ids",
                    )
                    if raw_enforced_obligation_ids is not None:
                        enforced_obligation_ids = _tuple_field(
                            spec,
                            "enforcedObligationIds",
                            "enforced_obligation_ids",
                        )
                    cases.append(
                        PolicyTestCase(
                            str(_field(spec, "caseId", "case_id", default=metadata.get("name"))),
                            request,
                            expectation,
                            evaluated_at=str(_field(spec, "evaluatedAt", "evaluated_at")),
                            enforced_obligation_ids=enforced_obligation_ids,
                        )
                    )
                if not bundles:
                    print(f"{args.policy}: no PolicyBundle document found")
                    return 1
                if not cases:
                    print(f"{args.cases}: no PolicyTestCase document found")
                    return 1
                report = run_policy_tests(StaticPolicyEvaluator.from_bundles(bundles), cases)
                if args.json:
                    print(
                        json.dumps(
                            {
                                "ok": report.passed,
                                "cases": [
                                    {
                                        "caseId": result.case_id,
                                        "passed": result.passed,
                                        "failures": list(result.failures),
                                    }
                                    for result in report.results
                                ],
                            },
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    for result in report.results:
                        status = "OK" if result.passed else "FAIL"
                        print(f"{status} {result.case_id}")
                        for failure in result.failures:
                            print(f"  {failure}")
                return 0 if report.passed else 1
            except (KeyError, TypeError, ValueError) as error:
                print(f"policy test error: {error}")
                return 1
        policy_parser.print_help()
        return 0
    parser.print_help()
    return 0
