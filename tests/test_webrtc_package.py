from __future__ import annotations

import importlib

import pytest

from graphblocks.packages import load_package_catalog, package_rows


def _import_webrtc(monkeypatch):
    return importlib.import_module("graphblocks.integrations.webrtc")


def test_webrtc_session_builds_voice_transport_contract(monkeypatch) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)
    offer = graphblocks_webrtc.WebRtcSessionDescription(
        "offer",
        "v=0\r\no=- 4611733057217495165 2 IN IP4 127.0.0.1\r\n",
    )
    session = graphblocks_webrtc.WebRtcSession(
        session_id="session-1",
        peer_id="browser-1",
        offer=offer,
        codec="opus",
        sample_rate_hz=48_000,
    )

    transport = session.to_voice_transport()

    assert transport.contract() == {
        "kind": "webrtc",
        "uri": "webrtc:session-1",
        "codec": "opus",
        "sampleRateHz": 48000,
        "channels": 1,
    }
    assert session.signaling_contract()["offer"]["type"] == "offer"


def test_webrtc_ice_candidates_are_sorted_and_contract_is_stable(monkeypatch) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)
    session = graphblocks_webrtc.WebRtcSession(
        session_id="session-1",
        peer_id="browser-1",
        offer=graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n"),
        ice_candidates=(
            graphblocks_webrtc.WebRtcIceCandidate("candidate:2", sdp_mid="audio", sdp_mline_index=0, sequence=2),
            graphblocks_webrtc.WebRtcIceCandidate("candidate:1", sdp_mid="audio", sdp_mline_index=0, sequence=1),
        ),
    )

    contract = session.signaling_contract()

    assert [candidate["candidate"] for candidate in contract["iceCandidates"]] == ["candidate:1", "candidate:2"]
    assert session.content_digest().startswith("sha256:")


def test_webrtc_digest_is_stable_for_duplicate_candidate_sequences(monkeypatch) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)
    offer = graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n")
    candidates = (
        graphblocks_webrtc.WebRtcIceCandidate("candidate:2", sdp_mid="audio"),
        graphblocks_webrtc.WebRtcIceCandidate("candidate:1", sdp_mid="audio"),
    )

    first = graphblocks_webrtc.WebRtcSession(
        session_id="session-1",
        peer_id="browser-1",
        offer=offer,
        ice_candidates=candidates,
    )
    second = graphblocks_webrtc.WebRtcSession(
        session_id="session-1",
        peer_id="browser-1",
        offer=offer,
        ice_candidates=tuple(reversed(candidates)),
    )

    assert first.signaling_contract() == second.signaling_contract()
    assert first.content_digest() == second.content_digest()


def test_webrtc_deduplicates_exact_candidate_replays_and_rejects_unstable_ids(
    monkeypatch,
) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)
    offer = graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n")
    candidate = graphblocks_webrtc.WebRtcIceCandidate(
        "candidate:1 1 UDP 2122260223 192.0.2.1 54400 typ host",
        sdp_mid="audio",
        sequence=1,
    )

    session = graphblocks_webrtc.WebRtcSession(
        "session-1",
        "browser-1",
        offer,
        ice_candidates=(candidate, candidate),
    )
    assert session.ice_candidates == (candidate,)

    for field, value in (
        ("session_id", "session 1"),
        ("peer_id", "browser\n1"),
        ("codec", "op us"),
    ):
        values = {
            "session_id": "session-1",
            "peer_id": "browser-1",
            "offer": offer,
            "codec": "opus",
        }
        values[field] = value
        with pytest.raises(
            graphblocks_webrtc.WebRtcAdapterError,
            match="exact non-empty",
        ):
            graphblocks_webrtc.WebRtcSession(**values)


def test_webrtc_validates_descriptions_and_candidates(monkeypatch) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)

    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcSessionDescription("rollback", "v=0\r\n")
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcIceCandidate("candidate", sdp_mline_index=-1)
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcIceCandidate("candidate", sdp_mline_index=True)  # type: ignore[arg-type]
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcIceCandidate("candidate", sequence=True)  # type: ignore[arg-type]
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcSession(" ", "browser", graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n"))


def test_webrtc_rejects_non_integer_transport_numbers_and_invalid_components(
    monkeypatch,
) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)
    offer = graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n")

    for field, value in (
        ("sample_rate_hz", True),
        ("sample_rate_hz", 48_000.5),
        ("channels", True),
        ("channels", 1.5),
    ):
        with pytest.raises(graphblocks_webrtc.WebRtcAdapterError, match=field):
            graphblocks_webrtc.WebRtcSession(
                "session-1",
                "browser-1",
                offer,
                **{field: value},
            )

    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError, match="offer"):
        graphblocks_webrtc.WebRtcSession("session-1", "browser-1", object())
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError, match="answer"):
        graphblocks_webrtc.WebRtcSession("session-1", "browser-1", offer, answer=object())
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError, match="ICE candidate"):
        graphblocks_webrtc.WebRtcSession(
            "session-1",
            "browser-1",
            offer,
            ice_candidates=(object(),),
        )


def test_webrtc_rejects_sdp_controls_and_wire_integer_overflow(monkeypatch) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)

    for sdp in ("v=0\r\n\x00", "v=0\r\n\x7f"):
        with pytest.raises(
            graphblocks_webrtc.WebRtcAdapterError,
            match="control",
        ):
            graphblocks_webrtc.WebRtcSessionDescription("offer", sdp)

    offer = graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n")
    invalid_values = (
        lambda: graphblocks_webrtc.WebRtcIceCandidate(
            "candidate:1",
            sdp_mline_index=1 << 16,
        ),
        lambda: graphblocks_webrtc.WebRtcIceCandidate(
            "candidate:1",
            sequence=1 << 64,
        ),
        lambda: graphblocks_webrtc.WebRtcSession(
            "session-1",
            "browser-1",
            offer,
            sample_rate_hz=1 << 32,
        ),
        lambda: graphblocks_webrtc.WebRtcSession(
            "session-1",
            "browser-1",
            offer,
            channels=1 << 16,
        ),
    )
    for factory in invalid_values:
        with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
            factory()


def test_webrtc_package_is_cataloged_as_optional_voice_adapter(monkeypatch) -> None:
    _import_webrtc(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-webrtc"] == {
        "distribution": "graphblocks-webrtc",
        "artifact": "graphblocks",
        "component": "graphblocks-webrtc",
        "import": "graphblocks.integrations.webrtc",
        "default": False,
        "layer": "voice_transport_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
