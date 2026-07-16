from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from graphblocks.deployment import ExecutionTarget


class TerraformBridgeError(ValueError):
    """Raised when Terraform bridge contracts are invalid."""


class TerraformOutputMissingError(TerraformBridgeError):
    def __init__(self, output_name: str) -> None:
        self.output_name = output_name
        super().__init__(f"required Terraform output {output_name!r} is missing")


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class TerraformVariable:
    name: str
    value: object
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise TerraformBridgeError("variable name must not be empty")
        try:
            _canonical_dumps(self.value)
        except (TypeError, ValueError) as error:
            raise TerraformBridgeError(f"variable {self.name!r} must be JSON-serializable") from error

    def canonical_value(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": deepcopy(self.value),
            "sensitive": self.sensitive,
        }


@dataclass(frozen=True, slots=True)
class TerraformOutputBinding:
    output_name: str
    graphblocks_key: str
    required: bool = True
    secret_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.output_name.strip():
            raise TerraformBridgeError("output_name must not be empty")
        if not self.graphblocks_key.strip():
            raise TerraformBridgeError("graphblocks_key must not be empty")
        if self.secret_ref is not None and not self.secret_ref.strip():
            raise TerraformBridgeError("secret_ref must not be empty")

    def canonical_value(self) -> dict[str, object]:
        return {
            "output_name": self.output_name,
            "graphblocks_key": self.graphblocks_key,
            "required": self.required,
            "secret_ref": self.secret_ref,
        }


@dataclass(frozen=True, slots=True)
class TerraformInfrastructureRequirement:
    target_id: str
    target_kind: str
    execution_host: str
    resource_type: str
    resource_name: str
    attributes: Mapping[str, object] = field(default_factory=dict)
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    effects: tuple[str, ...] = field(default_factory=tuple)
    image: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("target_id", self.target_id),
            ("target_kind", self.target_kind),
            ("execution_host", self.execution_host),
            ("resource_type", self.resource_type),
            ("resource_name", self.resource_name),
        ):
            if not value.strip():
                raise TerraformBridgeError(f"{field_name} must not be empty")
        attributes = {str(key): deepcopy(value) for key, value in sorted(dict(self.attributes).items())}
        try:
            _canonical_dumps(attributes)
        except (TypeError, ValueError) as error:
            raise TerraformBridgeError("requirement attributes must be JSON-serializable") from error
        object.__setattr__(self, "attributes", attributes)
        object.__setattr__(self, "capabilities", tuple(sorted({str(value) for value in self.capabilities})))
        object.__setattr__(self, "effects", tuple(sorted({str(value) for value in self.effects})))

    @classmethod
    def for_execution_target(
        cls,
        target: ExecutionTarget,
        *,
        resource_type: str,
        resource_name: str,
        attributes: Mapping[str, object] | None = None,
    ) -> TerraformInfrastructureRequirement:
        return cls(
            target_id=target.target_id,
            target_kind=target.kind,
            execution_host=target.execution_host,
            resource_type=resource_type,
            resource_name=resource_name,
            attributes=attributes or {},
            capabilities=target.capabilities,
            effects=target.effects,
            image=target.image,
        )

    def canonical_value(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "execution_host": self.execution_host,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "attributes": deepcopy(dict(self.attributes)),
            "capabilities": list(self.capabilities),
            "effects": list(self.effects),
            "image": self.image,
        }


@dataclass(frozen=True, slots=True)
class TerraformBridgeSpec:
    workspace: str
    variables: tuple[TerraformVariable, ...] = field(default_factory=tuple)
    output_bindings: tuple[TerraformOutputBinding, ...] = field(default_factory=tuple)
    requirements: tuple[TerraformInfrastructureRequirement, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.workspace.strip():
            raise TerraformBridgeError("workspace must not be empty")

        variables = tuple(sorted(self.variables, key=lambda variable: variable.name))
        variable_names = [variable.name for variable in variables]
        if len(variable_names) != len(set(variable_names)):
            raise TerraformBridgeError("variable names must be unique")
        object.__setattr__(self, "variables", variables)

        output_bindings = tuple(sorted(self.output_bindings, key=lambda binding: binding.output_name))
        output_names = [binding.output_name for binding in output_bindings]
        if len(output_names) != len(set(output_names)):
            raise TerraformBridgeError("output binding names must be unique")
        graphblocks_keys = [binding.graphblocks_key for binding in output_bindings]
        if len(graphblocks_keys) != len(set(graphblocks_keys)):
            raise TerraformBridgeError("output binding graphblocks_key values must be unique")
        object.__setattr__(self, "output_bindings", output_bindings)

        object.__setattr__(
            self,
            "requirements",
            tuple(sorted(self.requirements, key=lambda requirement: _canonical_dumps(requirement.canonical_value()))),
        )

    def tfvars_json(self) -> str:
        return _canonical_dumps({variable.name: variable.value for variable in self.variables})

    def requirement_contracts(self) -> list[dict[str, object]]:
        return [requirement.canonical_value() for requirement in self.requirements]

    def materialize_outputs(self, terraform_outputs: Mapping[str, object]) -> dict[str, object]:
        materialized: dict[str, object] = {}
        for binding in self.output_bindings:
            if binding.output_name not in terraform_outputs:
                if binding.required:
                    raise TerraformOutputMissingError(binding.output_name)
                continue
            raw_value = terraform_outputs[binding.output_name]
            if binding.secret_ref is not None:
                materialized[binding.graphblocks_key] = {"secretRef": binding.secret_ref}
                continue
            if isinstance(raw_value, Mapping) and "value" in raw_value:
                value = raw_value["value"]
            else:
                value = raw_value
            materialized[binding.graphblocks_key] = deepcopy(value)
        return {key: materialized[key] for key in sorted(materialized)}

    def materialize_binding_document(
        self,
        name: str,
        terraform_outputs: Mapping[str, object],
        *,
        api_version: str = "graphblocks.ai/v1alpha1",
        kind: str = "Binding",
    ) -> dict[str, object]:
        if not name.strip():
            raise TerraformBridgeError("binding document name must not be empty")
        if not api_version.strip():
            raise TerraformBridgeError("binding document api_version must not be empty")
        if not kind.strip():
            raise TerraformBridgeError("binding document kind must not be empty")

        spec: dict[str, object] = {}
        for graphblocks_key, value in self.materialize_outputs(terraform_outputs).items():
            path = graphblocks_key.split(".")
            if not path or any(not part for part in path):
                raise TerraformBridgeError(f"invalid graphblocks binding path {graphblocks_key!r}")
            current = spec
            for part in path[:-1]:
                existing = current.setdefault(part, {})
                if not isinstance(existing, dict):
                    raise TerraformBridgeError(f"conflicting graphblocks binding path {graphblocks_key!r}")
                current = existing
            leaf = path[-1]
            if leaf in current:
                raise TerraformBridgeError(f"duplicate graphblocks binding path {graphblocks_key!r}")
            current[leaf] = deepcopy(value)

        return {
            "apiVersion": api_version,
            "kind": kind,
            "metadata": {
                "name": name,
                "annotations": {
                    "graphblocks.ai/terraform-bridge-digest": self.content_digest(),
                    "graphblocks.ai/terraform-workspace": self.workspace,
                },
            },
            "spec": spec,
        }

    def content_digest(self) -> str:
        value = {
            "workspace": self.workspace,
            "variables": [variable.canonical_value() for variable in self.variables],
            "output_bindings": [binding.canonical_value() for binding in self.output_bindings],
            "requirements": [requirement.canonical_value() for requirement in self.requirements],
        }
        return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


__all__ = [
    "TerraformBridgeError",
    "TerraformBridgeSpec",
    "TerraformInfrastructureRequirement",
    "TerraformOutputBinding",
    "TerraformOutputMissingError",
    "TerraformVariable",
]
