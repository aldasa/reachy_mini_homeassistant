# BUILD_NOTES.md — `play_audio`, phase 1 (route A′ / REST transport)

Scope for this build: the REST transport only (`transport: rest` / `auto`),
per the build brief. Everything below is a deviation from DESIGN.md or a
deliberate deferral. Nothing here changes the design's conclusions.

## Implemented

| File | What |
|---|---|
| `custom_components/reachy_mini/const.py` | service/field names, `/api/media/*` endpoints, the daemon's extension allow-list and 25 MiB cap (mirrored), fetch cap, WAV format, timeouts |
| `custom_components/reachy_mini/audio_transcode.py` | **new** — media resolution (`http(s)`, `/media` + `/local` paths, `media-source://`), size-capped fetch, `to_wav_s16()` PyAV decode → 16 kHz mono s16 WAV, content-addressed upload naming, upload-cap guard |
| `custom_components/reachy_mini/services.py` | `play_audio`: target resolution (device **and** entity), awake gate, per-robot `asyncio.Lock`, volume override, upload → `play_sound` |
| `custom_components/reachy_mini/coordinator.py` | `async_upload_sound()` — multipart POST to `/api/media/sounds/upload` |
| `services.yaml`, `strings.json`, `translations/en.json` | service definition + translated name/description/fields |
| `README.md` | usage, TTS/sanotts automation examples, troubleshooting, design note |
| `tests/test_play_audio.py` | **new** — 32 tests |

## Deviations from DESIGN.md

1. **`async_upload_sound` lives on the coordinator, not in `services.py`.**
   DESIGN §5.3's handler outline already calls
   `coordinator.async_upload_sound(...)`, and the coordinator is the object
   that owns the aiohttp session and the base URL — so the method went there
   rather than into a module-level helper. No behavioural difference.

2. **Endpoint constants are prefixed `ENDPOINT_MEDIA_*`**
   (`ENDPOINT_MEDIA_SOUNDS_UPLOAD`, `ENDPOINT_MEDIA_PLAY_SOUND`,
   `ENDPOINT_MEDIA_STOP_SOUND`, `ENDPOINT_MEDIA_CLEAR_INCOMING_AUDIO`,
   `ENDPOINT_MEDIA_STATUS`) rather than DESIGN's generic `ENDPOINT_*`, to match
   the file's existing style (`ENDPOINT_VOLUME_*`, `ENDPOINT_MOVE_*`).

3. **Media resolution is broader than "URL / media-source".** DESIGN §5.2 asks
   for URL + media-source; because a `media-source://tts/...` id resolves to a
   *relative* `/api/tts_proxy/...` URL, `read_media` also maps `/media/...`,
   `/local/...` and config-relative paths, and — when a relative reference is
   not a readable local file — fetches it from HA's own
   `internal_url`/`external_url`. Without that last step the TTS path (the main
   motivation for the feature) would resolve to a path that does not exist.
   `file://` is deliberately *not* accepted.

4. **`transport: webrtc` raises `ServiceValidationError`** instead of silently
   falling back to REST. DESIGN §5.1 defines the option; this phase cannot
   honour it, so it fails loudly, with a TODO at the raise site.

5. **`keepalive` and `wait` are accepted and ignored.** They are meaningful
   only for the WebRTC uplink (session warming, end-of-utterance wait), which
   is out of scope; `play_sound` is fire-and-forget. Both stay in the schema so
   the service contract in §5.3 does not change when route A lands.

6. **Upload filenames are content-addressed** (`ha_<sha256[:8]>.wav`) instead
   of DESIGN's unspecified `digest()` — same shape, pinned algorithm, and
   replaying an asset overwrites its previous upload rather than filling the
   robot's temp directory.

7. **Error surface.** Every caller-actionable failure is `AudioSourceError`, a
   subclass of `ServiceValidationError`, so HA renders it as a validation
   error. A daemon 503 (a sleep racing the last poll) is mapped to the same
   "wake the robot first" message as the awake gate.

## Deliberately deferred (out of scope for this phase)

- `media_player.py` + `Platform.MEDIA_PLAYER` (DESIGN §5.2 stretch, plus the
  `__init__.py` change) — `tts.speak` therefore cannot target the robot yet;
  the README documents the `media-source://tts/...` → `play_audio` pattern
  instead.
- Route A (WebRTC uplink): `audio_track.py`, `stream.py`'s
  `_attach_audio_uplink`, `pcm_stream()` / `to_uplink_pcm()`, and the
  48 kHz/stereo uplink constants — all absent from this build.
- `stop_sound` / `clear_incoming_audio` wiring. The constants exist; nothing
  calls them yet (the media_player phase owns barge-in).
- A 5-second file read on `/api/media/status`, presence/keepalive handling —
  nothing beyond DESIGN's defaults.

## Local test environment (nothing outside this working directory was touched)

- `requirements_test.txt` lists bare `aiortc`, which resolves to a version
  pinning `av<15`; that `av` has no wheel for this Python and pip falls back to
  building it from source (needs FFmpeg headers — fails here). The venv was
  built with the repo's own pinned wheel
  (`wheels/aiortc-1.14.0+av17-py3-none-any.whl`) plus a binary `av` first.
- pip's resolver downgraded `pyopenssl` to 24.3.0, below the aiortc wheel's
  declared `>=25`. `aiortc`/`OpenSSL` imports still work and the pre-existing
  camera/stream tests behave exactly as on `main`.
- Running `tests/test_play_audio.py` **alone** leaves the HA test harness
  reporting one teardown error: a lingering `pycares` DNS helper thread
  (`_ChannelShutdownManager`) started by the first HTTP request in the session.
  The identical artifact appears on a clean `main` checkout for
  `tests/test_camera.py` alone (and 5 such errors across
  `test_camera.py` + `test_stream.py`), so it is a property of this
  harness/dependency set, not of these tests. In a full-suite run the thread
  exists before this file's first test and no error is reported.

## Not verified (needs the real robot — DESIGN §7, step 1)

- Audible playback: the daemon's GStreamer upload validation and `playbin` on
  real hardware.
- Whether a production HA's TTS media source returns an absolute or a relative
  URL (`internal_url` set). Both branches are implemented and unit-tested, but
  only a live instance exercises the resolver end to end.
