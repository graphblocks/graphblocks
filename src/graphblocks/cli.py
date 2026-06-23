from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import yaml

from . import __version__
from .compiler import compile_graph
from .diagnostics import Diagnostic
from .loader import load_documents
from .migration import migrate_document
from .packages import build_package_lock, doctor_package_catalog, load_package_catalog, package_rows
from .plugins import BlockCatalog, discover_plugins, load_plugin_manifest, validate_plugin_manifest
from .runtime import InProcessRuntime, stdlib_registry

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
    parser.print_help()
    return 0
