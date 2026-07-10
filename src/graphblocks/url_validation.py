from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from urllib.parse import urlparse


FORBIDDEN_WEBHOOK_HOSTS = frozenset({"localhost", "metadata.google.internal"})


@dataclass(frozen=True, slots=True)
class WebhookUrlValidation:
    allowed: bool
    reason: str
    host: str | None = None
    resolved_addresses: tuple[str, ...] = ()


def validate_webhook_url(
    url: object,
    *,
    allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
    allow_private: bool = False,
    resolved_addresses: tuple[str, ...] | None = None,
) -> WebhookUrlValidation:
    if not isinstance(allow_private, bool):
        raise ValueError("allow_private must be a boolean")
    if resolved_addresses is not None and (
        not isinstance(resolved_addresses, tuple)
        or not resolved_addresses
        or any(not isinstance(item, str) or not item for item in resolved_addresses)
    ):
        raise ValueError("resolved_addresses must be a non-empty tuple of IP address strings")
    if not allowed_schemes or any(
        not isinstance(scheme, str) or not scheme or scheme != scheme.lower()
        for scheme in allowed_schemes
    ):
        raise ValueError("allowed_schemes must contain lowercase scheme names")
    if not isinstance(url, str) or not url:
        return WebhookUrlValidation(False, "missing_url")
    if url != url.strip():
        return WebhookUrlValidation(False, "surrounding_whitespace")
    raw_scheme = url.split("://", 1)[0] if "://" in url else ""
    if raw_scheme not in allowed_schemes:
        return WebhookUrlValidation(False, "unsupported_scheme")

    try:
        parsed = urlparse(url)
    except ValueError:
        return WebhookUrlValidation(False, "invalid_host")
    if parsed.scheme not in allowed_schemes:
        return WebhookUrlValidation(False, "unsupported_scheme", parsed.hostname)

    raw_rest = url.split("://", 1)[1] if "://" in url else ""
    raw_authority = raw_rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "%" in raw_authority or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in raw_authority
    ):
        return WebhookUrlValidation(False, "invalid_host")
    raw_request_target = raw_rest[len(raw_authority) :]
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in raw_request_target
    ):
        return WebhookUrlValidation(False, "invalid_request_target")

    try:
        username = parsed.username
        password = parsed.password
        _ = parsed.port
        parsed_hostname = parsed.hostname
    except ValueError:
        return WebhookUrlValidation(False, "invalid_host")
    if username is not None or password is not None:
        return WebhookUrlValidation(False, "userinfo_not_allowed", parsed_hostname)
    if parsed_hostname is None or not parsed_hostname.strip():
        return WebhookUrlValidation(False, "missing_host")

    if raw_authority.startswith("["):
        closing_bracket = raw_authority.find("]")
        if closing_bracket < 0:
            return WebhookUrlValidation(False, "invalid_host")
        port_suffix = raw_authority[closing_bracket + 1 :]
        if port_suffix:
            if not port_suffix.startswith(":"):
                return WebhookUrlValidation(False, "invalid_host", parsed_hostname)
            raw_port = port_suffix[1:]
            if not raw_port.isascii() or not raw_port.isdecimal() or int(raw_port) > 65535:
                return WebhookUrlValidation(False, "invalid_host", parsed_hostname)
    elif ":" in raw_authority:
        if raw_authority.count(":") != 1:
            return WebhookUrlValidation(False, "invalid_host", parsed_hostname)
        _, raw_port = raw_authority.rsplit(":", 1)
        if not raw_port.isascii() or not raw_port.isdecimal() or int(raw_port) > 65535:
            return WebhookUrlValidation(False, "invalid_host", parsed_hostname)

    raw_host = parsed_hostname
    host = raw_host.strip().rstrip(".").lower()
    if host != raw_host.rstrip(".").lower() or "%" in host:
        return WebhookUrlValidation(False, "invalid_host", host or None)

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if parsed.netloc.startswith("[") or ":" in host:
            return WebhookUrlValidation(False, "invalid_host", host)
        if (
            any(
                not (character.isascii() and (character.isalnum() or character in "-."))
                for character in host
            )
            or any(
                not label or label.startswith("-") or label.endswith("-")
                for label in host.split(".")
            )
        ):
            return WebhookUrlValidation(False, "invalid_host", host)
        parsed_numeric_parts: list[int] = []
        for component in host.split("."):
            if component.lower().startswith("0x") and len(component) > 2:
                base = 16
                digits = component[2:]
            elif len(component) > 1 and component.startswith("0"):
                base = 8
                digits = component[1:]
            elif component.isascii() and component.isdecimal():
                base = 10
                digits = component
            else:
                parsed_numeric_parts = []
                break
            try:
                parsed_numeric_parts.append(int(digits, base))
            except ValueError:
                parsed_numeric_parts = []
                break

        numeric_ipv4 = None
        if len(parsed_numeric_parts) == 1 and parsed_numeric_parts[0] <= 0xFFFFFFFF:
            numeric_ipv4 = parsed_numeric_parts[0]
        elif (
            len(parsed_numeric_parts) == 2
            and parsed_numeric_parts[0] <= 0xFF
            and parsed_numeric_parts[1] <= 0xFFFFFF
        ):
            numeric_ipv4 = (parsed_numeric_parts[0] << 24) | parsed_numeric_parts[1]
        elif (
            len(parsed_numeric_parts) == 3
            and parsed_numeric_parts[0] <= 0xFF
            and parsed_numeric_parts[1] <= 0xFF
            and parsed_numeric_parts[2] <= 0xFFFF
        ):
            numeric_ipv4 = (
                (parsed_numeric_parts[0] << 24)
                | (parsed_numeric_parts[1] << 16)
                | parsed_numeric_parts[2]
            )
        elif len(parsed_numeric_parts) == 4 and all(
            component <= 0xFF for component in parsed_numeric_parts
        ):
            numeric_ipv4 = (
                (parsed_numeric_parts[0] << 24)
                | (parsed_numeric_parts[1] << 16)
                | (parsed_numeric_parts[2] << 8)
                | parsed_numeric_parts[3]
            )
        address = ipaddress.ip_address(numeric_ipv4) if numeric_ipv4 is not None else None
    else:
        if parsed.netloc.startswith("[") and address.version != 6:
            return WebhookUrlValidation(False, "invalid_host", host)

    if not allow_private:
        if host in FORBIDDEN_WEBHOOK_HOSTS or host.endswith(".localhost"):
            return WebhookUrlValidation(False, "forbidden_host", host)
        if address is not None and (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return WebhookUrlValidation(False, "forbidden_ip", host)

    normalized_resolved_addresses: list[str] = []
    for resolved_address in resolved_addresses or ():
        try:
            parsed_resolved_address = ipaddress.ip_address(resolved_address)
        except ValueError:
            return WebhookUrlValidation(False, "invalid_resolved_ip", host)
        normalized_address = str(parsed_resolved_address)
        if normalized_address not in normalized_resolved_addresses:
            normalized_resolved_addresses.append(normalized_address)
        if not allow_private and (
            not parsed_resolved_address.is_global
            or parsed_resolved_address.is_loopback
            or parsed_resolved_address.is_private
            or parsed_resolved_address.is_link_local
            or parsed_resolved_address.is_reserved
            or parsed_resolved_address.is_multicast
            or parsed_resolved_address.is_unspecified
        ):
            return WebhookUrlValidation(
                False,
                "forbidden_resolved_ip",
                host,
                tuple(normalized_resolved_addresses),
            )

    return WebhookUrlValidation(True, "allowed", host, tuple(normalized_resolved_addresses))
