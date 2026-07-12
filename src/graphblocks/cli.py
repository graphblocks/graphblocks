from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
from importlib import resources
import io
import json
from pathlib import Path
import tarfile

import yaml

from . import __version__
from .canonical import canonical_dumps, canonical_hash, canonical_loads
from .compiler import compile_graph
from .deployment import (
    DeploymentRevision,
    DeploymentTargetProfileSet,
    ExecutionTarget,
    GraphDeployment,
    GraphRelease,
    GraphReleaseGraph,
    GraphReleaseMutableReferencesError,
    ImageRef,
    KnowledgeBinding,
    PlacementRule,
    PlacementSelector,
    PromptLock,
    ReleaseLockRef,
    SupplyChainLock,
)
from .diagnostics import Diagnostic
from .loader import load_documents
from .migration import migrate_document
from .packages import (
    PackageManifestAuditPolicy,
    audit_package_manifests,
    build_package_lock,
    build_wheel_matrix,
    doctor_package_catalog,
    load_package_catalog,
    package_rows,
)
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
from .runtime import InProcessRuntime, SQLiteExecutionJournal, stdlib_registry
from .run_store import RunDeploymentProvenance, SQLiteRunStore
from .schema import SchemaManifest, SchemaManifestError

STRUCTURAL_KINDS = {
    "Application",
    "Binding",
    "ConformanceProfileSet",
    "DeploymentTargetProfileSet",
    "GraphDeployment",
    "GraphRelease",
    "ObservabilityProfile",
    "PluginManifest",
    "PolicyProfile",
}
PHASE_FIVE_IMAGE_ROLES = (
    "control-plane",
    "rag-cpu",
    "document-cpu",
    "ocr-gpu",
    "sandbox",
)


def _loads_strict_json(owner: str, value: str) -> object:
    try:
        return canonical_loads(value)
    except ValueError as error:
        raise ValueError(f"{owner} must be valid strict JSON") from error


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
    run_parser.add_argument(
        "--runtime",
        choices=("python", "native"),
        default="python",
        help="runtime backend to use; native delegates to graphblocks-runtime's Rust PyO3 bridge",
    )
    run_parser.add_argument("--run-store", type=Path, help="persist run metadata to a SQLite run store")
    run_parser.add_argument("--journal-store", type=Path, help="persist execution journal records to SQLite")
    run_parser.add_argument("--run-id", help="caller-selected run id for deterministic local execution evidence")
    run_parser.add_argument(
        "--deployment-plan",
        type=Path,
        help="deploy plan JSON whose immutable release and physical-plan identities are recorded on the run",
    )
    run_parser.add_argument(
        "--release-signature-digest",
        help="signature digest for the release referenced by --deployment-plan",
    )

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
    packages_doctor_parser.add_argument("--root", type=Path, help="cross-check local pyproject dependency closure")
    packages_doctor_parser.add_argument("--json", action="store_true", help="emit JSON")
    packages_audit_parser = packages_subparsers.add_parser(
        "audit",
        help="audit local package manifests for license and blocked dependencies",
    )
    packages_audit_parser.add_argument("--root", type=Path, default=Path("."))
    packages_audit_parser.add_argument("--allowed-license", action="append", default=["Apache-2.0"])
    packages_audit_parser.add_argument("--blocked-dependency", action="append", default=[])
    packages_audit_parser.add_argument("--json", action="store_true", help="emit JSON")
    packages_wheel_matrix_parser = packages_subparsers.add_parser(
        "wheel-matrix",
        help="validate first-party Python wheel build matrix",
    )
    packages_wheel_matrix_parser.add_argument("--root", type=Path, default=Path("."))
    packages_wheel_matrix_parser.add_argument(
        "--python",
        action="append",
        dest="python_versions",
        default=[],
        help="required Python version, repeatable; defaults to 3.11 and 3.12",
    )
    packages_wheel_matrix_parser.add_argument("--json", action="store_true", help="emit JSON")

    schemas_parser = subparsers.add_parser("schemas", help="inspect checked-in JSON Schema documents")
    schemas_subparsers = schemas_parser.add_subparsers(dest="schemas_command")
    schemas_manifest_parser = schemas_subparsers.add_parser(
        "manifest",
        help="emit a deterministic schema generation manifest",
    )
    schemas_manifest_parser.add_argument("path", nargs="?", type=Path)

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
    observe_journal_parser = observe_subparsers.add_parser(
        "journal",
        help="inspect one run execution journal from a SQLite journal store",
    )
    observe_journal_parser.add_argument("run_id")
    observe_journal_parser.add_argument("--store", required=True, type=Path, help="SQLite execution journal path")
    observe_journal_parser.add_argument("--json", action="store_true", help="emit JSON")

    release_parser = subparsers.add_parser("release", help="verify immutable graph releases")
    release_subparsers = release_parser.add_subparsers(dest="release_command")
    release_build_parser = release_subparsers.add_parser(
        "build",
        help="build a deterministic local GraphRelease bundle",
    )
    release_build_parser.add_argument("path", type=Path)
    release_build_parser.add_argument("--out", required=True, type=Path)
    release_build_parser.add_argument("--json", action="store_true", help="emit JSON")
    release_verify_parser = release_subparsers.add_parser(
        "verify",
        help="verify a GraphRelease document and its production pins",
    )
    release_verify_parser.add_argument("path", type=Path)
    release_verify_parser.add_argument("--json", action="store_true", help="emit JSON")

    deploy_parser = subparsers.add_parser("deploy", help="compile graph deployment plans")
    deploy_subparsers = deploy_parser.add_subparsers(dest="deploy_command")
    deploy_targets_parser = deploy_subparsers.add_parser(
        "targets-verify",
        help="verify a DeploymentTargetProfileSet manifest",
    )
    deploy_targets_parser.add_argument("path", type=Path)
    deploy_targets_parser.add_argument(
        "--required-role",
        action="append",
        default=[],
        help="required production image role; defaults to the Phase 5 image roles",
    )
    deploy_targets_parser.add_argument("--json", action="store_true", help="emit JSON")
    deploy_plan_parser = deploy_subparsers.add_parser(
        "plan",
        help="resolve GraphRelease and GraphDeployment documents into a physical plan",
    )
    deploy_plan_parser.add_argument("path", type=Path)
    deploy_plan_parser.add_argument("--revision", required=True, help="immutable deployment revision id")
    deploy_plan_parser.add_argument("--created-at", default="", help="deployment revision creation timestamp")
    deploy_plan_parser.add_argument("--graph", help="release graph name; inferred when the release has one graph")
    deploy_plan_parser.add_argument("--json", action="store_true", help="emit JSON")
    deploy_render_parser = deploy_subparsers.add_parser(
        "render",
        help="render manifests from a deploy plan JSON payload",
    )
    deploy_render_parser.add_argument("path", type=Path)
    deploy_render_parser.add_argument("--target", default="kubernetes", choices=["kubernetes", "helm"])
    deploy_render_parser.add_argument("--namespace", default="default")
    deploy_render_parser.add_argument("--name", help="manifest name prefix; defaults to deployment id")
    deploy_render_parser.add_argument("--replicas", type=int, default=1)
    deploy_render_parser.add_argument("--json", action="store_true", help="emit JSON")

    lock_parser = subparsers.add_parser("lock", help="create a semantic graph lockfile")
    lock_parser.add_argument("path", type=Path)
    lock_parser.add_argument("--output", type=Path, help="write lock JSON to this path instead of stdout")
    lock_parser.add_argument("--catalog", type=Path, help="override package-catalog.yaml")
    lock_parser.add_argument("--package", action="append", default=[], help="additional package distribution to lock")
    lock_parser.add_argument(
        "--no-default",
        action="store_true",
        help="do not include the default component and artifact selection",
    )

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
        try:
            inputs = _loads_strict_json("--input-json", args.input_json)
        except ValueError as error:
            print(error)
            return 1
        if not isinstance(inputs, dict):
            print("--input-json must decode to a JSON object")
            return 1
        deployment_provenance = None
        if (args.deployment_plan is None) != (args.release_signature_digest is None):
            print("--deployment-plan and --release-signature-digest must be provided together")
            return 1
        if args.deployment_plan is not None:
            try:
                deployment_plan_payload = _loads_strict_json(
                    "deploy plan payload",
                    args.deployment_plan.read_text(encoding="utf-8"),
                )
                if not isinstance(deployment_plan_payload, Mapping):
                    raise ValueError("deploy plan payload must be a JSON object")
                if deployment_plan_payload.get("ok") is not True:
                    raise ValueError("deploy plan payload is not successful")
                provenance_fields: dict[str, str] = {}
                for field_name in (
                    "releaseDigest",
                    "deploymentRevisionId",
                    "planHash",
                ):
                    value = deployment_plan_payload.get(field_name)
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(
                            f"deploy plan payload {field_name} must be a non-empty string"
                        )
                    if value != value.strip():
                        raise ValueError(
                            f"deploy plan payload {field_name} must not contain surrounding whitespace"
                        )
                    provenance_fields[field_name] = value
                deployment_revision = deployment_plan_payload.get("deploymentRevision")
                if not isinstance(deployment_revision, Mapping):
                    raise ValueError("deploy plan payload deploymentRevision must be an object")
                for revision_field, top_level_field in (
                    ("revisionId", "deploymentRevisionId"),
                    ("releaseDigest", "releaseDigest"),
                    ("physicalPlanHash", "planHash"),
                ):
                    if deployment_revision.get(revision_field) != provenance_fields[top_level_field]:
                        raise ValueError(
                            "deploy plan payload deploymentRevision does not match top-level provenance"
                        )
                deployment_provenance = RunDeploymentProvenance(
                    release_digest=provenance_fields["releaseDigest"],
                    deployment_revision_id=provenance_fields["deploymentRevisionId"],
                    physical_plan_hash=provenance_fields["planHash"],
                    release_signature_digest=args.release_signature_digest,
                ).validate_for_production()
                physical_plan = deployment_plan_payload.get("plan")
                if not isinstance(physical_plan, Mapping):
                    raise ValueError("deploy plan payload plan must be an object")
                graph_hash = physical_plan.get("graphHash")
                if not isinstance(graph_hash, str) or not graph_hash.strip():
                    raise ValueError("deploy plan payload plan.graphHash must be a non-empty string")
                compiled_graph_hash = compile_graph(graph_documents[0]).graph_hash
                if graph_hash != compiled_graph_hash:
                    raise ValueError("deploy plan graphHash does not match the runtime graph")
                targets = physical_plan.get("targets", {})
                if not isinstance(targets, Mapping):
                    raise ValueError("deploy plan payload plan.targets must be an object")
                canonical_targets: list[dict[str, object]] = []
                for target_id, target in sorted(targets.items()):
                    if not isinstance(target, Mapping):
                        raise ValueError("deploy plan payload plan target must be an object")
                    capabilities = target.get("capabilities", [])
                    effects = target.get("effects", [])
                    if not isinstance(capabilities, list) or not isinstance(effects, list):
                        raise ValueError(
                            "deploy plan payload plan target capabilities and effects must be arrays"
                        )
                    canonical_targets.append(
                        {
                            "target_id": target_id,
                            "kind": target.get("kind"),
                            "execution_host": target.get("executionHost"),
                            "capabilities": capabilities,
                            "effects": effects,
                            "package_lock": target.get("packageLock"),
                            "image": target.get("image"),
                        }
                    )
                placements = physical_plan.get("placements", [])
                if not isinstance(placements, list):
                    raise ValueError("deploy plan payload plan.placements must be an array")
                canonical_placements: list[dict[str, object]] = []
                for placement in placements:
                    if not isinstance(placement, Mapping):
                        raise ValueError("deploy plan payload plan placement must be an object")
                    selector = placement.get("selector")
                    if not isinstance(selector, Mapping):
                        raise ValueError("deploy plan payload plan placement selector must be an object")
                    selector_values = selector.get("values", [])
                    if not isinstance(selector_values, list):
                        raise ValueError(
                            "deploy plan payload plan placement selector values must be an array"
                        )
                    canonical_placements.append(
                        {
                            "rule_id": placement.get("ruleId"),
                            "selector": {
                                "kind": selector.get("kind"),
                                "values": selector_values,
                            },
                            "target_id": placement.get("target"),
                        }
                    )
                canonical_placements.sort(key=canonical_dumps)
                computed_plan_hash = canonical_hash(
                    {
                        "release_digest": provenance_fields["releaseDigest"],
                        "deployment_revision_id": provenance_fields["deploymentRevisionId"],
                        "graph_hash": graph_hash,
                        "package_lock_hash": physical_plan.get("packageLockHash"),
                        "targets": canonical_targets,
                        "placements": canonical_placements,
                        "default_target": physical_plan.get("defaultTarget"),
                    }
                )
                if computed_plan_hash != provenance_fields["planHash"]:
                    raise ValueError("deploy plan payload planHash does not match plan content")
                revision_digest_fields: dict[str, str] = {}
                for field_name in (
                    "deploymentSpecHash",
                    "resolvedBindingHash",
                    "targetCapabilityHash",
                    "contentDigest",
                ):
                    value = deployment_revision.get(field_name)
                    if not isinstance(value, str):
                        raise ValueError(
                            f"deploy plan payload deploymentRevision.{field_name} must be a canonical sha256 digest"
                        )
                    digest = value.removeprefix("sha256:")
                    if (
                        not value.startswith("sha256:")
                        or len(digest) != 64
                        or any(character not in "0123456789abcdef" for character in digest)
                    ):
                        raise ValueError(
                            f"deploy plan payload deploymentRevision.{field_name} must be a canonical sha256 digest"
                        )
                    revision_digest_fields[field_name] = value
                top_level_deployment_spec_hash = deployment_plan_payload.get(
                    "deploymentSpecHash"
                )
                if top_level_deployment_spec_hash != revision_digest_fields["deploymentSpecHash"]:
                    raise ValueError(
                        "deploy plan payload deploymentRevision deploymentSpecHash does not match top-level provenance"
                    )
                computed_revision_digest = canonical_hash(
                    {
                        "release_digest": provenance_fields["releaseDigest"],
                        "deployment_spec_hash": revision_digest_fields["deploymentSpecHash"],
                        "physical_plan_hash": provenance_fields["planHash"],
                        "resolved_binding_hash": revision_digest_fields["resolvedBindingHash"],
                        "target_capability_hash": revision_digest_fields["targetCapabilityHash"],
                    }
                )
                if computed_revision_digest != revision_digest_fields["contentDigest"]:
                    raise ValueError(
                        "deploy plan payload deploymentRevision contentDigest does not match revision content"
                    )
            except (OSError, ValueError) as error:
                print(error)
                return 1
        if args.runtime == "native":
            try:
                import graphblocks_runtime
            except ImportError as error:
                print(f"graphblocks-runtime is required for --runtime native: {error}")
                return 1
            if not graphblocks_runtime.native_extension_available():
                status = graphblocks_runtime.native_extension_status()
                print(f"graphblocks-runtime native extension is not available: {status.get('error')}")
                return 1
            try:
                graph_json = json.dumps(graph_documents[0], separators=(",", ":"), sort_keys=True)
                inputs_json = canonical_dumps(inputs)
                if (
                    args.run_id is not None
                    or args.run_store is not None
                    or args.journal_store is not None
                    or deployment_provenance is not None
                ):
                    runtime_options: dict[str, object] = {}
                    if args.run_id is not None:
                        runtime_options["runId"] = args.run_id
                    if args.run_store is not None:
                        runtime_options["runStorePath"] = str(args.run_store)
                    if args.journal_store is not None:
                        runtime_options["journalStorePath"] = str(args.journal_store)
                    if deployment_provenance is not None:
                        runtime_options["deploymentProvenance"] = deployment_provenance.canonical_value()
                    result_json = graphblocks_runtime.run_stdlib_graph_with_options_json(
                        graph_json,
                        inputs_json,
                        json.dumps(runtime_options, separators=(",", ":"), sort_keys=True),
                    )
                else:
                    result_json = graphblocks_runtime.run_stdlib_graph_json(
                        graph_json,
                        inputs_json,
                    )
                result_payload = _loads_strict_json("native runtime response", result_json)
            except (RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
                print(f"native runtime execution failed: {error}")
                return 1
            if not isinstance(result_payload, dict):
                print("native runtime response must decode to a JSON object")
                return 1
            print(canonical_dumps(result_payload))
            return 0 if result_payload.get("status") == "succeeded" else 1

        run_store = SQLiteRunStore(args.run_store) if args.run_store is not None else None
        journal_factory = (
            (lambda run_id: SQLiteExecutionJournal(args.journal_store, run_id))
            if args.journal_store is not None
            else None
        )
        result = InProcessRuntime(
            stdlib_registry(),
            run_store=run_store,
            journal_factory=journal_factory,
        ).run(
            graph_documents[0],
            inputs,
            run_id=args.run_id or "run-000001",
            deployment_provenance=deployment_provenance,
        )
        print(
            json.dumps(
                {
                    "runId": result.run_id,
                    "status": result.status,
                    "outputs": result.outputs,
                    "journal": [record.to_dict() for record in result.journal.records],
                },
                indent=2,
                sort_keys=True,
            )
        )
        if hasattr(result.journal, "close"):
            result.journal.close()
        if run_store is not None:
            run_store.close()
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
            "packageCatalogVersion": package_lock.catalog_version,
            "packageLockHash": package_lock.content_digest(),
            "artifacts": list(package_lock.artifacts),
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
            diagnostics = doctor_package_catalog(load_package_catalog(args.catalog), root=args.root)
            if args.json:
                print(json.dumps({"ok": diagnostics.ok, "diagnostics": diagnostics.to_list()}, indent=2, sort_keys=True))
            elif diagnostics.diagnostics:
                for item in diagnostics.diagnostics:
                    print(f"{item.severity} {item.code} {item.path}: {item.message}")
            else:
                print("OK")
            return 0 if diagnostics.ok else 1
        if args.packages_command == "audit":
            diagnostics = audit_package_manifests(
                args.root,
                policy=PackageManifestAuditPolicy(
                    allowed_licenses=tuple(args.allowed_license),
                    blocked_dependencies=tuple(args.blocked_dependency),
                ),
            )
            if args.json:
                print(json.dumps({"ok": diagnostics.ok, "diagnostics": diagnostics.to_list()}, indent=2, sort_keys=True))
            elif diagnostics.diagnostics:
                for item in diagnostics.diagnostics:
                    print(f"{item.severity} {item.code} {item.path}: {item.message}")
            else:
                print("OK")
            return 0 if diagnostics.ok else 1
        if args.packages_command == "wheel-matrix":
            matrix = build_wheel_matrix(
                args.root,
                python_versions=tuple(args.python_versions) or ("3.11", "3.12"),
            )
            payload = matrix.matrix_contract()
            payload["contentDigest"] = matrix.content_digest()
            payload["targetCount"] = payload.pop("target_count")
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif matrix.diagnostics:
                for item in matrix.diagnostics:
                    print(f"{item.severity} {item.code} {item.path}: {item.message}")
            else:
                print(f"OK {len(matrix.targets)} wheel targets")
            return 0 if matrix.ok else 1
        packages_parser.print_help()
        return 0
    if args.command == "schemas":
        if args.schemas_command == "manifest":
            try:
                schema_root = args.path
                if schema_root is None:
                    packaged_schema_root = resources.files("graphblocks").joinpath("schemas")
                    schema_root = (
                        Path(str(packaged_schema_root))
                        if packaged_schema_root.is_dir()
                        else Path("schemas")
                    )
                manifest = SchemaManifest.from_directory(schema_root)
            except SchemaManifestError as error:
                print(str(error))
                return 1
            print(json.dumps(manifest.manifest_payload(), indent=2, sort_keys=True))
            return 0
        schemas_parser.print_help()
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
        if args.observe_command == "journal":
            journal = SQLiteExecutionJournal(args.store, args.run_id)
            records = [record.to_dict() for record in journal.records]
            terminal_kind = journal.terminal_kind
            journal.close()
            if not records:
                print(f"journal not found: {args.run_id}")
                return 1
            payload = {
                "runId": args.run_id,
                "terminalKind": terminal_kind,
                "records": records,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"{args.run_id} terminal={terminal_kind or 'none'} records={len(records)}")
                for record in records:
                    print(f"{record['sequence']} {record['kind']}")
            return 0
        observe_parser.print_help()
        return 0
    release_documents: list[dict[str, object]] = []
    release: GraphRelease | None = None
    archive_manifest: Mapping[str, object] | None = None
    archive_release_bytes: bytes | None = None
    archive_digest: str | None = None
    if (
        args.command == "release"
        and args.release_command in {"build", "verify"}
    ) or (
        args.command == "deploy" and args.deploy_command == "plan"
    ):
        try:
            if (
                args.command == "release"
                and args.release_command == "verify"
                and args.path.suffix.lower() == ".gbr"
            ):
                archive_bytes = args.path.read_bytes()
                archive_digest = (
                    "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
                )
                with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
                    if archive.getnames() != ["manifest.json", "release.json"]:
                        raise ValueError(
                            "GraphRelease bundle must contain manifest.json and release.json"
                        )
                    manifest_member = archive.getmember("manifest.json")
                    release_member = archive.getmember("release.json")
                    if not manifest_member.isfile() or not release_member.isfile():
                        raise ValueError("GraphRelease bundle members must be regular files")
                    manifest_file = archive.extractfile(manifest_member)
                    release_file = archive.extractfile(release_member)
                    if manifest_file is None or release_file is None:
                        raise ValueError("GraphRelease bundle members could not be read")
                    manifest_value = _loads_strict_json(
                        "GraphRelease bundle manifest",
                        manifest_file.read().decode("utf-8"),
                    )
                    archive_release_bytes = release_file.read()
                    release_value = _loads_strict_json(
                        "GraphRelease bundle release",
                        archive_release_bytes.decode("utf-8"),
                    )
                if not isinstance(manifest_value, Mapping):
                    raise ValueError("GraphRelease bundle manifest must be a mapping")
                if not isinstance(release_value, Mapping):
                    raise ValueError("GraphRelease bundle release must be a mapping")
                archive_manifest = manifest_value
                release_documents = [dict(release_value)]
            else:
                release_documents = load_documents(args.path)
            matching_releases = [
                document
                for document in release_documents
                if document.get("kind") == "GraphRelease"
            ]
            if len(matching_releases) != 1:
                raise ValueError(
                    f"expected one GraphRelease document, found {len(matching_releases)}"
                )
            document = matching_releases[0]
            try:
                json.dumps(document, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "GraphRelease document must contain only finite JSON numbers"
                ) from error
            metadata = document.get("metadata", {})
            spec = document.get("spec", {})
            if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                raise ValueError("GraphRelease documents require metadata and spec mappings")
            name = str(_field(metadata, "name", default="")).strip()
            version = str(
                _field(metadata, "version", default=_field(spec, "version", default=""))
            ).strip()
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
                if (
                    bundle_digest is None
                    and isinstance(bundle_ref, str)
                    and "@sha256:" in bundle_ref
                ):
                    bundle_digest = f"sha256:{bundle_ref.rsplit('@sha256:', 1)[1]}"
                media_type = _field(bundle_data, "mediaType", "media_type")
                if bundle_digest is not None or media_type is not None:
                    release = release.with_bundle(
                        str(bundle_digest or ""),
                        str(media_type or ""),
                    )

            application_data = _field(spec, "application")
            if isinstance(application_data, Mapping):
                application_hash = _field(
                    application_data,
                    "hash",
                    "applicationHash",
                    "application_hash",
                )
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
                        graph_hash=str(
                            _field(graph_data, "graphHash", "graph_hash", default="")
                        ),
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

            locks_data = _field(spec, "locks", default={})
            if not isinstance(locks_data, Mapping):
                raise ValueError("GraphRelease spec.locks must be a mapping")
            for lock_name, lock_data in locks_data.items():
                if isinstance(lock_data, Mapping):
                    lock_ref = _field(lock_data, "ref", "path", "uri", default=lock_name)
                    lock_digest = _field(lock_data, "digest")
                    lock_type = _field(lock_data, "type", "lockType", "lock_type")
                else:
                    lock_ref = lock_data
                    lock_digest = None
                    lock_type = None
                release = release.with_lock(
                    str(lock_name),
                    ReleaseLockRef(
                        ref=str(lock_ref),
                        digest=str(lock_digest) if lock_digest is not None else None,
                        lock_type=str(lock_type) if lock_type is not None else None,
                    ),
                )

            prompts_data = _field(
                spec,
                "prompts",
                "promptLocks",
                "prompt_locks",
                default={},
            )
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
                        index_id=str(
                            _field(
                                knowledge_data,
                                "indexId",
                                "index_id",
                                default="default",
                            )
                        ),
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
                            index_id=str(
                                _field(
                                    binding_data,
                                    "indexId",
                                    "index_id",
                                    default=index_id,
                                )
                            ),
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
            supply_chain_data = _field(spec, "supplyChain", "supply_chain")
            if supply_chain_data is not None:
                if not isinstance(supply_chain_data, Mapping):
                    raise ValueError("GraphRelease spec.supplyChain must be a mapping")
                release = release.with_supply_chain(
                    SupplyChainLock(
                        sbom_ref=(
                            str(_field(supply_chain_data, "sbomRef", "sbom_ref"))
                            if _field(supply_chain_data, "sbomRef", "sbom_ref") is not None
                            else None
                        ),
                        provenance_ref=(
                            str(_field(supply_chain_data, "provenanceRef", "provenance_ref"))
                            if _field(supply_chain_data, "provenanceRef", "provenance_ref") is not None
                            else None
                        ),
                        signature_policy=(
                            str(_field(supply_chain_data, "signaturePolicy", "signature_policy"))
                            if _field(supply_chain_data, "signaturePolicy", "signature_policy") is not None
                            else None
                        ),
                    )
                )
            if archive_manifest is not None:
                if archive_release_bytes is None:
                    raise ValueError("GraphRelease bundle release content is missing")
                if _field(archive_manifest, "formatVersion") != 1:
                    raise ValueError("unsupported GraphRelease bundle formatVersion")
                if (
                    _field(archive_manifest, "mediaType")
                    != "application/vnd.graphblocks.release.bundle.v1+tar"
                ):
                    raise ValueError("unsupported GraphRelease bundle mediaType")
                if _field(archive_manifest, "releaseName") != release.name:
                    raise ValueError("GraphRelease bundle releaseName mismatch")
                if _field(archive_manifest, "releaseVersion") != release.version:
                    raise ValueError("GraphRelease bundle releaseVersion mismatch")
                if _field(archive_manifest, "releaseDigest") != release.content_digest():
                    raise ValueError("GraphRelease bundle releaseDigest mismatch")
                files = _field(archive_manifest, "files", default={})
                if not isinstance(files, Mapping):
                    raise ValueError("GraphRelease bundle files must be a mapping")
                expected_release_digest = _field(files, "release.json")
                actual_release_digest = (
                    "sha256:" + hashlib.sha256(archive_release_bytes).hexdigest()
                )
                if expected_release_digest != actual_release_digest:
                    raise ValueError("GraphRelease bundle release.json digest mismatch")
        except (OSError, TypeError, ValueError, tarfile.TarError, yaml.YAMLError) as error:
            if args.json:
                print(
                    json.dumps(
                        {"ok": False, "error": str(error)},
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(f"{args.command} error: {error}")
            return 1
    if args.command == "release":
        if args.release_command == "build":
            assert release is not None
            try:
                release.validate_production_pins()
            except GraphReleaseMutableReferencesError as error:
                payload = {
                    "ok": False,
                    "name": release.name,
                    "version": release.version,
                    "releaseDigest": release.content_digest(),
                    "mutableReferences": list(error.references),
                }
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(
                        f"FAIL {release.name} mutable references: "
                        f"{', '.join(error.references)}"
                    )
                return 1

            release_bytes = json.dumps(
                matching_releases[0],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            release_file_digest = (
                "sha256:" + hashlib.sha256(release_bytes).hexdigest()
            )
            manifest = {
                "formatVersion": 1,
                "mediaType": "application/vnd.graphblocks.release.bundle.v1+tar",
                "releaseName": release.name,
                "releaseVersion": release.version,
                "releaseDigest": release.content_digest(),
                "files": {"release.json": release_file_digest},
            }
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            bundle_buffer = io.BytesIO()
            with tarfile.open(
                fileobj=bundle_buffer,
                mode="w:",
                format=tarfile.USTAR_FORMAT,
            ) as archive:
                for filename, content in (
                    ("manifest.json", manifest_bytes),
                    ("release.json", release_bytes),
                ):
                    member = tarfile.TarInfo(filename)
                    member.size = len(content)
                    member.mode = 0o644
                    member.mtime = 0
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    archive.addfile(member, io.BytesIO(content))
            bundle_bytes = bundle_buffer.getvalue()
            bundle_digest = (
                "sha256:" + hashlib.sha256(bundle_bytes).hexdigest()
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(bundle_bytes)
            payload = {
                "ok": True,
                "name": release.name,
                "version": release.version,
                "releaseDigest": release.content_digest(),
                "bundleDigest": bundle_digest,
                "output": str(args.out),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(
                    f"{args.out} {bundle_digest} "
                    f"release={release.content_digest()}"
                )
            return 0
        if args.release_command == "verify":
            assert release is not None
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
            if archive_digest is not None:
                payload["bundleDigest"] = archive_digest
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif mutable_references:
                print(f"FAIL {release.name} mutable references: {', '.join(mutable_references)}")
            else:
                print(f"OK {release.name} {release.version} {release.content_digest()}")
            return 0 if payload["ok"] else 1
        release_parser.print_help()
        return 0
    if args.command == "deploy":
        if args.deploy_command == "targets-verify":
            try:
                documents = load_documents(args.path)
                target_documents = [
                    document
                    for document in documents
                    if document.get("kind") == "DeploymentTargetProfileSet"
                ]
                if len(target_documents) != 1:
                    raise ValueError(
                        f"expected one DeploymentTargetProfileSet document, found {len(target_documents)}"
                    )
                target_set = DeploymentTargetProfileSet.from_document(target_documents[0])
                required_roles = tuple(args.required_role or PHASE_FIVE_IMAGE_ROLES)
                coverage = target_set.coverage_for_required_image_roles(required_roles)
                payload = {
                    "ok": coverage.ok,
                    "targetCount": len(target_set.targets),
                    "targetIds": list(target_set.target_ids()),
                    "imageRoles": list(target_set.image_roles()),
                    "requiredImageRoles": list(required_roles),
                    "contentDigest": target_set.content_digest(),
                    "issues": coverage.issue_contracts(),
                }
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                elif coverage.ok:
                    print(f"OK {payload['targetCount']} targets {payload['contentDigest']}")
                else:
                    print(f"FAIL deployment target coverage: {len(coverage.issues)} issue(s)")
                return 0 if coverage.ok else 1
            except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
                if args.json:
                    print(json.dumps({"ok": False, "error": str(error)}, indent=2, sort_keys=True))
                else:
                    print(f"FAIL {error}")
                return 1
        if args.deploy_command == "render":
            try:
                from .integrations.kubernetes import (
                    KubernetesManifestSet,
                    KubernetesRenderOptions,
                    render_helm_chart,
                    render_target_manifests,
                )

                payload = _loads_strict_json(
                    "deploy plan payload",
                    args.path.read_text(encoding="utf-8"),
                )
                if not isinstance(payload, Mapping):
                    raise ValueError("deploy plan payload must be a JSON object")
                if payload.get("ok") is False:
                    raise ValueError("deploy plan payload is not successful")
                plan_payload = payload.get("plan")
                if not isinstance(plan_payload, Mapping):
                    raise ValueError("deploy plan payload requires plan mapping")
                targets_payload = plan_payload.get("targets")
                if not isinstance(targets_payload, Mapping) or not targets_payload:
                    raise ValueError("deploy plan payload requires non-empty plan.targets")

                options = KubernetesRenderOptions(namespace=args.namespace)
                manifest_documents: list[dict[str, object]] = []
                name_prefix = str(args.name or payload.get("deploymentId") or "graphblocks")
                for target_id, target_payload in sorted(targets_payload.items()):
                    if not isinstance(target_payload, Mapping):
                        raise ValueError(f"deploy plan target {target_id!r} must be a mapping")
                    target = ExecutionTarget(
                        target_id=str(target_id),
                        kind=str(target_payload.get("kind", "")),
                        execution_host=str(target_payload.get("executionHost", "")),
                        capabilities=tuple(str(item) for item in target_payload.get("capabilities", ()) or ()),
                        effects=tuple(str(item) for item in target_payload.get("effects", ()) or ()),
                        package_lock=(
                            str(target_payload.get("packageLock"))
                            if target_payload.get("packageLock") is not None
                            else None
                        ),
                        image=(
                            str(target_payload.get("image"))
                            if target_payload.get("image") is not None
                            else None
                        ),
                    )
                    manifest_set = render_target_manifests(
                        f"{name_prefix}-{target.target_id}",
                        target,
                        options=options,
                        replicas=args.replicas,
                    )
                    manifest_documents.extend(manifest_set.documents)

                manifest_set = KubernetesManifestSet(tuple(manifest_documents))
                manifest_digest = manifest_set.content_digest()
                if args.target == "helm":
                    chart_values = {
                        key: value
                        for key, value in {
                            "deploymentId": payload.get("deploymentId"),
                            "deploymentRevisionId": payload.get("deploymentRevisionId"),
                            "releaseDigest": payload.get("releaseDigest"),
                            "planHash": payload.get("planHash"),
                            "manifestDigest": manifest_digest,
                        }.items()
                        if value is not None
                    }
                    chart = render_helm_chart(
                        name_prefix,
                        manifest_set,
                        app_version=(
                            str(payload.get("deploymentRevisionId"))
                            if payload.get("deploymentRevisionId") is not None
                            else None
                        ),
                        values=chart_values,
                    )
                    output = {
                        "ok": True,
                        "target": args.target,
                        "deploymentId": payload.get("deploymentId"),
                        "deploymentRevisionId": payload.get("deploymentRevisionId"),
                        "planHash": payload.get("planHash"),
                        "manifestDigest": manifest_digest,
                        "chartName": chart.chart_name,
                        "chartDigest": chart.content_digest(),
                        "files": chart.file_map(),
                    }
                    if args.json:
                        print(json.dumps(output, indent=2, sort_keys=True))
                    else:
                        for file in chart.files:
                            print(f"# Source: {file.path}")
                            print(file.content, end="" if file.content.endswith("\n") else "\n")
                    return 0

                output = {
                    "ok": True,
                    "target": args.target,
                    "deploymentId": payload.get("deploymentId"),
                    "deploymentRevisionId": payload.get("deploymentRevisionId"),
                    "planHash": payload.get("planHash"),
                    "manifestDigest": manifest_digest,
                    "manifests": manifest_set.documents,
                }
                if args.json:
                    print(json.dumps(output, indent=2, sort_keys=True))
                else:
                    print(yaml.safe_dump_all(manifest_set.documents, sort_keys=True), end="")
                return 0
            except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                if args.json:
                    print(json.dumps({"ok": False, "error": str(error)}, indent=2, sort_keys=True))
                else:
                    print(f"FAIL {error}")
                return 1
        if args.deploy_command == "plan":
            assert release is not None
            try:
                release.validate_production_pins()
                deployment_documents = [
                    document
                    for document in release_documents
                    if document.get("kind") == "GraphDeployment"
                ]
                if len(deployment_documents) != 1:
                    raise ValueError(
                        f"expected one GraphDeployment document, found {len(deployment_documents)}"
                    )
                deployment_document = deployment_documents[0]
                metadata = deployment_document.get("metadata", {})
                spec = deployment_document.get("spec", {})
                if not isinstance(metadata, Mapping) or not isinstance(spec, Mapping):
                    raise ValueError(
                        "GraphDeployment documents require metadata and spec mappings"
                    )
                deployment_id = str(_field(metadata, "name", default="")).strip()
                if not deployment_id:
                    raise ValueError("GraphDeployment metadata.name is required")

                release_ref = _field(spec, "releaseRef", "release_ref", default={})
                if not isinstance(release_ref, Mapping):
                    raise ValueError("GraphDeployment spec.releaseRef must be a mapping")
                release_name = _field(release_ref, "name")
                if release_name is not None and str(release_name) != release.name:
                    raise ValueError(
                        f"GraphDeployment releaseRef.name {release_name!r} "
                        f"does not match {release.name!r}"
                    )
                release_digest = _field(release_ref, "digest")
                if (
                    release_digest is not None
                    and str(release_digest) != release.content_digest()
                ):
                    raise ValueError(
                        f"GraphDeployment releaseRef.digest {release_digest!r} "
                        f"does not match {release.content_digest()!r}"
                    )

                graph_name_value = args.graph or _field(
                    spec,
                    "graph",
                    "graphName",
                    "graph_name",
                )
                if graph_name_value is None:
                    if len(release.graphs) != 1:
                        raise ValueError(
                            "GraphDeployment requires --graph when the release does not contain exactly one graph"
                        )
                    graph_name_value = next(iter(release.graphs))
                graph_name = str(graph_name_value)
                if graph_name not in release.graphs:
                    raise ValueError(
                        f"GraphRelease {release.name!r} has no graph {graph_name!r}"
                    )

                deployment = GraphDeployment(
                    deployment_id=deployment_id,
                    release=release,
                    graph_name=graph_name,
                    deployment_revision_id=args.revision,
                    environment=str(
                        _field(spec, "environment", "profile", default="local")
                    ),
                )
                targets_data = _field(spec, "targets", default={})
                if not isinstance(targets_data, Mapping):
                    raise ValueError("GraphDeployment spec.targets must be a mapping")
                target_kind_names = {
                    "service": "service",
                    "workerPool": "worker_pool",
                    "worker_pool": "worker_pool",
                    "jobPool": "job_pool",
                    "job_pool": "job_pool",
                    "sandboxPool": "sandbox_pool",
                    "sandbox_pool": "sandbox_pool",
                    "statefulService": "stateful_service",
                    "stateful_service": "stateful_service",
                    "external": "external",
                }
                for target_id, target_data in targets_data.items():
                    if not isinstance(target_data, Mapping):
                        raise ValueError(
                            f"GraphDeployment target {target_id!r} must be a mapping"
                        )
                    raw_kind = str(_field(target_data, "kind", default=""))
                    target_kind = target_kind_names.get(raw_kind)
                    if target_kind is None:
                        raise ValueError(
                            f"GraphDeployment target {target_id!r} has unknown kind {raw_kind!r}"
                        )
                    execution_host = str(
                        _field(
                            target_data,
                            "executionHost",
                            "execution_host",
                            default="",
                        )
                    )
                    if not execution_host:
                        raise ValueError(
                            f"GraphDeployment target {target_id!r} requires executionHost"
                        )
                    accepts = _field(target_data, "accepts", default={})
                    if not isinstance(accepts, Mapping):
                        raise ValueError(
                            f"GraphDeployment target {target_id!r} accepts must be a mapping"
                        )
                    target = ExecutionTarget(
                        target_id=str(target_id),
                        kind=target_kind,
                        execution_host=execution_host,
                        capabilities=_tuple_field(
                            accepts,
                            "capabilities",
                        )
                        or _tuple_field(target_data, "capabilities"),
                        effects=_tuple_field(accepts, "effects")
                        or _tuple_field(target_data, "effects"),
                        package_lock=(
                            str(
                                _field(
                                    target_data,
                                    "packageLock",
                                    "package_lock",
                                )
                            )
                            if _field(
                                target_data,
                                "packageLock",
                                "package_lock",
                            )
                            is not None
                            else None
                        ),
                        image=(
                            str(_field(target_data, "image"))
                            if _field(target_data, "image") is not None
                            else None
                        ),
                    )
                    deployment = deployment.with_target(target)

                coordinator = _field(spec, "coordinator", default={})
                if coordinator is not None and not isinstance(coordinator, Mapping):
                    raise ValueError("GraphDeployment spec.coordinator must be a mapping")
                default_target = (
                    str(_field(coordinator, "target"))
                    if isinstance(coordinator, Mapping)
                    and _field(coordinator, "target") is not None
                    else None
                )
                placements_data = _field(spec, "placements", default=())
                if not isinstance(placements_data, list):
                    raise ValueError("GraphDeployment spec.placements must be a list")
                selector_kinds = {
                    "nodes": "nodes",
                    "executionGroups": "execution_groups",
                    "execution_groups": "execution_groups",
                    "blocks": "blocks",
                    "blockNamespaces": "blocks",
                    "block_namespaces": "blocks",
                    "capabilities": "capabilities",
                    "effects": "effects",
                    "executionClasses": "execution_classes",
                    "execution_classes": "execution_classes",
                }
                for index, placement_data in enumerate(placements_data):
                    if not isinstance(placement_data, Mapping):
                        raise ValueError(
                            f"GraphDeployment placement {index} must be a mapping"
                        )
                    selector_data = _field(placement_data, "select", "selector")
                    if not isinstance(selector_data, Mapping):
                        raise ValueError(
                            f"GraphDeployment placement {index} requires select mapping"
                        )
                    target_id = str(_field(placement_data, "target", default=""))
                    if not target_id:
                        raise ValueError(
                            f"GraphDeployment placement {index} requires target"
                        )
                    if bool(_field(selector_data, "default", default=False)):
                        if default_target is not None and default_target != target_id:
                            raise ValueError(
                                "GraphDeployment declares conflicting default targets "
                                f"{default_target!r} and {target_id!r}"
                            )
                        default_target = target_id
                        continue
                    selected = [
                        (selector_kinds[key], _tuple_field(selector_data, key))
                        for key in selector_kinds
                        if key in selector_data
                    ]
                    if len(selected) != 1:
                        raise ValueError(
                            f"GraphDeployment placement {index} must declare exactly one selector"
                        )
                    selector_kind, selector_values = selected[0]
                    if not selector_values:
                        raise ValueError(
                            f"GraphDeployment placement {index} selector cannot be empty"
                        )
                    deployment = deployment.with_placement(
                        PlacementRule(
                            rule_id=str(
                                _field(
                                    placement_data,
                                    "ruleId",
                                    "rule_id",
                                    "id",
                                    default=f"placement-{index + 1}",
                                )
                            ),
                            selector=PlacementSelector(
                                selector_kind,
                                selector_values,
                            ),
                            target_id=target_id,
                        )
                    )
                if default_target is not None:
                    if default_target not in deployment.targets:
                        raise ValueError(
                            f"GraphDeployment default target {default_target!r} is not defined"
                        )
                    deployment = deployment.with_default_target(default_target)

                package_lock_hash = _field(
                    spec,
                    "packageLockHash",
                    "package_lock_hash",
                )
                plan = deployment.to_physical_plan(
                    package_lock_hash=(
                        str(package_lock_hash)
                        if package_lock_hash is not None
                        else None
                    )
                )
                deployment_spec_hash = deployment.deployment_spec_hash()
                plan_hash = plan.plan_hash()
                resolved_binding_hash_value = _field(
                    spec,
                    "resolvedBindingHash",
                    "resolved_binding_hash",
                )
                if resolved_binding_hash_value is None:
                    binding_ref = _field(spec, "bindingRef", "binding_ref", default={})
                    resolved_binding_hash = canonical_hash(binding_ref)
                else:
                    resolved_binding_hash = str(resolved_binding_hash_value)
                revision = DeploymentRevision(
                    revision_id=deployment.deployment_revision_id,
                    release_digest=plan.release_digest,
                    deployment_spec_hash=deployment_spec_hash,
                    physical_plan_hash=plan_hash,
                    resolved_binding_hash=resolved_binding_hash,
                    target_capability_hash=plan.target_capability_hash(),
                    created_at=args.created_at,
                )
                payload = {
                    "ok": True,
                    "deploymentId": deployment.deployment_id,
                    "deploymentRevisionId": deployment.deployment_revision_id,
                    "graphName": deployment.graph_name,
                    "releaseDigest": plan.release_digest,
                    "deploymentSpecHash": deployment_spec_hash,
                    "planHash": plan_hash,
                    "deploymentRevision": {
                        "revisionId": revision.revision_id,
                        "releaseDigest": revision.release_digest,
                        "deploymentSpecHash": revision.deployment_spec_hash,
                        "physicalPlanHash": revision.physical_plan_hash,
                        "resolvedBindingHash": revision.resolved_binding_hash,
                        "targetCapabilityHash": revision.target_capability_hash,
                        "createdAt": revision.created_at,
                        "contentDigest": revision.content_digest(),
                    },
                    "plan": {
                        "graphHash": plan.graph_hash,
                        "packageLockHash": plan.package_lock_hash,
                        "defaultTarget": plan.default_target,
                        "targets": {
                            target_id: {
                                "kind": target.kind,
                                "executionHost": target.execution_host,
                                "capabilities": list(target.capabilities),
                                "effects": list(target.effects),
                                "packageLock": target.package_lock,
                                "image": target.image,
                            }
                            for target_id, target in sorted(plan.targets.items())
                        },
                        "placements": [
                            {
                                "ruleId": placement.rule_id,
                                "selector": {
                                    "kind": placement.selector.kind,
                                    "values": list(placement.selector.values),
                                },
                                "target": placement.target_id,
                            }
                            for placement in plan.placements
                        ],
                    },
                }
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(
                        f"{deployment.deployment_id} {plan.plan_hash()} "
                        f"release={plan.release_digest} revision={plan.deployment_revision_id}"
                    )
                return 0
            except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
                if args.json:
                    print(
                        json.dumps(
                            {"ok": False, "error": str(error)},
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(f"deploy plan error: {error}")
                return 1
        deploy_parser.print_help()
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
