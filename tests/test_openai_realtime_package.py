from __future__ import annotations

import importlib

import pytest

from graphblocks.packages import load_package_catalog, package_rows


def _import_openai_realtime(monkeypatch):
    return importlib.import_module("graphblocks.integrations.openai_realtime")


def test_openai_realtime_session_config_projects_provider_payload(monkeypatch) -> None:
    graphblocks_openai_realtime = _import_openai_realtime(monkeypatch)
    config = graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(
        model="gpt-realtime-2",
        instructions="Answer with concise support guidance.",
        voice="marin",
        modalities=("text", "audio"),
        metadata={"tenant": "acme"},
    )

    transport = config.to_voice_transport()

    assert config.session_payload() == {
        "type": "realtime",
        "model": "gpt-realtime-2",
        "instructions": "Answer with concise support guidance.",
        "modalities": ["audio", "text"],
        "audio": {"output": {"voice": "marin"}},
        "metadata": {"tenant": "acme"},
    }
    assert transport.contract() == {
        "kind": "provider_realtime",
        "uri": "openai-realtime:gpt-realtime-2",
        "codec": "pcm16",
        "sampleRateHz": 24000,
        "channels": 1,
    }


def test_openai_realtime_webrtc_call_contract_uses_unified_calls_endpoint(monkeypatch) -> None:
    graphblocks_openai_realtime = _import_openai_realtime(monkeypatch)
    config = graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(
        model="gpt-realtime-2",
        instructions="Answer using audio.",
    )
    call = graphblocks_openai_realtime.OpenAIRealtimeWebRtcCall(
        session_config=config,
        offer_sdp="v=0\r\n",
        safety_identifier="hashed-user-id",
    )

    assert call.request_contract() == {
        "method": "POST",
        "url": "https://api.openai.com/v1/realtime/calls",
        "contentType": "multipart/form-data",
        "fields": {
            "sdp": "v=0\r\n",
            "session": {
                "type": "realtime",
                "model": "gpt-realtime-2",
                "instructions": "Answer using audio.",
                "modalities": ["audio"],
                "audio": {"output": {"voice": "marin"}},
            },
        },
        "headers": {"OpenAI-Safety-Identifier": "hashed-user-id"},
        "requiresBearerToken": True,
    }
    assert call.answer_description("v=0\r\n") == {"type": "answer", "sdp": "v=0\r\n"}


def test_openai_realtime_client_secret_contract_uses_session_wrapper(monkeypatch) -> None:
    graphblocks_openai_realtime = _import_openai_realtime(monkeypatch)
    config = graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(
        model="gpt-realtime-2",
        instructions="Answer using audio.",
    )
    request = graphblocks_openai_realtime.OpenAIRealtimeClientSecretRequest(
        session_config=config,
        safety_identifier="hashed-user-id",
    )

    assert request.request_contract() == {
        "method": "POST",
        "url": "https://api.openai.com/v1/realtime/client_secrets",
        "contentType": "application/json",
        "body": {"session": config.session_payload()},
        "headers": {"OpenAI-Safety-Identifier": "hashed-user-id"},
        "requiresBearerToken": True,
    }


def test_openai_realtime_websocket_and_events_are_sdk_free_contracts(monkeypatch) -> None:
    graphblocks_openai_realtime = _import_openai_realtime(monkeypatch)
    config = graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(
        model="gpt-realtime-2",
        instructions="Be extra concise.",
    )
    session = graphblocks_openai_realtime.OpenAIRealtimeWebSocketSession(
        session_config=config,
        safety_identifier="hashed-user-id",
    )

    assert session.connection_contract() == {
        "url": "wss://api.openai.com/v1/realtime?model=gpt-realtime-2",
        "protocol": "realtime",
        "headers": {"OpenAI-Safety-Identifier": "hashed-user-id"},
        "requiresBearerToken": True,
    }
    assert graphblocks_openai_realtime.OpenAIRealtimeEvent.session_update(config).event_contract() == {
        "type": "session.update",
        "session": config.session_payload(),
    }
    assert graphblocks_openai_realtime.OpenAIRealtimeEvent.input_audio_append(
        "base64-audio",
        event_id="evt-1",
    ).event_contract() == {
        "event_id": "evt-1",
        "type": "input_audio_buffer.append",
        "audio": "base64-audio",
    }
    assert graphblocks_openai_realtime.OpenAIRealtimeEvent.user_text_message(
        "hello there!",
        event_id="evt-2",
    ).event_contract() == {
        "event_id": "evt-2",
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello there!"}],
        },
    }


def test_openai_realtime_validates_contracts(monkeypatch) -> None:
    graphblocks_openai_realtime = _import_openai_realtime(monkeypatch)
    config = graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(
        model="gpt-realtime-2",
        instructions="Answer using audio.",
    )

    with pytest.raises(
        graphblocks_openai_realtime.OpenAIRealtimeAdapterError,
        match="reserved envelope fields",
    ):
        graphblocks_openai_realtime.OpenAIRealtimeEvent(
            "session.update",
            {"type": "response.cancel", "event_id": "spoofed"},
            event_id="evt-authoritative",
        )

    with pytest.raises(graphblocks_openai_realtime.OpenAIRealtimeAdapterError):
        graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(model=" ", instructions="ok")
    with pytest.raises(graphblocks_openai_realtime.OpenAIRealtimeAdapterError):
        graphblocks_openai_realtime.OpenAIRealtimeSessionConfig(
            model="gpt-realtime-2",
            instructions="ok",
            modalities=(),
        )
    with pytest.raises(graphblocks_openai_realtime.OpenAIRealtimeAdapterError):
        graphblocks_openai_realtime.OpenAIRealtimeWebRtcCall(config, offer_sdp=" ")
    with pytest.raises(graphblocks_openai_realtime.OpenAIRealtimeAdapterError):
        graphblocks_openai_realtime.OpenAIRealtimeWebSocketSession(config, base_url="https://api.openai.com")


def test_openai_realtime_package_is_cataloged_as_optional_voice_provider_adapter(monkeypatch) -> None:
    _import_openai_realtime(monkeypatch)
    rows = {row["distribution"]: row for row in package_rows(load_package_catalog())}

    assert rows["graphblocks-openai-realtime"] == {
        "distribution": "graphblocks-openai-realtime",
        "artifact": "graphblocks",
        "component": "graphblocks-openai-realtime",
        "import": "graphblocks.integrations.openai_realtime",
        "default": False,
        "layer": "voice_provider_adapter",
        "kind": "pure_python",
        "implementationPhase": "integration-defined",
        "stability": "integration",
    }
