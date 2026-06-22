# Extension A. Realtime Voice와 Duplex Session

## A.1 위치

Voice는 `graphblocks-voice` 선택 extension이다. Core의 `Conversation`, `Message`, `ToolCall`, `ModelResponse`, `Answer`를 재사용하고 다음만 추가한다.

```text
audio track
transport
VAD/turn detection
playout
interruption
duplex provider session
```

## A.2 패키지

```text
graphblocks-voice             # canonical media/session contract
graphblocks-webrtc            # transport
graphblocks-websocket-media   # transport
graphblocks-silero-vad        # local acoustic VAD
graphblocks-openai-realtime   # provider adapter
```

기본 `graphblocks` install에 포함되지 않는다.

## A.3 Pipeline profile

```text
cascade
- audio → VAD → STT → text agent → TTS → audio

realtime
- audio ⇄ native realtime provider ⇄ audio
          ⇅ tools/control

hybrid
- 일부 modality/provider만 realtime
```

## A.4 Duplex session contract

```rust
#[async_trait]
pub trait RealtimeSession: Send {
    async fn send(&self, command: RealtimeCommand) -> Result<()>;
    fn events(&mut self) -> Pin<Box<dyn Stream<Item = Result<RealtimeEvent>> + Send + '_>>;
    async fn close(&self, reason: CloseReason) -> Result<()>;
}
```

Control lane은 audio data lane보다 우선순위가 높아야 한다.

```text
CancelResponse
ClearOutput
CommitInput
CreateResponse
ToolResult
TruncateConversation
CloseSession
```

## A.5 AudioFrame

```python
class AudioFrame(BaseModel):
    track_id: str
    data: bytes
    codec: Literal["pcm16", "opus", "mulaw", "alaw"]
    sample_rate: int
    channels: int
    timestamp_ms: int
    sequence: int
    duration_ms: int | None = None
```

AEC, noise suppression, resampling, jitter buffering은 VAD와 분리한다.

## A.6 VoiceSession

```python
class VoiceSession(BaseModel):
    voice_session_id: str
    conversation_id: str
    transport: str
    pipeline_kind: Literal["cascade", "realtime", "hybrid"]
    provider_session_id: str | None = None
    status: Literal["connecting", "active", "closing", "closed", "failed"]
```

User turn과 assistant response를 분리한다.

## A.7 VAD 계층

```text
Acoustic VAD
- 음성 존재 확률과 speech start/stop

Endpoint detector
- 물리적 silence와 max utterance

Semantic turn detector
- 의미상 발화 완료

Interruption classifier
- true interruption/backchannel/echo/noise/background speaker
```

## A.8 Authority

```yaml
turnDetection:
  authority: provider       # provider | graphblocks | client
  mode: semantic

localVad:
  enabled: true
  role: metrics_and_early_duck
```

하나의 turn authority만 응답 생성/commit 권한을 가져야 한다.

## A.9 Interruption

```yaml
interruption:
  policy: adaptive
  minSpeechMs: 180
  ignoreBackchannels: true
  onPossible: duck
  onConfirmed:
    - clear_playout
    - cancel_response
    - truncate_conversation
  onFalse:
    - resume_playout
```

## A.10 PlaybackLedger

사용자가 실제로 들은 위치를 추적한다.

```python
class PlaybackCursor(BaseModel):
    response_id: str
    item_id: str
    content_index: int
    generated_ms: int
    enqueued_ms: int
    played_ms: int
    acknowledged_ms: int
```

WebSocket transport에서는 client playout acknowledgement를 받아 conversation truncation을 계산해야 한다.

## A.11 RealtimeEvent

```text
SessionCreated
InputSpeechStarted
InputSpeechStopped
InputTranscriptDelta
InputTranscriptFinal
ResponseCreated
OutputTextDelta
OutputAudioDelta
OutputTranscriptDelta
ToolCallStarted
ToolCallArgumentsDelta
ToolCallCompleted
ResponseCompleted
ResponseCancelled
UsageUpdated
Error
```

Provider event를 그대로 core schema로 노출하지 않고 adapter가 canonical event로 변환한다.

## A.12 Voice storage default

```text
raw input audio: false
raw output audio: false
partial transcript: false
final transcript: redacted/configurable
final assistant message: configurable
playback metrics: true
```

Recording은 consent, encryption, retention을 명시해야 한다.

## A.13 Voice TCK

```text
session close/cancel race
control lane priority
VAD authority uniqueness
false interruption recovery
barge-in to audio stop latency
playback cursor/truncation
provider disconnect/reconnect
raw audio capture default
```

## A.14 OpenAI realtime adapter profile

OpenAI realtime adapter는 provider model/version과 session capabilities를 runtime bind 시점에 조회 또는 선언한다. `gpt-realtime-2` 같은 bidirectional speech-to-speech model을 지원할 수 있지만 GraphBlocks core가 특정 모델명에 의존하지 않는다.

Adapter는 다음을 mapping한다.

```text
session configuration
input audio buffer
server/semantic VAD
conversation items
response audio/text
function/tool calls
output buffer clear
conversation truncation
usage and errors
```

