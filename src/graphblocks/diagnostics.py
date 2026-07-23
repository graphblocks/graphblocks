from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    path: str = "$"
    severity: Severity = "error"

    def __post_init__(self) -> None:
        for field_name in ("code", "message", "path"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"diagnostic {field_name} must be a string")
            if not value.strip():
                raise ValueError(f"diagnostic {field_name} must not be empty")
            if value != value.strip():
                raise ValueError(
                    f"diagnostic {field_name} must not contain surrounding whitespace"
                )
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(f"diagnostic {field_name} must contain valid Unicode scalar values") from error
        if not isinstance(self.severity, str) or self.severity not in ("error", "warning", "info"):
            raise ValueError(f"diagnostic severity has invalid value {self.severity!r}")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticSet:
    diagnostics: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        if isinstance(self.diagnostics, (str, bytes)):
            raise ValueError("diagnostic set diagnostics must be a collection")
        try:
            diagnostics = tuple(self.diagnostics)
        except (TypeError, RuntimeError) as error:
            raise ValueError("diagnostic set diagnostics must be a collection") from error
        if any(not isinstance(item, Diagnostic) for item in diagnostics):
            raise ValueError("diagnostic set diagnostics must contain Diagnostic records")
        object.__setattr__(self, "diagnostics", diagnostics)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def to_list(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.diagnostics]
