#!/usr/bin/env python3
"""Project independent readiness axes from the stable release matrix."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = PurePosixPath("docs/project/stable-release-matrix.yaml")
START_MARKER = "<!-- BEGIN GENERATED READINESS AXES -->"
END_MARKER = "<!-- END GENERATED READINESS AXES -->"
EXPECTED_AXIS_IDS = (
    "supply-chain",
    "api",
    "runtime-security",
    "durability",
    "adapters",
)
AXIS_FIELDS = {
    "id",
    "label",
    "readiness",
    "primaryGates",
    "targetReleaseClaim",
    "blocksTargetRelease",
    "shippedP0P1RemainBlocking",
    "claimBoundary",
}


def render_readiness_projection(root: Path) -> tuple[Path, str]:
    matrix_file = root / MATRIX_PATH
    matrix = yaml.safe_load(matrix_file.read_text(encoding="utf-8"))
    if not isinstance(matrix, dict):
        raise ValueError("stable release matrix must be an object")
    readiness = matrix.get("readinessAxes")
    if not isinstance(readiness, dict) or set(readiness) != {
        "formatVersion",
        "generatedBy",
        "statusDocument",
        "axes",
    }:
        raise ValueError("readinessAxes must use the closed version 1 contract")
    if type(readiness["formatVersion"]) is not int or readiness["formatVersion"] != 1:
        raise ValueError("readinessAxes.formatVersion must equal 1")
    if readiness["generatedBy"] != "tools/generate_status_readiness.py":
        raise ValueError("readinessAxes.generatedBy is not this generator")

    status_value = readiness["statusDocument"]
    if type(status_value) is not str or not status_value:
        raise ValueError("readinessAxes.statusDocument must be a non-empty path")
    status_relative = PurePosixPath(status_value)
    if (
        status_relative.is_absolute()
        or "\\" in status_value
        or ".." in status_relative.parts
        or status_relative.as_posix() != status_value
    ):
        raise ValueError("readinessAxes.statusDocument must be a safe relative path")
    status_path = root / status_relative
    if not status_path.is_file():
        raise ValueError("readinessAxes.statusDocument does not exist")

    raw_gates = matrix.get("releaseGates")
    if not isinstance(raw_gates, list):
        raise ValueError("releaseGates must be an array")
    gate_ids: set[str] = set()
    gates_by_id: dict[str, dict[str, object]] = {}
    for gate in raw_gates:
        if not isinstance(gate, dict) or type(gate.get("id")) is not str:
            raise ValueError("releaseGates contains an invalid gate")
        gate_id = gate["id"]
        if not gate_id or gate_id in gate_ids:
            raise ValueError("releaseGates contains an empty or duplicate id")
        gate_ids.add(gate_id)
        gates_by_id[gate_id] = gate

    axes = readiness["axes"]
    if not isinstance(axes, list) or len(axes) != len(EXPECTED_AXIS_IDS):
        raise ValueError("readinessAxes.axes must contain the five required axes")
    rows: list[str] = []
    for expected_id, axis in zip(EXPECTED_AXIS_IDS, axes, strict=True):
        if not isinstance(axis, dict) or set(axis) != AXIS_FIELDS:
            raise ValueError(f"readiness axis {expected_id} has an invalid shape")
        if axis["id"] != expected_id:
            raise ValueError("readiness axes are missing, reordered, or relabeled")
        for field in ("label", "readiness", "targetReleaseClaim", "claimBoundary"):
            value = axis[field]
            if (
                type(value) is not str
                or not value
                or any(forbidden in value for forbidden in ("|", "\n", "\r"))
            ):
                raise ValueError(
                    f"readiness axis {expected_id}.{field} must be table-safe text"
                )
        if (
            type(axis["blocksTargetRelease"]) is not bool
            or type(axis["shippedP0P1RemainBlocking"]) is not bool
            or axis["shippedP0P1RemainBlocking"] is not True
        ):
            raise ValueError(
                f"readiness axis {expected_id} must declare exact tag and P0/P1 effects"
            )
        primary_gates = axis["primaryGates"]
        if (
            not isinstance(primary_gates, list)
            or not primary_gates
            or any(type(gate_id) is not str or not gate_id for gate_id in primary_gates)
            or len(primary_gates) != len(set(primary_gates))
        ):
            raise ValueError(
                f"readiness axis {expected_id}.primaryGates must be unique gate ids"
            )
        missing_gates = set(primary_gates) - gate_ids
        if missing_gates:
            raise ValueError(
                f"readiness axis {expected_id} names unknown primary gates"
            )
        blocking_primary_gates = [
            gate_id
            for gate_id in primary_gates
            if gates_by_id[gate_id].get("blocksTargetRelease") is True
        ]
        if not axis["blocksTargetRelease"] and blocking_primary_gates:
            raise ValueError(
                f"readiness axis {expected_id} cannot exclude target release while "
                "a primary gate blocks it"
            )
        rendered_gates = ", ".join(f"`{gate_id}`" for gate_id in primary_gates)
        tag_effect = (
            "blocks target release"
            if axis["blocksTargetRelease"]
            else "axis excluded; shipped P0/P1 still block"
        )
        rows.append(
            f"| {axis['label']} (`{expected_id}`) | `{axis['readiness']}` | "
            f"`{axis['targetReleaseClaim']}`; {tag_effect} | {rendered_gates} | "
            f"{axis['claimBoundary']} |"
        )

    projection = "\n".join(
        (
            START_MARKER,
            "<!-- Generated by tools/generate_status_readiness.py from "
            "docs/project/stable-release-matrix.yaml. -->",
            "| Readiness axis | Current state | 1.0 claim and tag effect | Primary gate(s) | Claim boundary |",
            "| --- | --- | --- | --- | --- |",
            *rows,
            END_MARKER,
        )
    )
    return status_path, projection


def projected_status(root: Path) -> tuple[Path, str]:
    status_path, projection = render_readiness_projection(root)
    current = status_path.read_text(encoding="utf-8")
    if current.count(START_MARKER) != 1 or current.count(END_MARKER) != 1:
        raise ValueError("status document must contain one readiness marker pair")
    prefix, marked = current.split(START_MARKER, 1)
    _, suffix = marked.split(END_MARKER, 1)
    return status_path, prefix + projection + suffix


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root containing docs/project",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in status projection is stale",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    try:
        status_path, expected = projected_status(root)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        print(f"status readiness generation failed: {error}", file=sys.stderr)
        return 2
    current = status_path.read_text(encoding="utf-8")
    if args.check:
        if current != expected:
            print(
                "generated readiness projection is stale; run "
                "python tools/generate_status_readiness.py",
                file=sys.stderr,
            )
            return 1
        return 0
    status_path.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
