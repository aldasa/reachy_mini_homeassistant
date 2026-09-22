# DESIGN.md — `reachy_mini.play_audio` for the HA fork

Paths: `HA:` = `reachy_mini_homeassistant/custom_components/reachy_mini/`;
`SDK:` = `reachy_mini/src/reachy_mini/`.

Read-only static study; the robot was not contacted.

---

## 0. Verdict

**Feasible with ZERO daemon changes — two independent routes, both already
supported by daemon 1.11.0.dev0 source.**

| Route | Mechanism | Daemon change | Risk |
|---|---|---|---|
| **A — WebRTC uplink** (audio track on the integration's existing aiortc session) | Answer the robot's already-offered `sendrecv` audio m-line as *sending*, attach an Opus track | none | medium-high (new interop territory) |
| **A′ — REST upload+play** (found during this study) | `POST /api/media/sounds/upload` → `POST /api/media/play_sound` | none | low (proven path, used by the official SDK client) |

**Recommendation: implement A′ as the default transport for `play_audio`
(file/TTS playback) and A as the low-latency streaming path behind one
config option.** Path B (companion app / daemon patch) is **not required** for
the requested feature. The only upstream asks are conveniences (§6.4).

The daemon is already a *bidirectional* WebRTC endpoint that plays consumer
audio on the speaker — this is a documented, shipped feature (mobile/desktop
app voice leg), not something the fork must invent.

---

## 1. Q1 — How the camera session works today

### Ownership / lifecycle

- One shared `ReachyMiniStreamClient` per config entry, created in
  `HA:camera.py:29-40` and torn down at unload via `entry.async_on_unload`
  (`HA:camera.py:37`). All HA consumers (stills, MJPEG viewers) share it.
- Consumer refcount: `acquire()` (`HA:stream.py:145`), `release()`
  (`HA:stream.py:166`); idle teardown arms at zero consumers after
  `CAMERA_IDLE_TIMEOUT = 10.0 s` (`HA:const.py:103`), with a generation counter
  so a fired-but-stale idle timer cannot kill a fresh session
  (`HA:stream.py:127-143`). Cooldown `CAMERA_RETRY_COOLDOWN = 5.0 s`
  (`HA:const.py:107`) suppresses reconnect storms after a robot-side failure.
- One `RTCPeerConnection` **per session**, built in `_run_session`
  (`HA:stream.py:254`) via the injectable `pc_factory`, and closed in the
  `finally` block (`HA:stream.py:~295`). A single websocket to
  `ws://<host>:8443` (`HA:const.py:98`, `ws_connect` at `HA:stream.py:260`).
- DTLS interop: aiortc's `RTCConfiguration` has **no `certificates` field**
  (verified in the vendored wheel: only the internal default at
  `rtcpeerconnection.py:295`), so the client mutates the private attribute
  `pc._RTCPeerConnection__certificates` (`HA:stream.py:52-59`) to install an
  RSA-capable cipher list (`HA:const.py:117-124`) — the robot's Linux gst build
  has an RSA DTLS cert (see `HA:stream.py:20-24`).

### Signalling: producer-offers, consumer-answers (NOT read-only)

`HA:stream.py:306-376` implements the gst-webrtc-signalling protocol against
the signalling server that `webrtcsink` runs itself
(`SDK:media/media_server.py:371` `run-signalling-server=True`; the relay's
upstream is its loopback twin, `SDK:media/central_signaling_relay.py:51`):

`welcome` → `list` → pick producer whose `meta.name == "reachymini"`
(`HA:const.py:99`) → `startSession{peerId}` → `sessionStarted{sessionId}` →
robot sends **`peer` with its SDP offer** → HA answers
(`HA:stream.py:337-360`: `setRemoteDescription` → `createAnswer` →
`setLocalDescription`) → local ICE candidates are trickled as individual `ice`
messages because gst `webrtcbin` ignores in-SDP candidates
(`HA:stream.py:22-26`, `360-372`).

**Consequence for Q1's core question:** the consumer cannot *add* an m-line —
SDP answers may only accept/reject what was offered, and the offerer here is
the robot. But the robot **already offers a negotiable audio m-line**:

- `SDK:media/media_server.py:444-445` — `_enable_audio_receive(webrtcbin)` is
  called in `consumer-added`, explicitly *"before SDP offer is generated"*.
- `SDK:media/media_server.py:482` `_enable_audio_receive` walks every
  transceiver and forces `GstWebRTCRTPTransceiverDirection.SENDRECV` (value 4),
  so the offer advertises `a=sendrecv` on the mic/audio m-line.
- Incoming consumer RTP is then picked up at
  `SDK:media/media_server.py:527` `_on_consumer_pad_added` (src pad, `media ==
  "audio"`) and forwarded, via a `DROP`-returning pad probe, into a separate
  playback pipeline (`appsrc → rtpopusdepay → opusdec → audiosink`). The
  original pipeline is untouched (adding elements to `webrtcsink`'s internal
  pipeline crashes it — comment at `SDK:media/media_server.py:527-535`).
- `RX_JITTER_LATENCY_MS = 300` (`SDK:media/media_server.py:156`) is set on each
  consumer's `webrtcbin` for exactly this phone→robot leg.

So: **the daemon is not read-only; it accepts uplink audio from any consumer
that answers the offered audio m-line as sending.** No client can inject a new
track, but no new track is needed.

Today the integration answers that m-line as *receiving only*, because aiortc
creates an implicit transceiver with `direction="recvonly"` for a remotely
offered m-line (`aiortc/rtcpeerconnection.py:897`). The mic audio the robot
sends is therefore already arriving (and being decoded) on HA, and is silently
discarded: `_on_track` only acts on `track.kind == "video"`
(`HA:stream.py:378-382`).

---

## 2. Q2 — SDK WebRTC audio path + REST alternatives

### 2.1 Who sends the audio

The class that actually pushes samples over the wire for a WebRTC session is
**`GstWebRTCClient`** (`SDK:media/webrtc_client_gstreamer.py`, `CameraBase +
AudioBase`), assigned to both `MediaManager.camera` and `MediaManager.audio`
when `media_backend="webrtc"` (`SDK:media/media_manager.py:217-233`,
port 8443 at `:174`).

- It consumes the producer via `webrtcsrc` with
  `signaller.producer-peer-id` + `uri=ws://host:8443`
  (`SDK:media/webrtc_client_gstreamer.py:168-182`).
- On the consumer's own `webrtcbin` it forces transceivers to `SENDRECV`
  (`:193-218`) and builds the send chain
  (`:362-505`): `appsrc → queue → audioconvert → audioresample → audiomixer
  (+ silent audiotestsrc keepalive) → capsfilter → opusenc → rtpopuspay →
  webrtcbin audio sink pad` (payload type read off the negotiated OPUS pad,
  `:400-414`).
- `start_playing()` is a no-op (`:507`) because the chain is created on
  `pad-added`; `push_audio_sample(np.float32)` feeds the appsrc
  (`SDK:media/audio_base.py:294-310`).
- Codec/format the daemon expects on the uplink: **Opus, 48 kHz, stereo,
  10 ms frames with `audio-type=restricted-lowdelay`** (`:462-466`), and the
  mixer is pinned to `audio/x-raw,rate=16000,channels=2` caps *specifically so
  opusenc/rtpopuspay advertise `sprop-maxcapturerate=16000` and stereo
  `encoding-params`, which is what the negotiated `webrtcbin` OPUS sink pad
  wants — otherwise `not-negotiated` the moment audio flows* (`:441-456`).
  Constants: `SAMPLE_RATE = 16000`, `CHANNELS = 2`
  (`SDK:media/audio_base.py:116-117`); AEC path uses S16LE 48 kHz stereo
  (`SDK:media/audio_base.py:46-47`).
- SDK-level format contract for the *local* backend:
  `push_audio_sample()` expects `(samples, 1 or 2)` `float32` @ 16 kHz
  (`docs/source/SDK/python-sdk.md:116-120`).
- `GstWebRTCClient.play_sound()` (`:552-583`) does **not** use WebRTC: it
  uploads a local file (`upload_sound`, `:585`) and POSTs to the daemon. This
  is the official precedent for route A′.

### 2.2 REST endpoints that already exist (daemon router `SDK:daemon/app/routers/media.py`, prefix `/media`, mounted under `/api` at `SDK:daemon/app/main.py:312-333`)

| Endpoint | Line | Notes |
|---|---|---|
| `POST /api/media/play_sound` | `media.py:77` | body `{"file": ...}`; absolute path, an assets filename, or a basename previously uploaded; **503 if `backend.ready` is unset** (`:92-94`) |
| `POST /api/media/stop_sound` | `media.py:105` | stops the playbin |
| `POST /api/media/clear_incoming_audio` | `media.py:118` | barge-in flush of WebRTC-received audio (`media_server.py:723`) |
| `POST /api/media/sounds/upload` | `media.py:207` | multipart; saved to `/tmp/reachy_mini_sounds/<name>` (`:27`); extension allow-list `.wav .mp3 .ogg .oga .opus .flac .m4a .aac` (`:32`), ≤**25 MiB** (`:38`), content-validated by a GStreamer discoverer probe (`is_valid_audio_file`, `SDK:media/gstreamer_utils.py:87-116`) |
| `GET /api/media/sounds`, `DELETE /api/media/sounds/{name}` | `:268`, `:279` | list/remove uploads |
| `POST /api/media/wobbling/{enable,disable}` | `:134`, `:152` | head-wobble on playback |
| `POST /api/media/release` / `/acquire`, `GET /status` | `:41`, `:48`, `:55` | hardware hand-off |

Playback itself is `GstMediaServer.play_sound()` (`SDK:media/media_server.py:1344`):
a `playbin` with a platform-aware sink tee'd to the head wobbler; it replaces
any previous `playbin` (one file at a time). Backend delegation:
`SDK:daemon/backend/abstract.py:1176-1206` (no-op when `_media_server is None`).

**So yes: a REST play-audio path exists and is the cheapest route.**

---

## 3. Q3 — Is the vendored wheel sufficient for outbound Opus on HA OS?

`wheels/README.md` is accurate: the wheel is a **byte-identical repackage of
upstream pure-Python aiortc 1.14.0 with one metadata line relaxed** — the cap
`av<17` becomes `av<19` (`aiortc-1.14.0+av17.dist-info/METADATA`:
`Requires-Dist: av<19,>=14.0.0`). Contents are stock (`aiortc/codecs/opus.py`,
`rtcrtpsender.py`, `contrib/media.py`, …). It is a `py3-none-any` wheel, so
there is nothing platform-specific or patched about media handling.

Sending audio needs:

1. `RTCRtpSender.replaceTrack` — present (`aiortc/rtcrtpsender.py:194`).
2. `RTCRtpTransceiver.direction` setter — present
   (`aiortc/rtcrtptransceiver.py:68-71`), fed into the answer via
   `and_direction(transceiver.direction, transceiver._offerDirection)`
   (`rtcpeerconnection.py:577-580`).
3. Sender auto-start when the negotiated direction allows it:
   `__connect()` starts the sender only for `currentDirection in
   ["sendonly","sendrecv"]` (`rtcpeerconnection.py:1070`), and
   `currentDirection = and_direction(direction, _offerDirection)` is set on
   `setLocalDescription` of the answer (`:838-841`). **That is exactly the
   hook the fork needs.**
4. An Opus encoder: `get_encoder` maps `audio/opus` → `OpusEncoder`
   (`aiortc/codecs/__init__.py:172`), which is `av.CodecContext.create("libopus",
   "w")` at 48 kHz/stereo/s16 with `application=voip`
   (`aiortc/codecs/opus.py:26-40`). Opus is also the codec aiortc advertises by
   default at pt 96, 48000, 2ch (`codecs/__init__.py:34`) — matching the robot's
   negotiated pt/codec.

Dependency footprint: `aioice>=0.10.1`, `av>=14`, `cryptography>=44`,
`google-crc32c`, `pyee>=13`, `pylibsrtp>=0.10`, `pyopenssl>=25` (METADATA).
These are **already installed and already running** (the camera works live per
the module docstrings), so route A adds **no new HA requirement** —
`manifest.json` stays as-is. No new wheels, nothing needing the HA-OS toolchain.

Two things to verify that static reading cannot settle:

- The **libopus encoder** must be present inside HA's `av` wheel. PyAV's binary
  wheels bundle an FFmpeg with libopus enabled (this is what makes aiortc's own
  Opus sender work anywhere), but confirm empirically in the HA container:
  `python -c "import av; print(av.Codec('libopus','w').long_name)"`.
- Route A′ needs only container/muxer support (`wav` + `pcm_s16le`), which is
  unconditional in any FFmpeg build — so **A′ has no codec-availability risk at
  all**, independent of the libopus question. HA also has an optional `ffmpeg`
  integration, but nothing here should depend on an `ffmpeg` *binary*: PyAV is
  the guaranteed dependency.

---

## 4. Q4 — App-slot / remote-session lock vs a second media session from HA

`RobotAppLock` (`SDK:daemon/robot_app_lock.py:64`) is explicitly **not** a lock
on all robot access — its own docstring (`:1-27`) says SDK clients over
LAN/WebSocket *"bypass it entirely"*; it only serialises the two **managed**
entry points:

- local apps launched by `AppManager` → `acquire_local_evicting_remote`
  (`:203`), state `local_app`;
- remote WebRTC clients handled by the **central signalling relay** →
  `try_acquire_remote("remote")` (`:311`), state `remote_session`.

States: `free | local_app | remote_session` (`:45-50`). Transitions are driven
by `AppManager.start_app`/exit and by the relay's `startSession`/`endSession`
(`central_signaling_relay.py:1368`, `:1413`, `:1473`). A local-app acquire
evicts a remote session (`endSession` pushed to the peer, `:306-331`); a remote
acquire is refused while a local app runs (`:1413-1420`). Slot release to
`free` triggers a daemon idle reset (`_fire_became_free`, `:144`).

Key facts for this fork:

1. **HA's camera session bypasses the lock.** The integration dials the
   `webrtcsink` signalling server directly (`ws://<robot>:8443`) — the same
   endpoint the relay uses as its *loopback* upstream
   (`central_signaling_relay.py:51` `ws://127.0.0.1:8443`) — and never goes
   through central. The integration's own comment says so
   (`HA:coordinator.py:22-27`: *"camera-feed consumption also uses WebRTC but
   never takes the slot"*), and `_derive_app_slot` maps `remote_session` from
   the lock, not from WebRTC presence. So HA's camera coexists with a local app
   and with a phone session; the README claim holds.
2. **Media multiplexing is per-consumer on the daemon side.** Each consumer
   connection creates its own `webrtcbin` (`consumer-added`,
   `media_server.py:415`), so a second consumer (HA audio) does not disturb the
   first — but each costs its own encode.
3. **Requested-but-limited: incoming audio is single-slot in the daemon.**
   `self._pipeline_playback` is one attribute (`media_server.py:527+`) and
   `clear_incoming_audio()` flushes "the" pipeline (`:723`); `_incoming_audio`
   is per-peer, so two concurrent uplinks *technically* each get a pipeline,
   but the AEC far-end probe and the flush semantics are single-slot (comments
   at `:565-580` and `:723-740`). **Design rule: exactly one uplink per robot —
   HA and the mobile app must not both stream audio.**
4. **Two independent sink owners.** `play_sound()` owns a `playbin`
   (`media_server.py:1344-1400`) while WebRTC-received audio owns the probe
   pipeline (`:527+`). Both open the speaker. Mixing routes A and A′ at the same
   moment is undefined; the service must serialise.
5. **Sleep kills everything.** Waking/sleeping is backend start/stop
   (`SDK:daemon/daemon.py:435-440` start media; `:499-553` stop), and every
   media route 503s unless `backend.ready` (`routers/media.py:92-94`). HA's
   `camera.available` already gates on `daemon_state == running`
   (`HA:camera.py:74-78`); `play_audio` must gate identically.

---

## 5. Implementation plan (file by file)

### 5.1 Routing policy (both transports, one service)

```
play_audio(media=…) ──┬─ transport=auto (default)
                      │   file/URL/TTS → REST (A′): decode→WAV→upload→play_sound
                      │   live PCM     → WebRTC (A): uplink track
                      ├─ transport=rest   → force A′
                      └─ transport=webrtc → force A (PCM; re-encode of files)
```

`auto` = **REST unless a caller supplies live PCM**, because A′ needs no
session, no ICE, no keepalive, and works whether or not a dashboard is open.

### 5.2 New / changed files

| File | Kind | Content |
|---|---|---|
| `HA:const.py` | edit | `SERVICE_PLAY_AUDIO = "play_audio"`; `ENDPOINT_*` for `/api/media/{sounds/upload,play_sound,stop_sound,clear_incoming_audio,status}`; `AUDIO_UPLINK_RATE=48000`, `_CHANNELS=2`, `_FRAME_SAMPLES=960`, `_KEEPALIVE=True`; `WAV_SAMPLE_RATE=16000`, `WAV_CHANNELS=1` |
| `HA:audio_transcode.py` | **new** | PyAV-only helpers: `fetch_bytes(hass, url)` (HA client session, size caps, http/https allow-list), `read_media_bytes(hass, media_id)` for `media_source`/`/media/...`, `to_wav_s16(bytes, rate=16000, channels=1) -> bytes`, `pcm_stream(bytes) -> AsyncIterator[bytes]` (48 kHz s16 stereo frames of 960 samples) |
| `HA:audio_track.py` | **new** | `PushAudioTrack(MediaStreamTrack)` — `kind="audio"`, bounded `asyncio.Queue` of int16 PCM, `recv()` paced at 20 ms, silence when idle, never raises `MediaStreamError` (sketch §5.4) |
| `HA:stream.py` | edit | Optional `audio_uplink: PushAudioTrack \| None` ctor arg; in `_signalling_loop` (before `createAnswer`, `HA:stream.py:340`) call new `_attach_audio_uplink(pc)`; expose `async_send_audio(bytes)` / `async_wait_idle()`; keep the 10 s idle teardown but let the service hold an `acquire()` for the utterance duration |
| `HA:services.py` | edit | register `play_audio` in `async_register_services`; `PLAY_AUDIO_SCHEMA`; extend `_coordinators_for_devices` (`services.py:33-50`) to accept `entity_id` via the entity registry; per-robot `asyncio.Lock` to serialise utterances |
| `HA:services.yaml` | edit | `play_audio` UI definition, `selector: media` for the picker (§5.3) |
| `HA:media_player.py` | **new (stretch)** | `ReachyMiniMediaPlayer(ReachyMiniEntity, MediaPlayerEntity)`: `PLAY_MEDIA` + `STOP`; `async_play_media(media_type, media_id)` → route A′; `async_media_stop` → `POST /api/media/stop_sound`; `extra_state_attributes` from `/api/media/status` |
| `HA:__init__.py` | edit | add `Platform.MEDIA_PLAYER` to `PLATFORMS` (`__init__.py:36-43`) |
| `HA:strings.json`, `HA:translations/en.json` | edit | service name/fields + media_player entity translation keys (mirror existing `camera`/`button` style) |
| `HA:manifest.json` | **no change** | aiortc wheel URL already pinned; no new requirement (§3) |
| `HA:README.md` | edit | document `reachy_mini.play_audio`, `media_player.reachy_mini`, transport choice, sleep gating |
| `HA:tests/test_play_audio.py`, `tests/test_audio_uplink.py`, `tests/test_media_player.py` | **new** | `aioclient_mock` for new REST endpoints (`tests/conftest.py` pattern); `pc_factory` injection (`HA:stream.py:104-113`) to assert the answer SDP carries `a=sendonly`/`sendrecv` on the audio m-line and that `replaceTrack` was called (`tests/test_stream.py` already proves the fake-PC/browser-free style) |

### 5.3 Service schema

```yaml
play_audio:
  name: Play audio
  description: >-
    Play audio on a Reachy Mini's speaker. `media` accepts a /media path,
    an http(s) URL (e.g. a TTS cache file), or a media-source id.
  target:
    entity: { integration: reachy_mini, domain: media_player }
    device: { integration: reachy_mini }
  fields:
    media: { name: Media, required: true, selector: { media: } }   # picker; URLs typed freely
    transport:
      name: Transport
      default: auto
      selector: { select: { options: [auto, rest, webrtc] } }
    volume:
      name: Volume override (0-100)
      selector: { number: { min: 0, max: 100, unit_of_measurement: "%" } }
    keepalive: { name: Keep session warm until the utterance ends, default: true, selector: { boolean: } }
    wait: { name: Wait for completion, default: true, selector: { boolean: } }
```

Python schema (`HA:services.py`), following the existing
`vol.Required(ATTR_DEVICE_ID)` + `cv.ensure_list` idiom (`services.py:25-31`):

```python
PLAY_AUDIO_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): vol.All(cv.ensure_list, [cv.entity_id]),
    vol.Optional(ATTR_DEVICE_ID):  vol.All(cv.ensure_list, [cv.string]),
    vol.Required(ATTR_MEDIA): cv.string,      # url | /media/... | media-source id
    vol.Optional(ATTR_TRANSPORT, default="auto"): vol.In(("auto", "rest", "webrtc")),
    vol.Optional(ATTR_VOLUME): vol.All(vol.Coerce(int), vol.Range(0, 100)),
    vol.Optional(ATTR_KEEPALIVE, default=True): cv.boolean,
    vol.Optional(ATTR_WAIT, default=True): cv.boolean,
})
```

Handler outline (route A′ — the default):

```python
async def _handle_play_audio(call: ServiceCall) -> None:
    coordinators = _coordinators_for_call(hass, call)      # entity_id | device_id
    raw = call.data[ATTR_MEDIA]
    media = await _resolve_media(hass, raw)                # URL fetch or media_source
    if call.data[ATTR_VOLUME] is not None:
        await coordinators[0].async_post("/api/volume/set", body={"volume": call.data[ATTR_VOLUME]})
    wav = await hass.async_add_executor_job(to_wav_s16, media, 16000, 1)   # PyAV, ffmpeg-free
    for coordinator in coordinators:
        async with _lock_for(coordinator):
            await coordinator.async_upload_sound(wav, filename=f"ha_{digest(wav)[:8]}.wav")
            await coordinator.async_post(ENDPOINT_PLAY_SOUND, body={"file": name})
```

`async_upload_sound` posts multipart to
`/api/media/sounds/upload` with the coordinator's session and returns the
daemon-side absolute path; the file must carry an allow-listed extension
(`media.py:32`) and pass the discoverer probe (`gstreamer_utils.py:87`), so WAV
(PCM s16, 16 kHz mono = 32 kB/s; 25 MiB ≈ 13 min) is the safe default and also
the smallest guaranteed-present encoder.

### 5.4 Code sketches (the hard parts)

**(a) `PushAudioTrack` — compatible with the vendored aiortc**

```python
from av import AudioFrame
from aiortc.mediastreams import MediaStreamTrack

RATE, CHANNELS, SAMPLES = 48000, 2, 960          # 20 ms, matches aiortc's OpusEncoder resampler
FRAME_S = SAMPLES / RATE

class PushAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)   # ~1 s of backlog
        self._pts = 0
        self._silence = bytes(SAMPLES * CHANNELS * 2)
        self._last_push = 0.0
        self.active = False

    async def push(self, pcm_s16: bytes) -> None:
        """Called by the service; re-chunked to whole 20 ms frames."""
        for i in range(0, len(pcm_s16), SAMPLES * CHANNELS * 2):
            chunk = pcm_s16[i:i + SAMPLES * CHANNELS * 2]
            if len(chunk) < SAMPLES * CHANNELS * 2:
                chunk += bytes(SAMPLES * CHANNELS * 2 - len(chunk))
            try:
                self._q.put_nowait(chunk)
            except asyncio.QueueFull:
                self._q.get_nowait()          # drop oldest: live audio, not a file transfer
                self._q.put_nowait(chunk)
        self._last_push = time.monotonic()

    async def recv(self) -> AudioFrame:
        # Never raise MediaStreamError: that permanently stops aiortc's sender
        # (see aiortc/rtcrtpreceiver.py / mediastreams.MediaStreamError contract).
        try:
            payload = await asyncio.wait_for(self._q.get(), timeout=FRAME_S)
            self.active = True
        except TimeoutError:
            payload = self._silence            # keepalive: mirrors the SDK's live audiotestsrc
            self.active = False
        await asyncio.sleep(FRAME_S)           # pace; the robot's sink is sync=True
        frame = AudioFrame(format="s16", layout="stereo", samples=SAMPLES)
        frame.sample_rate = RATE
        frame.planes[0].update(payload)
        frame.pts, frame.time_base = self._pts, Fraction(1, RATE)
        self._pts += SAMPLES
        return frame
```

Notes tied to evidence: format/layout are constrained by
`aiortc/codecs/opus.py:36-45` (`s16`, `mono|stereo`, internal resampler to
48 kHz/960); `av` accepts `s16` packed via `planes[0]`. If `keepalive` is off,
return only what is queued and accept that the robot's `appsrc → opusdec →
audiosink` chain restarts cold (the SDK's keepalive exists precisely to avoid
"first-word swallowing" — `SDK:media/webrtc_client_gstreamer.py:425-437`).
Silence at Opus 96 kbps stereo ≈ 12 kB/s — negligible on a LAN, but expose the
toggle.

**(b) Session handshake reuse — attach the uplink while answering**

```python
# HA:stream.py — inside _signalling_loop, the "peer"/sdp branch (~line 337)
elif mtype == "peer" and "sdp" in msg:
    await pc.setRemoteDescription(
        RTCSessionDescription(msg["sdp"]["sdp"], msg["sdp"]["type"])
    )
    self._attach_audio_uplink(pc)                      # NEW, before createAnswer
    await pc.setLocalDescription(await pc.createAnswer())
    ...

def _attach_audio_uplink(self, pc: RTCPeerConnection) -> None:
    """Answer the robot's already-offered audio m-line as a sender.

    aiortc creates implicit transceivers with direction='recvonly' for a
    remotely offered m-line (rtcpeerconnection.py:897), and the answer
    direction is and_direction(direction, _offerDirection) (:577).
    Bumping direction to sendonly/sendrecv is what makes
    _RTCRtpSender.send() run (:1070).
    """
    if self._audio_uplink is None:
        return
    for transceiver in pc.getTransceivers():
        if transceiver.kind != "audio" or transceiver.mid is None:
            continue
        transceiver.direction = "sendonly"        # robot→us mic audio stays off
        transceiver.sender.replaceTrack(self._audio_uplink)
        return                                     # one audio m-line in the offer
```

Rationale for `sendonly` over `sendrecv`: we need uplink only; `sendonly` means
the robot's mic audio is not sent to HA at all (today it is sent and dropped,
`HA:stream.py:378`). Both are legal answers; if the hardware test shows gst
`webrtcbin` refusing the answer, fall back to `sendrecv`. `addTrack()` must
**not** be used: it would try to add an m-line the offer does not contain.

**(c) 16 kHz mono float32 → Opus (via s16 48 kHz stereo)**

```python
def to_uplink_pcm(src: bytes) -> list[bytes]:
    """Decode any container PyAV can read → 48 kHz stereo s16, 960-sample chunks."""
    with av.open(io.BytesIO(src)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(
            format="s16", layout=f"{AUDIO_UPLINK_CHANNELS}c", rate=AUDIO_UPLINK_RATE
        )
        out: list[bytes] = []
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):     # handles 16k mono float32 input
                out.append(bytes(resampled.planes[0]))
        for resampled in resampler.resample(None):          # flush
            out.append(bytes(resampled.planes[0]))
    return out
```

The daemon needs no 16 kHz input on the WebRTC path: `opusdec` in its playback
pipeline emits S16LE and the branch resamples to the device
(`SDK:media/media_server.py:585-640`). The 16 kHz contract in
`python-sdk.md:116` applies to the *local* `push_audio_sample` backend, which
the WebRTC client satisfies by resampling internally.

**(d) media_player (stretch) — TTS-native target**

```python
class ReachyMiniMediaPlayer(ReachyMiniEntity, MediaPlayerEntity):
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.STOP

    async def async_play_media(self, media_type, media_id, **kwargs) -> None:
        url = async_process_play_media_url(self.hass, media_id)   # TTS hands us a URL
        await self._client.play_url(url)                          # shared A′ code path
    async def async_media_stop(self) -> None:
        await self.coordinator.async_post("/api/media/stop_sound")
```

That makes `tts.speak` → `media_player.reachy_mini` work with no glue
automation. Browse support (stretch²) can list `/api/media/sounds` via
`async_browse_media`.

---

## 6. Risks

1. **No audio m-line means no uplink.** If the robot's capture chain fails,
   `_configure_audio` returns early (`media_server.py:1172-1190`, "Streaming
   video only; audio will be unavailable") and the offer carries video only —
   route A becomes impossible on that session. A′ is unaffected.
2. **Unproven interop direction.** Today only `webrtcsrc` (SDK) and browsers
   exercise the robot's receive path; no aiortc peer has ever *sent* to this
   daemon. The failure could be anywhere between the SDP answer and the
   `pad-added` at `media_server.py:527`.
3. **Codec/PT negotiation details.** The SDK pins 16 kHz/stereo mixer caps so
   `opusenc` advertises `sprop-maxcapturerate`/`encoding-params=2` matching the
   negotiated sink pad, warning that mismatch ⇒ `not-negotiated`
   (`webrtc_client_gstreamer.py:441-456`). aiortc sends 48 kHz stereo Opus at
   pt 96 (`codecs/__init__.py:34`) — semantically identical wire format, but the
   caps comparison on the gst side is unverified here.
4. **No in-band FEC from aiortc.** `OpusEncoder` sets only `application=voip`
   (`codecs/opus.py:29-33`) while the daemon's `opusdec` enables
   `use-inband-fec` + `plc` (`media_server.py:585-600`). Decoding still works;
   Wi-Fi loss just won't be reconstructed. Mitigation: keepalive + PLC on the
   robot side.
5. **One uplink slot in the daemon** (`self._pipeline_playback`,
   `media_server.py:527+`; `clear_incoming_audio` `:723`; single AEC far-end
   probe `:565-580`): serialise HA audio, and never alongside a phone/desktop
   session's voice leg.
6. **Two sink owners**: `playbin` (`:1344`) vs the incoming-audio pipeline —
   mixing A and A′ concurrently is undefined.
7. **Session lifetime vs utterance length.** Teardown arms 10 s after the last
   `release()` (`const.py:103`); failures arm a 5 s cooldown (`const.py:107`).
   `play_audio` must hold an `acquire()` for the whole utterance and surface a
   distinct error on cooldown instead of silently failing.
8. **Robot-side ICE watchdog: 12 s** (`media_server.py:64-70`) — the new answer
   path must negotiate inside that budget; a slow first answer looks like an
   ICE timeout.
9. **Sleep/idle reset.** Slot→free fires a daemon idle reset
   (`robot_app_lock.py:55-62`, `:144`); sleep stops media (`daemon.py:499-553`)
   and all media routes then 503 (`routers/media.py:92-94`). `play_audio` must
   gate on `awake` (like `camera.available`, `HA:camera.py:74-78`) and raise a
   "wake the robot first" `ServiceValidationError`.
10. **Upload constraints (A′).** Extension allow-list + 25 MiB cap + discoverer
    probe (`media.py:32-38`, `:87-116`): malformed WAV ⇒ 400. Encode with PyAV
    (always available) rather than shelling out to `ffmpeg`; don't assume an
    `ffmpeg` binary in HA OS core.
11. **HA-side serialisation & resource use.** Fetch/decode/re-encode is
    CPU-bound on the HA host: keep it in `async_add_executor_job` and take a
    per-coordinator lock for multi-robot/multi-utterance calls.
12. **The direction change is a behaviour change.** `sendonly` stops the
    robot's mic audio reaching HA; nothing else consumes it (voice/DOA use REST
    `/api/state/doa`, `coordinator.py:230-236`), but confirm live.

---

## 7. Test plan (real hardware, 3 steps)

your HA instance, integration under the HACS path
`custom_components/reachy_mini/`.

**Step 1 — Wake + REST utterance (proves A′ end-to-end, isolates transport).**
Wake via `button.living_room_reachy_mini_wake_up`; confirm the awake sensors
flip and `camera` becomes available. Call `reachy_mini.play_audio` with a 3 s
local WAV (`transport: rest`). Pass criteria: `POST /api/media/sounds/upload`
→ 200 with a `/tmp/reachy_mini_sounds/...` path, `POST /api/media/play_sound`
→ 200, audio audible, daemon logs `Using ALSA device reachymini_audio_sink for
playback` (`media_server.py:1415-1420`). Repeat with an MP3 and an OGG for the
transcoder. No daemon file may need editing for this step — that is the point.

**Step 2 — WebRTC uplink on the fork's own session (proves A / the risky half).**
With no dashboard open, call `play_audio(transport: webrtc)`; the service must
establish the session itself (acquire → `startSession`), then stream PCM.
Verify, in order: (a) HA logs an answer SDP with `a=sendonly` and `m=audio` pt 96
opus/48000/2 (assert in the unit test too); (b) daemon logs `Consumer pad: …
media=audio` then `Audio playback pipeline started for peer <id>`
(`media_server.py:543`, `:660`); (c) audible playback, no first-word clipping;
(d) repeat with a dashboard live view open (one shared session), then close it
and confirm the 10 s idle teardown does not cut the next utterance. If (b)
never fires, try in order: `sendrecv` instead of `sendonly`; keepalive on/off;
diff the robot's offer m=audio line against our answer.

**Step 3 — Integration behaviour (media_player + coexistence + lifecycle).**
`tts.speak` targeting `media_player.reachy_mini` (three utterances back to
back, then one after >10 s idle, then one while a management app is running and
while the phone/desktop app holds the session). Pass criteria: all utterances
audible; `active_app`/`remote_session_active` state is unchanged by HA audio
(no app-slot churn — `robot_app_lock.py:311`, `:337`); `stop` produces
immediate silence via `/api/media/stop_sound`; with the robot asleep,
`play_audio` fails with a clear "wake the robot first" error rather than a
timeout.

---

## 8. Single biggest uncertainty (real-hardware only)

**Whether gst `webrtcbin` on the robot will actually deliver a playable
incoming-audio pad when the *answerer* is aiortc and the answer flips the audio
m-line from the current `recvonly` to `sendonly`/`sendrecv`** — i.e. whether the
daemon's `pad-added` → pad-probe → `rtpopusdepay`/`opusdec` chain
(`media_server.py:527-660`) accepts an aiortc-originated Opus stream at pt 96,
48 kHz stereo, with no in-band FEC and no `sprop-*` hint, inside the 12 s ICE
watchdog. Static reading proves every hook exists and that the direction change
is exactly what starts aiortc's sender (`rtcpeerconnection.py:577`, `:1070`),
but only the real robot can show that the gst-side caps negotiation agrees — and
route A′ (REST upload + `play_sound`) is the guaranteed fallback if it does not.
