from __future__ import annotations

from collections.abc import Mapping

from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012


def find_regular_expression_keyword(
    schema: Mapping[str, object],
) -> str | None:
    """Return a regex-bearing keyword reachable as a Draft 2020-12 schema."""

    resource = Resource.from_contents(
        schema,
        default_specification=DRAFT202012,
    )
    resolver = Registry().resolver_with_root(resource)
    pending: list[object] = [schema]
    visited: set[int] = set()

    while pending:
        candidate = pending.pop()
        if isinstance(candidate, bool) or not isinstance(candidate, Mapping):
            continue
        identity = id(candidate)
        if identity in visited:
            continue
        visited.add(identity)

        if "pattern" in candidate:
            return "pattern"
        pattern_properties = candidate.get("patternProperties")
        if isinstance(pattern_properties, Mapping) and pattern_properties:
            return "patternProperties"

        pending.extend(DRAFT202012.subresources_of(candidate))
        for reference_keyword in ("$ref", "$dynamicRef"):
            reference = candidate.get(reference_keyword)
            if not isinstance(reference, str) or (
                reference != "" and not reference.startswith("#")
            ):
                continue
            try:
                pending.append(resolver.lookup(reference).contents)
            except Unresolvable:
                # The validator reports an invalid local reference if execution
                # reaches it. No regex is executed during this safety scan.
                continue

    return None


__all__ = ["find_regular_expression_keyword"]
