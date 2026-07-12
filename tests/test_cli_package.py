from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks import __version__


ROOT = Path(__file__).parents[1]


def test_cli_package_exposes_main_entrypoint(monkeypatch, capsys) -> None:
    graphblocks_cli = importlib.import_module("graphblocks.cli")

    assert graphblocks_cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
