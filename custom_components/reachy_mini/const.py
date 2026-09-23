"""Constants for the Reachy Mini integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "reachy_mini"

# Reachy Mini daemon defaults.
DEFAULT_PORT = 8000

# REST endpoints the coordinator fans out to on every poll. Each one
# fails independently — the entities backed by a failing endpoint go
# unavailable, others keep working.
ENDPOINT_STATUS = "/api/daemon/status"
ENDPOINT_APP_LOCK = "/api/daemon/robot-app-lock-status"
ENDPOINT_DOA = "/api/state/doa"
ENDPOINT_VOLUME_SPEAKER = "/api/volume/current"
ENDPOINT_VOLUME_MIC = "/api/volume/microphone/current"

# POST endpoints used by the controllable `number` entities.
# Body: {"volume": <int 0-100>}.
# Note: ENDPOINT_VOLUME_SPEAKER_SET plays a short confirmation sound
# on the robot every time it's invoked — that's existing SDK
# behaviour, not something the integration controls.
ENDPOINT_VOLUME_SPEAKER_SET = "/api/volume/set"
ENDPOINT_VOLUME_MIC_SET = "/api/volume/microphone/set"

# Other writable endpoints used by select / button entities.
ENDPOINT_MOTOR_SET_MODE = "/api/motors/set_mode/{mode}"  # path-templated
ENDPOINT_MOVE_WAKE_UP = "/api/move/play/wake_up"

# Daemon lifecycle endpoints — the canonical wake/sleep on the
# Wireless unit, where "asleep" means the backend is fully stopped
# and every /api/motors/* and /api/move/* route returns 503. Mirrors
# the official dashboard (dashboard/static/js/daemon.js). The start
# path also enables motor torque before the wake move, which the bare
# ENDPOINT_MOVE_WAKE_UP does not.
ENDPOINT_DAEMON_START_WAKE = "/api/daemon/start?wake_up=true"
ENDPOINT_DAEMON_STOP_SLEEP = "/api/daemon/stop?goto_sleep=true"

# /api/daemon/status "state" value while the backend is up. Anything
# else (stopped, not_initialized, starting, stopping, error) means the
# backend-gated endpoints are unusable.
DAEMON_STATE_RUNNING = "running"
ENDPOINT_APP_STOP_CURRENT = "/api/apps/stop-current-app"
ENDPOINT_APP_RESTART_CURRENT = "/api/apps/restart-current-app"
ENDPOINT_VOLUME_TEST_SOUND = "/api/volume/test-sound"
ENDPOINT_DAEMON_RESTART = "/api/daemon/restart"

# Motor mode values — must match the SDK's MotorControlMode enum
# `.value` strings. Kept here as a tuple so the select dropdown
# options stay in sync with the SDK without depending on its types.
MOTOR_MODES: tuple[str, ...] = ("enabled", "disabled", "gravity_compensation")

# Polling cadence. Matches HA's REST default and is plenty fast for
# "is the robot awake / which app is running / who's speaking".
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)
DEFAULT_TIMEOUT = 5.0

# TXT record keys exposed by the SDK's _reachy-mini._tcp.local. service.
TXT_UNIT_ID = "unit_id"
TXT_MODEL = "model"
TXT_MANUFACTURER = "manufacturer"
TXT_VERSION = "version"
TXT_ROBOT_NAME = "robot_name"

# Variant model strings — exactly mirror the values the SDK advertises
# in its mDNS TXT `model` record (see reachy_mini/utils/discovery.py).
MODEL_WIRELESS = "Reachy Mini Wireless"
MODEL_LITE = "Reachy Mini Lite"
MODEL_DEFAULT = "Reachy Mini"

# Config entry data keys.
CONF_UNIT_ID = "unit_id"
CONF_MODEL = "model"

# Recorded-move datasets the daemon preloads at startup. Mirrors
# DEFAULT_DATASETS in reachy_mini/motion/recorded_move.py on the SDK
# side. Add new entries here when the SDK ships a third library.
EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"
DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"
RECORDED_MOVE_DATASETS: tuple[str, ...] = (EMOTIONS_DATASET, DANCES_DATASET)

# Move catalog + playback endpoints. The dataset segment can contain
# '/' (HF repo paths like "pollen-robotics/...") — both endpoints use
# FastAPI :path matching on the daemon side.
ENDPOINT_MOVE_LIST = "/api/move/recorded-move-datasets/list/{dataset}"
ENDPOINT_MOVE_PLAY = "/api/move/play/recorded-move-dataset/{dataset}/{move}"

# Service actions surfaced under Developer Tools → Services.
SERVICE_PLAY_RECORDED_MOVE = "play_recorded_move"
SERVICE_PLAY_AUDIO = "play_audio"

# --- Audio playback (play_audio, route A′) ----------------------------
# The daemon's remote-sound routes (SDK:
# reachy_mini/daemon/app/routers/media.py, prefix /media under /api).
# upload + play_sound are the two play_audio uses; stop_sound and
# clear_incoming_audio are declared here for the stop/barge-in paths on
# later phases, media_status for the media_player attributes.
ENDPOINT_MEDIA_SOUNDS_UPLOAD = "/api/media/sounds/upload"
ENDPOINT_MEDIA_PLAY_SOUND = "/api/media/play_sound"
ENDPOINT_MEDIA_STOP_SOUND = "/api/media/stop_sound"
ENDPOINT_MEDIA_CLEAR_INCOMING_AUDIO = "/api/media/clear_incoming_audio"
ENDPOINT_MEDIA_STATUS = "/api/media/status"

# The daemon rejects uploads whose extension is outside its allow-list
# (media.py ALLOWED_SOUND_EXTENSIONS). Mirrored so our own uploads are
# named legally and a source can be classified before any request.
ALLOWED_SOUND_EXTENSIONS: tuple[str, ...] = (
    ".wav",
    ".mp3",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".m4a",
    ".aac",
)

# Daemon-side upload cap (media.py MAX_SOUND_UPLOAD_BYTES) applied
# before its GStreamer discoverer probe. Enforced on our side too, so a
# doomed payload never leaves the HA host.
MAX_SOUND_UPLOAD_BYTES = 25 * 1024 * 1024

# Independent cap on what we pull from a URL / media source. Compression
# makes source size a poor predictor of decoded size, so the fetch is
# bounded separately from the upload.
MAX_MEDIA_FETCH_BYTES = 64 * 1024 * 1024

# Upload format: PCM s16 WAV, 16 kHz mono. ~32 kB/s, so even 13 minutes
# of TTS stays inside the 25 MiB cap, and every GStreamer build decodes
# it. The daemon resamples to the device; route A would want 48 kHz
# stereo instead (see DESIGN.md §5.4(c)).
WAV_SAMPLE_RATE = 16000
WAV_CHANNELS = 1

# Timeouts. URL fetches can be a TTS round-trip, uploads a few MB.
MEDIA_FETCH_TIMEOUT = 60.0
MEDIA_UPLOAD_TIMEOUT = 30.0

# `play_audio` field names.
ATTR_MEDIA = "media"
ATTR_TRANSPORT = "transport"
ATTR_VOLUME = "volume"
ATTR_KEEPALIVE = "keepalive"
ATTR_WAIT = "wait"

# Transport selector values (DESIGN.md §5.1 / §5.3).
TRANSPORT_AUTO = "auto"
TRANSPORT_REST = "rest"
TRANSPORT_WEBRTC = "webrtc"
TRANSPORTS: tuple[str, ...] = (TRANSPORT_AUTO, TRANSPORT_REST, TRANSPORT_WEBRTC)

# Error text for the sleep gate — shared so tests and docs agree.
ERR_ROBOT_ASLEEP = "wake the robot first"

# --- Camera / WebRTC stream -------------------------------------------
# The daemon's webrtcsink runs a gst-webrtc-signalling server on this
# port; the camera+audio producer is advertised with this meta name
# (see reachy_mini/media/media_server.py on the SDK side).
SIGNALLING_PORT = 8443
PRODUCER_NAME = "reachymini"

# Tear the robot session down this many seconds after the last HA
# consumer (still image or MJPEG stream) goes away.
CAMERA_IDLE_TIMEOUT = 10.0

# After the robot side fails or ends the session, don't reconnect for
# this long — keeps auto-refreshing dashboards from hammering the robot.
CAMERA_RETRY_COOLDOWN = 5.0

# Live-view frame pacing. 10 fps is sized for a Raspberry Pi 5 host
# software-decoding 720p30 H264 and JPEG-encoding on demand.
CAMERA_MJPEG_FPS = 10

# --- Microphone stream (audio-only WebRTC tap) ------------------------
# The robot's single gst-webrtcsink producer carries camera *and* mic
# (one `meta.name`), so the mic listener talks to the same signalling
# endpoint as the camera — but with its own session and lifetime.

# Downstream PCM format on the gateway side (WAKEWORD-PLAN.md §1): the
# detector wants a fixed 16 kHz mono stream regardless of what Opus
# negotiated robot-side.
MIC_SAMPLE_RATE = WAV_SAMPLE_RATE  # 16000
MIC_CHANNELS = WAV_CHANNELS  # 1
MIC_SAMPLE_WIDTH = 2  # s16

# Opus packet duration the daemon's webrtcsink uses (20 ms). Only used
# to compute expected-frame counts and to spot capture gaps.
MIC_FRAME_MS = 20

# Reconnect backoff. Unlike the camera's retry cooldown (which exists to
# stop dashboards hammering a broken robot) the listener is *always on*,
# so it retries forever: fast at first, capped at 30 s, jittered so a
# robot that reboots does not synchronise every HA restart into one
# thundering reconnect.
MIC_RECONNECT_INITIAL = 1.0
MIC_RECONNECT_MAX = 30.0
MIC_RECONNECT_FACTOR = 2.0
MIC_RECONNECT_JITTER = 0.3

# How often the daemon-state gate is re-read while the session is up, and
# how long the supervisor sleeps between checks while it is closed.
MIC_GATE_POLL_INTERVAL = 1.0

# Two consecutive audio frames further apart than this many frame
# durations are recorded as a capture gap (jitter allowance).
MIC_GAP_TOLERANCE = 1.5

# Direction used for the video m-line in our answer.
#
# The intent is "do not have the CM4 encode video for a mic listener"
# (WAKEWORD-PLAN.md §4 risk 2) — but ``inactive`` cannot be used to say
# it. The robot's offer is BUNDLE (``a=group:BUNDLE video0 audio1
# application2``) and ``video0`` *is* the bundle transport m-line: an
# inactive one never starts ICE, so the producer sits in
# ``have-local-offer`` until its 12 s watchdog gives up and the session
# dies with no media at all (measured on daemon 1.10.0, 2026-09-23).
# ``sendonly`` keeps that m-line — and the shared transport — active
# while declining to receive video. Measured against ``recvonly``
# (aiortc's implicit default, which makes the CM4 encode): 0 video
# frames received over 25 s vs 741, identical audio yield, control loop
# unchanged. ``MIC_VIDEO_DIRECTION_RECEIVE`` is the fallback if a future
# robot build refuses ``sendonly``.
MIC_VIDEO_DIRECTION = "sendonly"
MIC_VIDEO_DIRECTION_RECEIVE = "recvonly"

# aiortc's default DTLS cipher list is ECDSA-only; the robot's Linux
# GStreamer build has an RSA DTLS certificate (verified live: Chrome
# negotiates TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256), while the macOS
# build generates an ECDSA one — offer both suite families.
DTLS_CIPHER_LIST = (
    b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    b"ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    b"ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    b"ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES128-SHA"
)
