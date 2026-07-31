#!/usr/bin/env python3
"""Generate the stdlib reference inventory from the built-in plugin manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "graphblocks" / "data" / "builtin-plugin.yaml"
DOC_TARGET = ROOT / "docs" / "specification" / "reference" / "stdlib-inventory.md"
TCK_TARGET = ROOT / "tck" / "stdlib" / "inventory.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in inventory differs from the manifest",
    )
    args = parser.parse_args()

    document = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("built-in plugin manifest must be a mapping")
    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("built-in plugin manifest spec must be a mapping")
    blocks = spec.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("built-in plugin manifest blocks must be a list")
    sys.path.insert(0, str(ROOT / "src"))
    plugins = importlib.import_module("graphblocks.plugins")
    preview_catalog = plugins.builtin_block_catalog(profile="preview")
    stable_catalog = plugins.builtin_block_catalog(profile="stable")

    rows: list[str] = []
    seen: set[str] = set()
    manifest_metadata: dict[str, dict[str, object]] = {}
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError(f"built-in plugin block {index} must be a mapping")
        type_id = block.get("typeId")
        version = block.get("version")
        implementation = block.get("implementation")
        role = block.get("role")
        if (
            not isinstance(type_id, str)
            or not type_id
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or not isinstance(implementation, str)
            or not implementation
            or not isinstance(role, str)
            or not role
        ):
            raise ValueError(
                f"built-in plugin block {index} has invalid identity metadata"
            )
        block_id = f"{type_id}@{version}"
        if block_id in seen:
            raise ValueError(f"built-in plugin manifest duplicates {block_id}")
        seen.add(block_id)
        manifest_metadata[block_id] = {
            "implementation": implementation,
            "role": role,
        }
        inputs = block.get("inputs", [])
        outputs = block.get("outputs", [])
        if not isinstance(inputs, list) or not isinstance(outputs, list):
            raise ValueError(f"built-in plugin block {block_id} ports must be lists")
        input_names = [
            port.get("name")
            for port in inputs
            if isinstance(port, dict) and isinstance(port.get("name"), str)
        ]
        output_names = [
            port.get("name")
            for port in outputs
            if isinstance(port, dict) and isinstance(port.get("name"), str)
        ]
        if len(input_names) != len(inputs) or len(output_names) != len(outputs):
            raise ValueError(f"built-in plugin block {block_id} ports require names")
        preview_descriptor = preview_catalog.get(block_id)
        if preview_descriptor is None:
            raise ValueError(f"preview stdlib catalog omits manifest block {block_id}")
        if tuple(port.name for port in preview_descriptor.inputs) != tuple(
            input_names
        ) or tuple(port.name for port in preview_descriptor.outputs) != tuple(
            output_names
        ):
            raise ValueError(
                f"preview stdlib catalog ports drift from manifest block {block_id}"
            )
        stable_descriptor = stable_catalog.get(block_id)
        profiles = "`preview`"
        config_contract = "`preview`"
        if stable_descriptor is not None:
            profiles = "`stable`, `preview`"
            if stable_descriptor.config_schema == preview_descriptor.config_schema:
                config_contract = "`shared`"
            else:
                config_contract = "`profile-specific`"
        rows.append(
            "| `{}` | `{}` | `{}` | {} | {} | {} | {} |".format(
                block_id,
                implementation,
                role,
                profiles,
                config_contract,
                ", ".join(f"`{name}`" for name in input_names) or "—",
                ", ".join(f"`{name}`" for name in output_names) or "—",
            )
        )
    if seen != set(preview_catalog.descriptors):
        raise ValueError(
            "preview stdlib catalog and built-in manifest inventory differ"
        )

    profile_contracts: dict[str, dict[str, object]] = {}
    for profile, catalog in (
        ("preview", preview_catalog),
        ("stable", stable_catalog),
    ):
        resolved_blocks = []
        for descriptor in catalog.to_blocks():
            block_id = f"{descriptor['typeId']}@{descriptor['version']}"
            metadata = manifest_metadata.get(block_id)
            if metadata is None:
                raise ValueError(
                    f"{profile} stdlib catalog block {block_id} is absent "
                    "from the manifest"
                )
            resolved_blocks.append(
                {
                    "blockId": block_id,
                    "implementation": metadata["implementation"],
                    "role": metadata["role"],
                    "descriptor": descriptor,
                }
            )
        profile_contracts[profile] = {"blocks": resolved_blocks}
    tck_inventory = {
        "inventoryVersion": 1,
        "manifestSha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "profiles": profile_contracts,
        "source": SOURCE.relative_to(ROOT).as_posix(),
    }
    rendered_tck = (
        json.dumps(
            tck_inventory,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    rendered = "\n".join(
        [
            "# Standard-library inventory",
            "",
            "<!-- Generated by tools/generate_stdlib_inventory.py. Do not edit by hand. -->",
            "",
            "The authoritative source is "
            "[`builtin-plugin.yaml`](../../../src/graphblocks/data/builtin-plugin.yaml). "
            "Python runtime completeness checks bind handlers to its block and "
            "implementation identifiers. The generated "
            "[TCK inventory](../../../tck/stdlib/inventory.json) exposes resolved "
            "stable and preview contracts for cross-runtime parity. Profile "
            "membership and descriptor overlays are resolved through "
            "`builtin_block_catalog`; "
            "`profile-specific` marks a stable descriptor that intentionally differs "
            "from its preview contract.",
            "",
            "| Block | Implementation | Role | Profiles | Config contract | Inputs | Outputs |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *rows,
            "",
        ]
    )
    if args.check:
        stale_targets = []
        if (
            not DOC_TARGET.is_file()
            or DOC_TARGET.read_text(encoding="utf-8") != rendered
        ):
            stale_targets.append(DOC_TARGET.relative_to(ROOT).as_posix())
        if (
            not TCK_TARGET.is_file()
            or TCK_TARGET.read_text(encoding="utf-8") != rendered_tck
        ):
            stale_targets.append(TCK_TARGET.relative_to(ROOT).as_posix())
        if stale_targets:
            print(
                "stdlib inventories are stale: "
                + ", ".join(stale_targets)
                + "; run python tools/generate_stdlib_inventory.py",
                file=sys.stderr,
            )
            return 1
        return 0

    TCK_TARGET.parent.mkdir(parents=True, exist_ok=True)
    DOC_TARGET.write_text(rendered, encoding="utf-8")
    TCK_TARGET.write_text(rendered_tck, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
