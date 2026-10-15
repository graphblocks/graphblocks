"""Private, bounded text projection for errors recorded as strict JSON."""

from __future__ import annotations


def _bounded_error_message(error: BaseException) -> str:
    try:
        message = str.__str__(str(error))
    except Exception:
        message = "<unprintable exception>"
    message = message[:1_025].encode("utf-8", errors="backslashreplace").decode("utf-8")
    # At most 1,024 Unicode scalars also bounds the UTF-8 projection to 4 KiB.
    if len(message) > 1_024:
        message = message[:1_021] + "..."
    return message
