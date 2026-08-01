from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
import textwrap
import tomllib

from packaging.requirements import Requirement
import pytest
import yaml

import graphblocks
from graphblocks._root_compat import _ROOT_COMPAT_EXPORTS
from tools.verify_wheelhouse import validate_base_graphblocks_install


ROOT = Path(__file__).parents[1]
BOUNDARY_PATH = ROOT / "compatibility" / "python-package-boundaries.yaml"


def _package_boundary() -> dict[str, object]:
    boundary = yaml.safe_load(BOUNDARY_PATH.read_text(encoding="utf-8"))
    assert isinstance(boundary, dict)
    return boundary


def test_base_dependency_and_extra_boundaries_are_exact() -> None:
    boundary = _package_boundary()
    dependencies = boundary["dependencies"]
    assert isinstance(dependencies, dict)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == dependencies["base"]
    optional = pyproject["project"]["optional-dependencies"]
    feature_extras = dependencies["optionalFeatureExtras"]
    assert isinstance(feature_extras, dict)
    assert {
        name: optional[name] for name in feature_extras
    } == feature_extras
    assert set(optional) - set(feature_extras) == set(
        dependencies["developmentExtras"]
    )


def test_stable_owners_and_preview_default_are_explicit() -> None:
    boundary = _package_boundary()
    root_facade = boundary["rootFacade"]
    assert isinstance(root_facade, dict)
    preview_compatibility = root_facade["previewCompatibility"]
    assert isinstance(preview_compatibility, dict)
    surface = yaml.safe_load(
        (ROOT / "compatibility" / "stable-python-surface.yaml").read_text(
            encoding="utf-8"
        )
    )
    stable_exports = tuple(
        dict.fromkeys(
            entry["path"].split(".", 2)[1] for entry in surface["symbols"]
        )
    )
    module_tiers = boundary["moduleTiers"]
    assert isinstance(module_tiers, dict)

    assert tuple(graphblocks.__all__) == stable_exports
    assert {
        _ROOT_COMPAT_EXPORTS[name] for name in stable_exports
    } == set(module_tiers["stableSymbolOwnerModules"])
    assert module_tiers["moduleOwnershipDoesNotPromoteWholeModule"] is True
    assert module_tiers["unlistedPublicModuleTier"] == "preview"
    assert module_tiers["integrationsTier"] == "preview"
    assert preview_compatibility == {
        "coldDiscoverable": False,
        "typedAsStable": False,
        "explicitNamespaces": "defining-leaf-modules",
        "guidance": "import-preview-from-defining-leaf-module",
    }


def test_lazy_root_export_catalog_is_exact_and_resolvable() -> None:
    boundary = _package_boundary()
    root_facade = boundary["rootFacade"]
    assert isinstance(root_facade, dict)
    lazy_exports = root_facade["lazyExports"]
    assert isinstance(lazy_exports, dict)
    source_path = ROOT / lazy_exports["source"]
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_ROOT_COMPAT_EXPORTS_BY_MODULE"
            for target in node.targets
        )
    )
    exports_by_module = ast.literal_eval(assignment.value)
    flattened = {
        export: module
        for module, exports in exports_by_module.items()
        for export in exports
    }

    assert len(flattened) == sum(map(len, exports_by_module.values()))
    assert len(flattened) == lazy_exports["expectedCount"]
    assert flattened == _ROOT_COMPAT_EXPORTS
    snapshot = json.loads((ROOT / lazy_exports["snapshot"]).read_text(encoding="utf-8"))
    assert snapshot == {
        "snapshotVersion": 1,
        "targetRelease": "1.0",
        "contract": "historical-package-root-runtime-aliases",
        "aliases": [
            {"name": name, "owner": flattened[name]}
            for name in sorted(flattened)
        ],
    }
    for module in exports_by_module:
        relative_module = module.removeprefix("graphblocks.").replace(".", "/")
        assert (ROOT / "src" / "graphblocks" / f"{relative_module}.py").is_file()

    probe = textwrap.dedent(
        """
        import importlib
        import sys

        sys.path.insert(0, sys.argv[1])
        import graphblocks
        from graphblocks._root_compat import _ROOT_COMPAT_EXPORTS

        for name, module_name in _ROOT_COMPAT_EXPORTS.items():
            assert getattr(graphblocks, name) is getattr(
                importlib.import_module(module_name), name
            )
        print(len(_ROOT_COMPAT_EXPORTS))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT / "src")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == str(lazy_exports["expectedCount"])


def test_base_install_payload_validation_is_fail_closed() -> None:
    boundary = _package_boundary()
    dependencies = boundary["dependencies"]
    budgets = boundary["coldImportBudgets"]
    assert isinstance(dependencies, dict)
    assert isinstance(budgets, dict)
    root_budget = budgets["graphblocks"]
    canonical_budget = budgets["graphblocks.canonical"]
    assert isinstance(root_budget, dict)
    assert isinstance(canonical_budget, dict)
    expected_exports = list(graphblocks.__all__)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    metadata_requirements = list(dependencies["base"])
    for extra, requirements in project["optional-dependencies"].items():
        metadata_requirements.extend(
            f"{requirement}; extra == '{extra}'" for requirement in requirements
        )
    payload = {
        "canonicalModules": canonical_budget["allowedGraphblocksModules"],
        "distributionVersion": project["version"],
        "graphblocksDistributions": ["graphblocks"],
        "requirements": sorted(
            str(Requirement(requirement)) for requirement in metadata_requirements
        ),
        "rootAll": expected_exports,
        "rootAttributes": root_budget["maxRootAttributes"],
        "rootModules": root_budget["allowedGraphblocksModules"],
        "rootPublicNames": sorted(expected_exports),
        "stableResolved": expected_exports,
    }
    validation_arguments = {
        "expected_version": project["version"],
        "expected_requirements": metadata_requirements,
        "expected_root_exports": expected_exports,
        "expected_root_modules": root_budget["allowedGraphblocksModules"],
        "expected_canonical_modules": canonical_budget["allowedGraphblocksModules"],
        "max_root_attributes": root_budget["maxRootAttributes"],
    }

    assert validate_base_graphblocks_install(
        payload, **validation_arguments
    ) == payload
    invalid_values = {
        "requirements": payload["requirements"][:-1],
        "graphblocksDistributions": ["graphblocks", "graphblocks-runtime"],
        "rootPublicNames": [*payload["rootPublicNames"], "PreviewName"],
        "stableResolved": payload["stableResolved"][:-1],
        "rootAttributes": root_budget["maxRootAttributes"] + 1,
    }
    for field, invalid_value in invalid_values.items():
        invalid_payload = {**payload, field: invalid_value}
        with pytest.raises(RuntimeError):
            validate_base_graphblocks_install(
                invalid_payload, **validation_arguments
            )


def test_cold_imports_stay_within_time_memory_module_and_api_budgets() -> None:
    boundary = _package_boundary()
    protocol = boundary["coldImportProtocol"]
    assert isinstance(protocol, dict)
    assert protocol["processIsolation"] == "fresh-interpreter-per-observation"
    assert protocol["warmupRuns"] == 0
    assert protocol["measuredRuns"] == 3
    assert protocol["evaluation"] == "every-observation-must-pass"
    budgets = boundary["coldImportBudgets"]
    assert isinstance(budgets, dict)
    probe = textwrap.dedent(
        """
        import ctypes
        import importlib
        import json
        import os
        from pathlib import Path
        import sys
        import time

        def current_rss_bytes():
            if sys.platform == "win32":
                from ctypes import wintypes

                class ProcessMemoryCounters(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]

                counters = ProcessMemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                get_current_process = ctypes.windll.kernel32.GetCurrentProcess
                get_current_process.argtypes = []
                get_current_process.restype = wintypes.HANDLE
                get_process_memory_info = (
                    ctypes.windll.psapi.GetProcessMemoryInfo
                )
                get_process_memory_info.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(ProcessMemoryCounters),
                    wintypes.DWORD,
                ]
                get_process_memory_info.restype = wintypes.BOOL
                process = get_current_process()
                if not get_process_memory_info(
                    process, ctypes.byref(counters), counters.cb
                ):
                    raise OSError("GetProcessMemoryInfo failed")
                return counters.WorkingSetSize
            if sys.platform == "darwin":
                import resource

                return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            statm = Path("/proc/self/statm")
            if statm.is_file():
                resident_pages = int(statm.read_text(encoding="ascii").split()[1])
                return resident_pages * os.sysconf("SC_PAGE_SIZE")
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

        sys.path.insert(0, sys.argv[2])
        attribute_name = sys.argv[3] if len(sys.argv) == 4 else None
        if attribute_name is not None:
            imported = importlib.import_module(sys.argv[1])
        before_rss = current_rss_bytes()
        before_modules = len(sys.modules)
        started = time.perf_counter()
        if attribute_name is None:
            imported = importlib.import_module(sys.argv[1])
        else:
            getattr(imported, attribute_name)
        elapsed = time.perf_counter() - started
        payload = {
            "elapsedSeconds": elapsed,
            "rssIncreaseBytes": max(0, current_rss_bytes() - before_rss),
            "loadedModuleIncrease": len(sys.modules) - before_modules,
            "graphblocksModules": sum(
                name == "graphblocks" or name.startswith("graphblocks.")
                for name in sys.modules
            ),
            "graphblocksModuleNames": sorted(
                name
                for name in sys.modules
                if name == "graphblocks" or name.startswith("graphblocks.")
            ),
        }
        if sys.argv[1] == "graphblocks" and attribute_name is None:
            from graphblocks._root_compat import _ROOT_COMPAT_EXPORTS

            preview_aliases = set(_ROOT_COMPAT_EXPORTS) - set(imported.__all__)
            payload["rootAttributes"] = len(vars(imported))
            payload["discoverablePreviewAliases"] = len(
                preview_aliases.intersection(dir(imported))
            )
            payload["discoverablePublicNames"] = sorted(
                name for name in dir(imported) if not name.startswith("_")
            )
        print(json.dumps(payload, sort_keys=True))
        """
    )

    for module_name, raw_budget in budgets.items():
        assert isinstance(raw_budget, dict)
        observations = []
        for _repetition in range(protocol["measuredRuns"]):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    probe,
                    module_name,
                    str(ROOT / "src"),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            observations.append(json.loads(completed.stdout))

        for observation in observations:
            assert observation["elapsedSeconds"] <= raw_budget["maxElapsedSeconds"]
            assert observation["rssIncreaseBytes"] <= raw_budget["maxRssIncreaseBytes"]
            assert (
                observation["loadedModuleIncrease"]
                <= raw_budget["maxLoadedModuleIncrease"]
            )
            assert (
                observation["graphblocksModules"]
                <= raw_budget["maxGraphblocksModules"]
            )
            assert observation["graphblocksModuleNames"] == raw_budget[
                "allowedGraphblocksModules"
            ]
            if module_name == "graphblocks":
                assert observation["rootAttributes"] <= raw_budget["maxRootAttributes"]
                assert observation["discoverablePreviewAliases"] == 0
                assert observation["discoverablePublicNames"] == sorted(
                    graphblocks.__all__
                )

    stable_access_budgets = boundary["stableFirstAccessBudgets"]
    assert isinstance(stable_access_budgets, dict)
    for symbol_path, raw_budget in stable_access_budgets.items():
        assert isinstance(raw_budget, dict)
        module_name, attribute_name = symbol_path.rsplit(".", 1)
        for _repetition in range(protocol["measuredRuns"]):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    probe,
                    module_name,
                    str(ROOT / "src"),
                    attribute_name,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            observation = json.loads(completed.stdout)
            assert observation["elapsedSeconds"] <= raw_budget["maxElapsedSeconds"]
            assert observation["rssIncreaseBytes"] <= raw_budget["maxRssIncreaseBytes"]
            assert (
                observation["loadedModuleIncrease"]
                <= raw_budget["maxLoadedModuleIncrease"]
            )
            assert (
                observation["graphblocksModules"]
                <= raw_budget["maxGraphblocksModules"]
            )
