from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks import __version__


ROOT = Path(__file__).parents[1]


def test_cli_package_exposes_main_entrypoint(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-cli" / "src"))
    graphblocks_cli = importlib.import_module("graphblocks_cli")

    assert graphblocks_cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
