# Realtime Voice Extension

A duplex voice session binds media transport, input/output formats, VAD
authority, model/provider session identity, interruption policy, and a playback
ledger. Media chunks and control events MUST preserve session and monotonic
sequence identity.

Local VAD may detect a candidate interruption, but when provider authority is
configured it MUST return `continue` until a matching provider confirmation is
received. A confirmation for another provider or session is invalid. A valid
confirmation may interrupt only the active playback entry.

Playback uses immutable `PlaybackEntry` records with these transitions:

```text
queued -> started -> completed -> acknowledged
                  \-> interrupted -> acknowledged
```

Append is idempotent only for identical canonical content. Sequence and
timestamps are monotonic; started, terminal, and acknowledgement timestamps
cannot move backwards. Conflicting acknowledgement reuse MUST fail. Completed or
interrupted entries are immutable except for a matching acknowledgement.

The Python package and Rust runtime-core implement provider-authoritative
interruption and playback acknowledgement. The shared voice TCK MUST consume
provider authority fields when they are present so local VAD remains advisory
until a matching provider confirmation is recorded.
