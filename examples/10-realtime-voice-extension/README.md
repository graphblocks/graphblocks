# Realtime Voice Extension

This graph composes media input, provider turn detection, local VAD metrics,
interruption policy, model streaming, playout, acknowledgements, and session
telemetry. Provider confirmation remains authoritative for interruption.

```bash
python examples/10-realtime-voice-extension/run.py
```

Validation opens no media session. The voice acceptance application executes
duplex, provider-authority, and playback-ledger probes.
