# P1 mic-stream — live findings (2026-09-23)

Companion to `projects/reachy-relay/P1-RESULTS.md` (full numbers + evidence). Everything here
was measured against the real robot: Reachy Mini Wireless **192.168.1.214**, daemon 1.10.0,
myc fork branch `feature/mic-stream`.

## What shipped

`custom_components/reachy_mini/mic_stream.py` — `MicStreamClient`: a second, independent
WebRTC session (own refcount, **no idle teardown**, auto-reconnect with backoff, gated on
`daemon_state == running`) that receives the mic and fans decoded **16 kHz mono s16 PCM** out
to async consumers. `mic_audio.py` adds the ring buffer + streaming WAV writer;
`tools/mic_capture.py` is the live harness (WAV + stats + JSONL timeline).

Measured on the gateway: 30 min capture, **in-session yield 0.99986**, 6.6 % of one core,
166 MB RSS. Camera sessions (3 fork-client probes + an HA `camera.snapshot`) and a forced
socket drop cost **zero frames**; a dropped socket renegotiates instantly.

## The SDP trap (affects any future "audio-only" consumer)

The robot's offer is **BUNDLE**, and the **video m-line is the bundle transport m-line**:

```
a=group:BUNDLE video0 audio1 application2
m=video 9 ... a=sendrecv ... a=mid:video0     <- transport m-line
m=audio 0 ... a=bundle-only ... a=mid:audio1  (OPUS/48000/2)   <- port 0 + bundle-only is normal
```

| video answer | result |
|---|---|
| `inactive` | **no media at all** — producer never applies the answer (`signaling_state=have-local-offer`), ICE never starts, its 12 s watchdog kills the session |
| `recvonly` (aiortc default) | audio works, but the CM4 encodes video for the listener: 741 video frames / 25 s |
| `sendonly` (shipped) | audio works, **0 video frames** — the m-line stays active, we simply receive nothing |

So: answer audio `recvonly`, video `sendonly`. `MIC_VIDEO_DIRECTION` in `const.py` documents
this; `video_direction=` is the per-client escape hatch if a future robot build disagrees.

## Sleep is not pose-only from HA's side

HA's *Go to sleep* presses `POST /api/daemon/stop?goto_sleep=true`, which stops the daemon's
**backend and media server** — port 8443 closes and the producer disappears (the HTTP API
stays up, which is why the wake button can work). The `daemon_state` gate therefore shuts the
listener down while the robot sleeps and reopens it on wake (measured: 114 s closed, 0 frames,
0 reconnect attempts, then automatic renegotiation on wake).

Consequence for the wake-word plan: an always-on WebRTC ear cannot hear a sleeping robot, so
a wake word cannot be the thing that wakes it. Either accept that (trigger the wake from HA /
presence / a button first), or keep the robot's *pose-only* sleep while the daemon runs — that
would be an integration feature, not a robot-side write.
