from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from . import __version__
from .compiler import compile_graph
from .diagnostics import Diagnostic
from .loader import load_documents
from .migration import migrate_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphblocks")
    parser.add_argument("--version", action="store_true", help="show package version")
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate", help="validate GraphBlocks YAML documents")
    validate_parser.add_argument("path", type=Path)
    validate_parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")

    plan_parser = subparsers.add_parser("plan", help="compile a GraphSpec into normalized plan JSON")
    plan_parser.add_argument("path", type=Path)
    plan_parser.add_argument("--expand", action="store_true", help="include normalized graph")
    plan_parser.add_argument("--show-bindings", action="store_true", help="include Binding documents from the same file")
    plan_parser.add_argument("--show-packages", action="store_true", help="include inferred semantic block requirements")
    plan_parser.add_argument("--target", default="local-python", help="execution target label for diagnostics")

    migrate_parser = subparsers.add_parser("migrate", help="read legacy alpha documents and emit current YAML")
    migrate_parser.add_argument("path", type=Path)

    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.command == "validate":
        documents = load_documents(args.path)
        diagnostics: list[dict[str, str]] = []
        ok = True
        for index, document in enumerate(documents):
            if document.get("kind") == "Graph":
                plan = compile_graph(document)
                ok = ok and plan.ok
                diagnostics.extend(
                    {
                        **item.to_dict(),
                        "document": str(index),
                    }
                    for item in plan.diagnostics.diagnostics
                )
            elif document.get("kind") in {"Binding", "Application", "PolicyProfile", "GraphDeployment", "PluginManifest"}:
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
        plan = compile_graph(graph_documents[0])
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
    if args.command == "migrate":
        documents = [migrate_document(document) for document in load_documents(args.path)]
        print(yaml.safe_dump_all(documents, sort_keys=False, allow_unicode=True).rstrip())
        return 0
    parser.print_help()
    return 0
