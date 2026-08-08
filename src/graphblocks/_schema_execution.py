from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from ._canonical_reference import canonical_dumps


@dataclass(frozen=True, slots=True)
class SchemaExecutionPolicy:
    """Resource ceilings applied before a JSON Schema can be executed."""

    max_schema_bytes: int = 1_048_576
    max_nodes: int = 10_000
    max_depth: int = 64
    max_pattern_bytes: int = 256
    allow_pattern: bool = True
    allow_remote_ref: bool = False
    max_validation_steps: int = 20_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_schema_bytes",
            "max_nodes",
            "max_depth",
            "max_pattern_bytes",
            "max_validation_steps",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if not isinstance(self.allow_pattern, bool):
            raise TypeError("allow_pattern must be a boolean")
        if not isinstance(self.allow_remote_ref, bool):
            raise TypeError("allow_remote_ref must be a boolean")


@dataclass(frozen=True, slots=True)
class SchemaExecutionMetrics:
    schema_bytes: int
    nodes: int
    depth: int
    validation_steps: int


class SchemaExecutionPolicyError(ValueError):
    """Raised when a schema exceeds its execution trust boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


DEFAULT_SCHEMA_EXECUTION_POLICY = SchemaExecutionPolicy()
UNTRUSTED_SCHEMA_EXECUTION_POLICY = SchemaExecutionPolicy(allow_pattern=False)


def _schema_shape_metrics(
    schema: Mapping[str, object] | bool,
    *,
    policy: SchemaExecutionPolicy,
    owner: str,
) -> tuple[int, int]:
    pending: list[tuple[object, int, bool]] = [(schema, 0, False)]
    active_containers: set[int] = set()
    nodes = 0
    maximum_depth = 0
    while pending:
        candidate, depth, leaving = pending.pop()
        if leaving:
            active_containers.remove(id(candidate))
            continue
        nodes += 1
        if nodes > policy.max_nodes:
            raise SchemaExecutionPolicyError(
                "max_nodes",
                f"{owner} must not contain more than {policy.max_nodes} JSON nodes",
            )
        if depth > policy.max_depth:
            raise SchemaExecutionPolicyError(
                "max_depth",
                f"{owner} nesting must not exceed {policy.max_depth} levels",
            )
        maximum_depth = max(maximum_depth, depth)
        if isinstance(candidate, Mapping):
            identity = id(candidate)
            if identity in active_containers:
                raise SchemaExecutionPolicyError(
                    "recursive_schema",
                    f"{owner} must not contain recursive values",
                )
            try:
                children = tuple(candidate.values())
            except Exception as error:
                raise SchemaExecutionPolicyError(
                    "unstable_schema",
                    f"{owner} must contain stable JSON values",
                ) from error
            active_containers.add(identity)
            pending.append((candidate, depth, True))
            pending.extend((value, depth + 1, False) for value in children)
        elif isinstance(candidate, Sequence) and not isinstance(
            candidate,
            (str, bytes, bytearray),
        ):
            identity = id(candidate)
            if identity in active_containers:
                raise SchemaExecutionPolicyError(
                    "recursive_schema",
                    f"{owner} must not contain recursive values",
                )
            try:
                children = tuple(candidate)
            except Exception as error:
                raise SchemaExecutionPolicyError(
                    "unstable_schema",
                    f"{owner} must contain stable JSON values",
                ) from error
            active_containers.add(identity)
            pending.append((candidate, depth, True))
            pending.extend((value, depth + 1, False) for value in children)
    return nodes, maximum_depth


def _pattern_uses_backtracking_construct(pattern: str) -> bool:
    """Conservatively reject regex constructs with non-linear risk."""

    escaped = False
    in_character_class = False
    for index, character in enumerate(pattern):
        if escaped:
            if character.isdigit() or character in {"g", "k"}:
                return True
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "[" and not in_character_class:
            in_character_class = True
            continue
        if character == "]" and in_character_class:
            in_character_class = False
            continue
        if in_character_class:
            continue
        if character == "(" and index + 1 < len(pattern) and pattern[index + 1] == "?":
            return True
        if character == ")" and index + 1 < len(pattern):
            following = pattern[index + 1]
            if following in {"*", "+", "?", "{"}:
                return True
    return False


def _check_pattern(
    pattern: str,
    *,
    keyword: str,
    policy: SchemaExecutionPolicy,
    owner: str,
) -> None:
    if not policy.allow_pattern:
        raise SchemaExecutionPolicyError(
            "pattern_disabled",
            f"{owner} regular-expression keyword {keyword!r} is disabled",
        )
    pattern_bytes = len(pattern.encode("utf-8"))
    if pattern_bytes > policy.max_pattern_bytes:
        raise SchemaExecutionPolicyError(
            "max_pattern_bytes",
            f"{owner} regular-expression keyword {keyword!r} must not exceed "
            f"{policy.max_pattern_bytes} UTF-8 bytes",
        )
    if _pattern_uses_backtracking_construct(pattern):
        raise SchemaExecutionPolicyError(
            "unsafe_pattern",
            f"{owner} regular-expression keyword {keyword!r} uses unsupported "
            "backtracking constructs",
        )


def _schema_execution_walk(
    schema: Mapping[str, object] | bool,
    *,
    policy: SchemaExecutionPolicy,
    owner: str,
    stop_at_pattern: bool,
) -> tuple[int, str | None]:
    resource = DRAFT202012.create_resource(schema)
    resolver = Registry().resolver_with_root(resource)
    pending: list[object] = [schema]
    visited: set[int] = set()
    validation_steps = 0

    while pending:
        candidate = pending.pop()
        if isinstance(candidate, bool) or not isinstance(candidate, Mapping):
            continue
        identity = id(candidate)
        if identity in visited:
            continue
        visited.add(identity)
        validation_steps += 1
        if validation_steps > policy.max_validation_steps:
            raise SchemaExecutionPolicyError(
                "max_validation_steps",
                f"{owner} must not require more than "
                f"{policy.max_validation_steps} validation steps",
            )

        pattern = candidate.get("pattern")
        if isinstance(pattern, str):
            if stop_at_pattern:
                return validation_steps, "pattern"
            _check_pattern(
                pattern,
                keyword="pattern",
                policy=policy,
                owner=owner,
            )
        pattern_properties = candidate.get("patternProperties")
        if isinstance(pattern_properties, Mapping) and pattern_properties:
            if stop_at_pattern:
                return validation_steps, "patternProperties"
            for property_pattern in pattern_properties:
                if isinstance(property_pattern, str):
                    _check_pattern(
                        property_pattern,
                        keyword="patternProperties",
                        policy=policy,
                        owner=owner,
                    )

        try:
            subresources = tuple(DRAFT202012.subresources_of(candidate))
        except (AttributeError, TypeError):
            # Meta-schema validation reports malformed applicator values. The
            # resource preflight still owns shape and byte ceilings first.
            subresources = ()
        pending.extend(subresources)
        for reference_keyword in ("$ref", "$dynamicRef"):
            reference = candidate.get(reference_keyword)
            if not isinstance(reference, str):
                continue
            is_local = reference == "" or reference.startswith("#")
            if not is_local:
                if not policy.allow_remote_ref:
                    raise SchemaExecutionPolicyError(
                        "remote_ref",
                        f"{owner} contains non-local {reference_keyword}; "
                        f"{reference_keyword} references must be local fragments",
                    )
                continue
            try:
                pending.append(resolver.lookup(reference).contents)
            except Unresolvable:
                # The validator owns unresolved-local-reference diagnostics. No
                # remote load or regex execution occurs during this preflight.
                continue

    return validation_steps, None


def enforce_schema_execution_policy(
    schema: Mapping[str, object] | bool,
    *,
    policy: SchemaExecutionPolicy = DEFAULT_SCHEMA_EXECUTION_POLICY,
    owner: str = "JSON Schema",
) -> SchemaExecutionMetrics:
    """Fail closed before constructing or running a JSON Schema validator."""

    if isinstance(schema, bool):
        pass
    elif not isinstance(schema, Mapping):
        raise TypeError("schema must be a mapping or boolean")
    if not isinstance(policy, SchemaExecutionPolicy):
        raise TypeError("policy must be a SchemaExecutionPolicy")
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be a non-empty string")

    nodes, depth = _schema_shape_metrics(schema, policy=policy, owner=owner)
    schema_bytes = len(canonical_dumps(schema).encode("utf-8"))
    if schema_bytes > policy.max_schema_bytes:
        raise SchemaExecutionPolicyError(
            "max_schema_bytes",
            f"{owner} must not exceed {policy.max_schema_bytes} UTF-8 bytes",
        )
    validation_steps, _ = _schema_execution_walk(
        schema,
        policy=policy,
        owner=owner,
        stop_at_pattern=False,
    )
    return SchemaExecutionMetrics(
        schema_bytes=schema_bytes,
        nodes=nodes,
        depth=depth,
        validation_steps=validation_steps,
    )


def find_regular_expression_keyword(
    schema: Mapping[str, object],
) -> str | None:
    """Return a regex-bearing keyword reachable as a Draft 2020-12 schema."""

    _, keyword = _schema_execution_walk(
        schema,
        policy=DEFAULT_SCHEMA_EXECUTION_POLICY,
        owner="JSON Schema",
        stop_at_pattern=True,
    )
    return keyword


__all__ = [
    "DEFAULT_SCHEMA_EXECUTION_POLICY",
    "SchemaExecutionMetrics",
    "SchemaExecutionPolicy",
    "SchemaExecutionPolicyError",
    "UNTRUSTED_SCHEMA_EXECUTION_POLICY",
    "enforce_schema_execution_policy",
    "find_regular_expression_keyword",
]
