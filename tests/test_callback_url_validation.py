from __future__ import annotations

import sys
from pathlib import Path

import pytest


CALLBACKS_SRC = Path(__file__).parents[1] / "packages" / "graphblocks-callbacks" / "src"
if str(CALLBACKS_SRC) not in sys.path:
    sys.path.insert(0, str(CALLBACKS_SRC))


from graphblocks.url_validation import validate_webhook_url  # noqa: E402
from graphblocks_callbacks import CallbackEndpointAuth, CallbackEndpointRef  # noqa: E402


def test_webhook_url_validation_rejects_malformed_authorities() -> None:
    for url in (
        "https://hooks example.com/events",
        "https://hooks.example.com\t/events",
        "https://hooks.example.com%2fevil.test/events",
        "https://[not-ipv6]/events",
        "https://[fe80::1%25eth0]/events",
        "https://hooks.example.com:/events",
        "https://hooks.example.com:abc/events",
        "https://hooks.example.com:65536/events",
        "https://hooks_example.com/events",
    ):
        result = validate_webhook_url(url)

        assert result.allowed is False
        assert result.reason == "invalid_host"


def test_webhook_url_validation_separates_syntax_from_egress_policy() -> None:
    private = validate_webhook_url("https://10.0.0.7/callback")
    explicitly_allowed = validate_webhook_url(
        "https://10.0.0.7/callback",
        allow_private=True,
    )

    assert private.allowed is False
    assert private.reason == "forbidden_ip"
    assert explicitly_allowed.allowed is True
    assert explicitly_allowed.reason == "allowed"


def test_webhook_url_validation_rejects_legacy_loopback_ipv4_forms() -> None:
    for url in (
        "https://127.1/callback",
        "https://127.0.1/callback",
        "https://0177.0.0.1/callback",
        "https://0x7f.0.0.1/callback",
    ):
        result = validate_webhook_url(url)

        assert result.allowed is False
        assert result.reason == "forbidden_ip"


def test_webhook_url_validation_rejects_raw_request_target_whitespace_and_controls() -> None:
    for url in (
        "https://callbacks.example.com/bad path",
        "https://callbacks.example.com/events?name=bad\tvalue",
        "https://callbacks.example.com/events?name=bad\r\nInjected: value",
    ):
        result = validate_webhook_url(url)

        assert result.allowed is False
        assert result.reason == "invalid_request_target"


def test_webhook_url_validation_can_require_https() -> None:
    result = validate_webhook_url(
        "http://callbacks.example.com/events",
        allowed_schemes=frozenset({"https"}),
    )

    assert result.allowed is False
    assert result.reason == "unsupported_scheme"

    uppercase = validate_webhook_url("HTTPS://callbacks.example.com/events")

    assert uppercase.allowed is False
    assert uppercase.reason == "unsupported_scheme"


def test_webhook_url_validation_rejects_private_resolved_hostname_addresses() -> None:
    result = validate_webhook_url(
        "https://callbacks.example.com/events",
        resolved_addresses=("93.184.216.34", "127.0.0.1"),
    )

    assert result.allowed is False
    assert result.reason == "forbidden_resolved_ip"


@pytest.mark.parametrize(
    "address",
    (
        "100.64.0.1",  # shared address space (CGNAT)
        "198.18.0.1",  # benchmark network
        "192.0.2.1",  # documentation network
        "2001:db8::1",  # IPv6 documentation network
        "64:ff9b:1::1",  # local-use IPv4/IPv6 translation prefix
    ),
)
def test_webhook_url_validation_rejects_non_global_literal_addresses(address: str) -> None:
    authority = f"[{address}]" if ":" in address else address

    result = validate_webhook_url(f"https://{authority}/callback")

    assert result.allowed is False
    assert result.reason == "forbidden_ip"


@pytest.mark.parametrize(
    "address",
    (
        "100.64.0.1",
        "198.18.0.1",
        "192.0.2.1",
        "2001:db8::1",
        "64:ff9b:1::1",
    ),
)
def test_webhook_url_validation_rejects_non_global_resolved_addresses(address: str) -> None:
    result = validate_webhook_url(
        "https://callbacks.example.com/events",
        resolved_addresses=(address,),
    )

    assert result.allowed is False
    assert result.reason == "forbidden_resolved_ip"


def test_webhook_url_validation_explicit_override_allows_non_global_addresses() -> None:
    literal = validate_webhook_url(
        "https://100.64.0.1/callback",
        allow_private=True,
    )
    resolved = validate_webhook_url(
        "https://callbacks.example.com/events",
        allow_private=True,
        resolved_addresses=("100.64.0.1",),
    )

    assert literal.allowed is True
    assert resolved.allowed is True
    assert resolved.resolved_addresses == ("100.64.0.1",)


def test_callback_endpoint_ref_uses_shared_authority_validation() -> None:
    for url in (
        "https://hooks example.com/v1/callbacks/op_ci_001",
        "https://graphblocks.example.com%2fevil/v1/callbacks/op_ci_001",
        "https://[not-ipv6]/v1/callbacks/op_ci_001",
        "https://[fe80::1%25eth0]/v1/callbacks/op_ci_001",
        "https://graphblocks.example.com:/v1/callbacks/op_ci_001",
    ):
        with pytest.raises(ValueError, match="url host is malformed"):
            CallbackEndpointRef(
                endpoint_id="cbep_ci_001",
                url=url,
                accepted_schema="schemas/CICallback@1",
                auth=CallbackEndpointAuth(kind="hmac", secret_ref="secret://callbacks/ci"),
                operation_id="op_ci_001",
                run_id="run_coding_001",
                node_id="waitCI",
                attempt_id="attempt_001",
                release_id="rel_001",
                tenant_id="tenant_001",
            )
