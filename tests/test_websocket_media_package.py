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


def test_websocket_media_stream_preserves_one_shot_message_iterables(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)
    message = graphblocks_websocket_media.WebSocketMediaMessage.audio_delta(
        "mic",
        1,
        audio_ref="blob:1",
        duration_ms=20,
    )

    stream = graphblocks_websocket_media.WebSocketMediaStream(
        "mic",
        (item for item in (message,)),
    )

    assert stream.messages == (message,)


def test_websocket_media_validates_endpoint_and_messages(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)

    with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
        graphblocks_websocket_media.WebSocketMediaEndpoint("https://voice.example.com/media")
    with pytest.raises(
        graphblocks_websocket_media.WebSocketMediaAdapterError,
        match="absolute",
    ):
        graphblocks_websocket_media.WebSocketMediaEndpoint("wss:///media")
    for uri in (
        "wss://user:secret@voice.example.com/media",
        "wss://voice.example.com:/media",
    ):
        with pytest.raises(
            graphblocks_websocket_media.WebSocketMediaAdapterError,
            match="absolute",
        ):
            graphblocks_websocket_media.WebSocketMediaEndpoint(uri)
    with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
        graphblocks_websocket_media.WebSocketMediaMessage.audio_delta("mic", -1, audio_ref="blob:1", duration_ms=20)
    with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
        graphblocks_websocket_media.WebSocketMediaMessage.audio_delta("mic", 1, audio_ref="blob:1", duration_ms=0)
    with pytest.raises(
        graphblocks_websocket_media.WebSocketMediaAdapterError,
        match="sequence 1 must be unique",
    ):
        graphblocks_websocket_media.WebSocketMediaStream(
            "mic",
            (
                graphblocks_websocket_media.WebSocketMediaMessage.audio_delta(
                    "mic", 1, audio_ref="blob:1", duration_ms=20
                ),
                graphblocks_websocket_media.WebSocketMediaMessage.audio_delta(
                    "mic", 1, audio_ref="blob:2", duration_ms=20
                ),
            ),
        )


def test_websocket_media_rejects_non_integer_transport_numbers(
    monkeypatch,
) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)

    for field, value in (
        ("sample_rate_hz", True),
        ("sample_rate_hz", 24_000.5),
        ("channels", True),
        ("channels", 1.5),
    ):
        with pytest.raises(
            graphblocks_websocket_media.WebSocketMediaAdapterError,
            match=field,
        ):
            graphblocks_websocket_media.WebSocketMediaEndpoint(
                "wss://voice.example.com/media",
                **{field: value},
            )

    for field, value in (
        ("sequence", True),
        ("sequence", 1.5),
        ("duration_ms", True),
        ("duration_ms", 20.5),
    ):
        values = {
            "stream_id": "mic",
            "sequence": 1,
            "kind": "audio_delta",
            "audio_ref": "blob:1",
            "duration_ms": 20,
        }
        values[field] = value
        with pytest.raises(
            graphblocks_websocket_media.WebSocketMediaAdapterError,
            match=field,
        ):
            graphblocks_websocket_media.WebSocketMediaMessage(**values)


def test_websocket_media_enforces_kind_specific_payloads(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)

    invalid_messages = (
        {"stream_id": "mic", "sequence": 1, "kind": "audio_delta", "audio_ref": "blob:1"},
        {"stream_id": "mic", "sequence": 1, "kind": "control"},
        {"stream_id": "mic", "sequence": 1, "kind": "transcript_delta"},
        {
            "stream_id": "mic",
            "sequence": 1,
            "kind": "control",
            "control": "stop",
            "text": "unexpected",
        },
    )

    for message in invalid_messages:
        with pytest.raises(graphblocks_websocket_media.WebSocketMediaAdapterError):
            graphblocks_websocket_media.WebSocketMediaMessage(**message)

    with pytest.raises(
        graphblocks_websocket_media.WebSocketMediaAdapterError,
        match="message entries",
    ):
        graphblocks_websocket_media.WebSocketMediaStream("mic", (object(),))


def test_websocket_media_validates_and_freezes_string_maps(monkeypatch) -> None:
    graphblocks_websocket_media = _import_websocket_media(monkeypatch)

    with pytest.raises(
        graphblocks_websocket_media.WebSocketMediaAdapterError,
        match="headers must be a mapping",
    ):
        graphblocks_websocket_media.WebSocketMediaEndpoint(
            "wss://voice.example.com/media",
            headers=None,
        )
    with pytest.raises(
        graphblocks_websocket_media.WebSocketMediaAdapterError,
        match="headers values must be strings",
    ):
        graphblocks_websocket_media.WebSocketMediaEndpoint(
            "wss://voice.example.com/media",
            headers={"X-Test": 7},
        )
    with pytest.raises(
        graphblocks_websocket_media.WebSocketMediaAdapterError,
        match="metadata values must be strings",
    ):
        graphblocks_websocket_media.WebSocketMediaMessage(
            "mic",
            1,
            "control",
            control="stop",
            metadata={"attempt": 1},
        )

    headers = {"X-Test": "trusted"}
    endpoint = graphblocks_websocket_media.WebSocketMediaEndpoint(
        "wss://voice.example.com/media",
        headers=headers,
    )
    headers["X-Test"] = "caller-mutated"
    with pytest.raises(TypeError):
        endpoint.headers["X-Test"] = "consumer-mutated"
    assert endpoint.handshake_contract()["headers"] == {"X-Test": "trusted"}


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
