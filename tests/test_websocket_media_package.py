from __future__ import annotations

import importlib

import pytest

from graphblocks.packages import load_package_catalog, package_rows


def _import_websocket_media(monkeypatch):
    return importlib.import_module("graphblocks.integrations.websocket_media")


def test_websocket_media_endpoint_builds_voice_transport(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)
    endpoint = graphblocks_websocket_media.WebSocketMediaEndpoint(
        uri="wss://voice.example.com/media",
        protocol="graphblocks.voice.v1",
        codec="pcm16",
        sample_rate_hz=24_000,
    )

    transport = endpoint.to_voice_transport()

    assert transport.contract() == {
        "kind": "websocket",
        "uri": "wss://voice.example.com/media",
        "codec": "pcm16",
        "sampleRateHz": 24000,
        "channels": 1,
    }
    assert endpoint.handshake_contract() == {
        "uri": "wss://voice.example.com/media",
        "protocol": "graphblocks.voice.v1",
        "codec": "pcm16",
        "sampleRateHz": 24000,
        "channels": 1,
        "headers": {},
    }


def test_websocket_media_messages_are_sequence_ordered(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)
    stream = graphblocks_websocket_media.WebSocketMediaStream(
        stream_id="mic",
        messages=(
            graphblocks_websocket_media.WebSocketMediaMessage.audio_delta("mic", 2, audio_ref="blob:2", duration_ms=20),
            graphblocks_websocket_media.WebSocketMediaMessage.audio_delta("mic", 1, audio_ref="blob:1", duration_ms=20),
        ),
    )

    assert [message.sequence for message in stream.messages] == [1, 2]
    assert stream.stream_contract()["messages"][0]["audioRef"] == "blob:1"
    assert stream.content_digest().startswith("sha256:")


def test_websocket_media_validates_endpoint_and_messages(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)

    with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
        graphblocks_websocket_media.WebSocketMediaEndpoint("https://voice.example.com/media")
    with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
        graphblocks_websocket_media.WebSocketMediaMessage.audio_delta("mic", -1, audio_ref="blob:1", duration_ms=20)
    with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
        graphblocks_websocket_media.WebSocketMediaMessage.audio_delta("mic", 1, audio_ref="blob:1", duration_ms=0)


def test_websocket_media_package_is_cataloged_as_optional_voice_adapter(monkeypatch) -> None:
    _import_websocket_media(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-websocket-media"] == {
        "distribution": "graphblocks-websocket-media",
        "artifact": "graphblocks",
        "component": "graphblocks-websocket-media",
        "import": "graphblocks.integrations.websocket_media",
        "default": False,
        "layer": "voice_transport_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
