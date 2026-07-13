from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from graphblocks.canonical import canonical_hash


MODEL_CHOICES = ("gpt", "gemini", "claude")
LOGICAL_RESOURCE = "coding-model"


def load_model_profile(example_root: Path, choice: str) -> dict[str, str]:
    if choice not in MODEL_CHOICES:
        raise ValueError(f"unsupported model choice {choice!r}; choose one of {MODEL_CHOICES}")

    binding_path = example_root / "bindings" / f"{choice}.yaml"
    document = yaml.safe_load(binding_path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or document.get("kind") != "Binding":
        raise ValueError(f"{binding_path} must contain one Binding document")

    metadata = document.get("metadata")
    spec = document.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("name"), str):
        raise ValueError(f"{binding_path} must declare metadata.name")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("resources"), Mapping):
        raise ValueError(f"{binding_path} must declare spec.resources")

    resources = spec["resources"]
    if set(resources) != {LOGICAL_RESOURCE}:
        raise ValueError(
            f"{binding_path} must bind only the logical resource {LOGICAL_RESOURCE!r}"
        )
    resource = resources[LOGICAL_RESOURCE]
    if not isinstance(resource, Mapping) or resource.get("kind") != "ChatModel":
        raise ValueError(f"{binding_path} {LOGICAL_RESOURCE!r} must be a ChatModel")

    implementation = resource.get("implementation")
    config = resource.get("config")
    credentials = resource.get("credentials")
    if not isinstance(implementation, str):
        raise ValueError(f"{binding_path} must declare a model implementation")
    if not isinstance(config, Mapping):
        raise ValueError(f"{binding_path} must declare model config")
    if not isinstance(credentials, Mapping):
        raise ValueError(f"{binding_path} must declare model credentials")

    provider = config.get("provider")
    model = config.get("model")
    api = config.get("api")
    secret_ref = credentials.get("secretRef")
    if not all(isinstance(value, str) and value for value in (provider, model, api)):
        raise ValueError(f"{binding_path} provider, model, and api must be non-empty strings")
    if not isinstance(secret_ref, str) or not secret_ref.startswith("secret://"):
        raise ValueError(f"{binding_path} credentials must use a secret:// reference")

    return {
        "choice": choice,
        "binding": str(metadata["name"]),
        "resource": LOGICAL_RESOURCE,
        "provider": provider,
        "model": model,
        "api": api,
        "implementation": implementation,
        "secretRef": secret_ref,
        "digest": canonical_hash(document),
    }


def model_selection_evidence(example_root: Path, selected_choice: str) -> dict[str, object]:
    profiles = {choice: load_model_profile(example_root, choice) for choice in MODEL_CHOICES}
    for field in ("binding", "provider", "model", "implementation", "secretRef", "digest"):
        values = {profile[field] for profile in profiles.values()}
        if len(values) != len(MODEL_CHOICES):
            raise ValueError(f"model binding profiles must use distinct {field} values")

    catalog = {
        choice: {
            "provider": profile["provider"],
            "model": profile["model"],
            "api": profile["api"],
        }
        for choice, profile in profiles.items()
    }
    return {
        "modelSelection": {
            "selected": profiles[selected_choice],
            "available": catalog,
            "execution": {
                "mode": "offline-fixture",
                "provider": "scripted-llm",
                "externalRequestSent": False,
            },
        }
    }
