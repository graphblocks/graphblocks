from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks.packages import load_package_catalog, package_rows


ROOT = Path(__file__).parents[1]


def _import_webrtc(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-voice" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-webrtc" / "src"))
    return importlib.import_module("graphblocks_webrtc")


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


def test_webrtc_validates_descriptions_and_candidates(monkeypatch) -> None:
    graphblocks_webrtc = _import_webrtc(monkeypatch)

    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcSessionDescription("rollback", "v=0\r\n")
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcIceCandidate("candidate", sdp_mline_index=-1)
    with pytest.raises(graphblocks_webrtc.WebRtcAdapterError):
        graphblocks_webrtc.WebRtcSession(" ", "browser", graphblocks_webrtc.WebRtcSessionDescription("offer", "v=0\r\n"))


def test_webrtc_package_is_cataloged_as_optional_voice_adapter(monkeypatch) -> None:
    _import_webrtc(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-webrtc"] == {
        "distribution": "graphblocks-webrtc",
        "import": "graphblocks_webrtc",
        "default": False,
        "layer": "voice_transport_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
