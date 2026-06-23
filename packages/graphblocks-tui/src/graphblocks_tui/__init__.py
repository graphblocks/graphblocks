from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json


class TuiContractError(ValueError):
    """Raised when a TUI screen contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sorted_str_mapping(values: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in sorted(dict(values).items())}


def _content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TuiSection:
    title: str
    rows: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise TuiContractError("section title must not be empty")
        object.__setattr__(self, "rows", _sorted_str_mapping(self.rows))

    def section_contract(self) -> dict[str, object]:
        return {
            "title": self.title,
            "rows": deepcopy(dict(self.rows)),
        }


@dataclass(frozen=True, slots=True)
class TuiCommand:
    label: str
    action: str
    key: str | None = None

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise TuiContractError("command label must not be empty")
        if not self.action.strip():
            raise TuiContractError("command action must not be empty")
        if self.key is not None and not self.key.strip():
            raise TuiContractError("command key must not be empty")

    def command_contract(self) -> dict[str, str]:
        contract = {"label": self.label, "action": self.action}
        if self.key is not None:
            contract["key"] = self.key
        return contract


@dataclass(frozen=True, slots=True)
class TuiScreen:
    name: str
    title: str
    sections: tuple[TuiSection, ...] = field(default_factory=tuple)
    commands: tuple[TuiCommand, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise TuiContractError("screen name must not be empty")
        if not self.title.strip():
            raise TuiContractError("screen title must not be empty")
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "commands", tuple(self.commands))

    def screen_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "sections": [section.section_contract() for section in self.sections],
            "commands": [command.command_contract() for command in self.commands],
        }


@dataclass(frozen=True, slots=True)
class TuiSessionSnapshot:
    screens: tuple[TuiScreen, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "screens", tuple(self.screens))

    def snapshot_contract(self) -> dict[str, object]:
        return {"screens": [screen.screen_contract() for screen in self.screens]}

    def content_digest(self) -> str:
        return _content_digest(self.snapshot_contract())


def run_status_screen(
    *,
    run_id: str,
    state: str,
    last_event: str,
    counters: Mapping[str, object] | None = None,
) -> TuiScreen:
    if not run_id.strip():
        raise TuiContractError("run_id must not be empty")
    if not state.strip():
        raise TuiContractError("state must not be empty")
    if not last_event.strip():
        raise TuiContractError("last_event must not be empty")
    return TuiScreen(
        name="run-status",
        title=f"Run {run_id}",
        sections=(
            TuiSection("State", {"state": state, "last_event": last_event}),
            TuiSection("Counters", counters or {}),
        ),
        commands=(
            TuiCommand("Refresh", "refresh", "r"),
            TuiCommand("Cancel", "cancel", "c"),
        ),
    )


__all__ = [
    "TuiCommand",
    "TuiContractError",
    "TuiScreen",
    "TuiSection",
    "TuiSessionSnapshot",
    "run_status_screen",
]
