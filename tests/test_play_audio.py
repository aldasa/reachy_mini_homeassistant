"""``reachy_mini.play_audio``: REST upload path, gating and transcode.

Covers route A′ of DESIGN.md §5 — PyAV transcode → multipart upload →
``play_sound`` — plus the sleep gate, the 25 MiB upload cap, URL fetch
failures, the volume override, the extension allow-list and the
per-robot serialisation lock.

The robot is never contacted: ``aioclient_mock`` stands in for the
daemon and every media fixture is synthesised locally (stdlib ``wave``,
plus PyAV for the transcode assertions).
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import os
import re
import wave
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.reachy_mini import audio_transcode
from custom_components.reachy_mini.audio_transcode import (
    ensure_within_upload_cap,
    read_media,
    source_extension,
    to_wav_s16,
    tts_extension_suffix,
    upload_filename,
)
from custom_components.reachy_mini.const import (
    ATTR_MEDIA,
    ATTR_TRANSPORT,
    ATTR_VOLUME,
    CONF_UNIT_ID,
    DOMAIN,
    ENDPOINT_MEDIA_PLAY_SOUND,
    ENDPOINT_MEDIA_SOUNDS_UPLOAD,
    ENDPOINT_VOLUME_SPEAKER_SET,
    MAX_SOUND_UPLOAD_BYTES,
    SERVICE_PLAY_AUDIO,
    WAV_CHANNELS,
    WAV_SAMPLE_RATE,
)
from custom_components.reachy_mini.services import async_register_services

from .conftest import BASE_URL

# A TTS-cache-shaped URL — what the assist/sanotts pipeline hands us.
REMOTE_URL = "http://tts.lan/api/tts_proxy/reply.mp3"


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _wav_bytes(
    seconds: float = 1.0,
    rate: int = WAV_SAMPLE_RATE,
    channels: int = WAV_CHANNELS,
    freq: float = 440.0,
) -> bytes:
    """Synthesise a real PCM s16 WAV with the stdlib."""
    frames = array.array("h")
    for index in range(int(seconds * rate)):
        value = int(12000 * math.sin(2 * math.pi * freq * index / rate))
        if channels == 2:
            frames.extend((value, value))
        else:
            frames.append(value)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.tobytes())
    return buffer.getvalue()


def _write_media_file(hass, name: str, data: bytes) -> str:
    """Drop *data* in HA's media dir and return its ``/media/...`` path."""
    media_dir = hass.config.path("media")
    os.makedirs(media_dir, exist_ok=True)
    with open(os.path.join(media_dir, name), "wb") as handle:
        handle.write(data)
    return f"/media/{name}"


def _requests(aioclient_mock) -> list[tuple[str, str, Any, Any]]:
    """(METHOD, path, payload, headers) for every request the mock saw.

    The HA harness records ``(method, url, data, headers)`` where
    ``data`` is the request's ``data`` **or** ``json`` payload.
    """
    parsed: list[tuple[str, str, Any, Any]] = []
    for call in aioclient_mock.mock_calls:
        method, url = call[0], call[1]
        payload = call[2] if len(call) > 2 else None
        headers = call[3] if len(call) > 3 else None
        parsed.append(
            (
                str(method).upper(),
                getattr(url, "path", str(url)),
                payload,
                headers,
            )
        )
    return parsed


def _post_order(aioclient_mock) -> list[str]:
    """Paths of every POST, in the order they were made."""
    return [
        path
        for method, path, _, _ in _requests(aioclient_mock)
        if method == "POST"
    ]


def _post_payloads(aioclient_mock, path: str) -> list[Any]:
    return [
        payload
        for method, p, payload, _ in _requests(aioclient_mock)
        if method == "POST" and p == path
    ]


async def _uploaded_part(form) -> tuple[str, str, str, bytes]:
    """(field name, filename, content-type, payload) of a FormData part.

    aiohttp's ``FormData`` records each part as
    ``(name/filename, headers, payload)``; its ``MultipartWriter`` is not
    directly iterable in this context, so the recorded part is inspected
    as-is. The robot's upload route reads the ``file`` part by name and
    saves it under ``filename`` — both are asserted here.
    """
    name_info, headers, payload = form._fields[0]
    return (
        name_info["name"],
        name_info.get("filename"),
        headers.get("Content-Type"),
        payload,
    )


async def _register_target(hass, coordinator, config_entry):
    """Wire the coordinator into hass.data and register a device for it."""
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, config_entry.data[CONF_UNIT_ID])},
        name="Reachy Mini",
    )
    async_register_services(hass)
    return device


async def _awake_target(hass, coordinator, config_entry) -> str:
    """A coordinator reporting a running backend + its registered device."""
    coordinator.async_set_updated_data({"daemon_state": "running"})
    device = await _register_target(hass, coordinator, config_entry)
    return device.id


async def _call_play_audio(hass, device_id: str, media: str | None, **extra) -> None:
    data: dict[str, Any] = {ATTR_MEDIA: media, **extra} if media is not None else dict(extra)
    if device_id is not None:
        data["device_id"] = device_id
    await hass.services.async_call(
        DOMAIN, SERVICE_PLAY_AUDIO, data, blocking=True
    )


async def _play_audio(hass, coordinator, device_id, media, **extra) -> None:
    """Call ``play_audio`` with the coordinator's post-POST refresh stubbed.

    ``Coordinator.async_post`` asks for a debounced state refresh after a
    write — existing coordinator behaviour, covered by
    tests/test_wake_sleep.py. Inside these tests it would need every
    polled endpoint mocked and would leave a pending debounce timer
    behind, so it is stubbed here and only the service's own requests are
    asserted.
    """
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await _call_play_audio(hass, device_id, media, **extra)


def _mock_upload_and_play(aioclient_mock) -> None:
    aioclient_mock.post(
        f"{BASE_URL}{ENDPOINT_MEDIA_SOUNDS_UPLOAD}",
        json={"status": "ok", "path": "/tmp/reachy_mini_sounds/ha_00000000.wav"},
    )
    aioclient_mock.post(
        f"{BASE_URL}{ENDPOINT_MEDIA_PLAY_SOUND}", json={"status": "ok"}
    )


# --------------------------------------------------------------------------
# route A′: upload + play
# --------------------------------------------------------------------------


async def test_play_audio_uploads_then_plays_a_media_file(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """Happy path: /media file → WAV upload → play_sound, in that order."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.5))
    _mock_upload_and_play(aioclient_mock)

    await _play_audio(hass, coordinator, device_id, media_path)

    assert _post_order(aioclient_mock) == [
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]

    upload = _post_payloads(aioclient_mock, ENDPOINT_MEDIA_SOUNDS_UPLOAD)[0]
    name, filename, content_type, payload = await _uploaded_part(upload)
    # The part is named "file" (the daemon's parameter name) and carries
    # an allow-listed filename — anything else is a 400 on the robot.
    assert name == "file"
    assert content_type == "audio/wav"
    assert re.fullmatch(r"ha_[0-9a-f]{8}\.wav", filename), filename
    assert payload.startswith(b"RIFF")  # the uploaded payload is a real WAV

    play = _post_payloads(aioclient_mock, ENDPOINT_MEDIA_PLAY_SOUND)[0]
    assert play == {"file": filename}


async def test_play_audio_fetches_an_http_url(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """An http(s) URL (TTS cache file) is fetched, then uploaded + played."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    aioclient_mock.get(REMOTE_URL, content=_wav_bytes(seconds=0.25))
    _mock_upload_and_play(aioclient_mock)

    await _play_audio(hass, coordinator, device_id, REMOTE_URL)

    assert _requests(aioclient_mock)[0][:2] == ("GET", "/api/tts_proxy/reply.mp3")
    assert _post_order(aioclient_mock) == [
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]


async def test_play_audio_fetches_a_relative_url_from_ha_itself(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A relative HA URL (TTS proxy) is fetched from HA's own origin.

    HA's TTS entity hands integrations a ``media-source://tts/...`` id
    that resolves to an ``/api/tts_proxy/...`` URL. When that path is not
    a readable local file, it is fetched over HA's internal URL — which
    is exactly what ``play_audio`` does for speech.
    """
    device_id = await _awake_target(hass, coordinator, config_entry)
    hass.config.internal_url = "http://homeassistant.lan:8123"
    aioclient_mock.get(
        "http://homeassistant.lan:8123/api/tts_proxy/abc123.mp3",
        content=_wav_bytes(seconds=0.25),
    )
    _mock_upload_and_play(aioclient_mock)

    await _play_audio(hass, coordinator, device_id, "/api/tts_proxy/abc123.mp3")

    assert _requests(aioclient_mock)[0][:2] == (
        "GET",
        "/api/tts_proxy/abc123.mp3",
    )
    assert _post_order(aioclient_mock) == [
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]


async def test_play_audio_resolves_a_media_source_id(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A media-source:// id is resolved through HA's media_source."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    await async_setup_component(hass, "media_source", {})
    # HA's stock layout: media dir name "local" → <config>/media.
    hass.config.media_dirs.setdefault("local", hass.config.path("media"))
    _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    _mock_upload_and_play(aioclient_mock)

    await _play_audio(
        hass, coordinator, device_id, "media-source://media_source/local/ding.wav"
    )

    assert _post_order(aioclient_mock) == [
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]


async def test_play_audio_decodes_off_the_event_loop(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """PyAV decoding is CPU-bound: it must run in the executor."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    _mock_upload_and_play(aioclient_mock)

    seen: list[str] = []
    real_executor_job = hass.async_add_executor_job

    async def _spy(func, *args):
        seen.append(getattr(func, "__name__", repr(func)))
        return await real_executor_job(func, *args)

    with patch.object(hass, "async_add_executor_job", _spy):
        await _play_audio(hass, coordinator, device_id, media_path)

    assert "to_wav_s16" in seen


# --------------------------------------------------------------------------
# gating and error surfacing
# --------------------------------------------------------------------------


async def test_play_audio_requires_an_awake_robot(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A sleeping robot 503s every media route — say so, don't try."""
    coordinator.async_set_updated_data({"daemon_state": "stopped"})
    device = await _register_target(hass, coordinator, config_entry)

    with pytest.raises(ServiceValidationError, match="wake the robot first"):
        await _call_play_audio(hass, device.id, "/media/ding.wav")

    assert not aioclient_mock.mock_calls


async def test_play_audio_maps_a_daemon_503_to_the_sleep_error(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A sleep that races the last poll still surfaces as 'wake it'."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    aioclient_mock.post(
        f"{BASE_URL}{ENDPOINT_MEDIA_SOUNDS_UPLOAD}",
        status=503,
        json={"detail": "Backend not running"},
    )

    with pytest.raises(ServiceValidationError, match="wake the robot first"):
        await _play_audio(hass, coordinator, device_id, media_path)


async def test_play_audio_surfaces_a_url_fetch_failure(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A dead TTS URL reports its status and never reaches the robot."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    aioclient_mock.get(REMOTE_URL, status=404)

    with pytest.raises(ServiceValidationError, match="HTTP 404"):
        await _play_audio(hass, coordinator, device_id, REMOTE_URL)

    assert _post_order(aioclient_mock) == []


async def test_play_audio_surfaces_a_connection_failure(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A transport-level fetch error is reported, not swallowed."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    aioclient_mock.get(REMOTE_URL, exc=TimeoutError())

    with pytest.raises(ServiceValidationError, match="could not fetch media"):
        await _play_audio(hass, coordinator, device_id, REMOTE_URL)

    assert _post_order(aioclient_mock) == []


async def test_play_audio_rejects_an_unsupported_scheme(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """file:// and friends are refused — no local-file-read primitive."""
    device_id = await _awake_target(hass, coordinator, config_entry)

    with pytest.raises(ServiceValidationError, match="unsupported media reference"):
        await _play_audio(hass, coordinator, device_id, "file:///etc/passwd")

    assert not aioclient_mock.mock_calls


async def test_play_audio_rejects_the_unimplemented_webrtc_transport(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """transport: webrtc is a later phase — fail loudly, not silently."""
    device_id = await _awake_target(hass, coordinator, config_entry)

    with pytest.raises(ServiceValidationError, match="webrtc"):
        await _play_audio(
            hass,
            coordinator,
            device_id,
            "/media/ding.wav",
            **{ATTR_TRANSPORT: "webrtc"},
        )

    assert not aioclient_mock.mock_calls


async def test_play_audio_requires_a_target(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """Without a resolvable target the service explains itself."""
    coordinator.async_set_updated_data({"daemon_state": "running"})
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    async_register_services(hass)

    with pytest.raises(ServiceValidationError, match="No Reachy Mini device"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_PLAY_AUDIO,
            {ATTR_MEDIA: "/media/ding.wav"},
            blocking=True,
        )

    assert not aioclient_mock.mock_calls


# --------------------------------------------------------------------------
# size caps
# --------------------------------------------------------------------------


async def test_play_audio_rejects_a_payload_over_the_upload_cap(
    hass, coordinator, config_entry, aioclient_mock, monkeypatch
) -> None:
    """Over the daemon's upload cap: refuse before uploading."""
    monkeypatch.setattr(audio_transcode, "MAX_SOUND_UPLOAD_BYTES", 1024)
    device_id = await _awake_target(hass, coordinator, config_entry)
    # 1 s of 16 kHz mono s16 ≈ 32 kB — well over the shrunken cap.
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=1.0))

    with pytest.raises(ServiceValidationError, match="upload cap"):
        await _play_audio(hass, coordinator, device_id, media_path)

    assert not aioclient_mock.mock_calls


def test_encoded_audio_over_25_mib_is_rejected() -> None:
    """The cap mirrors the daemon's MAX_SOUND_UPLOAD_BYTES."""
    assert MAX_SOUND_UPLOAD_BYTES == 25 * 1024 * 1024

    ensure_within_upload_cap(b"\0" * MAX_SOUND_UPLOAD_BYTES)  # boundary is fine

    with pytest.raises(ServiceValidationError, match="25 MiB upload cap"):
        ensure_within_upload_cap(b"\0" * (MAX_SOUND_UPLOAD_BYTES + 1))


async def test_play_audio_rejects_an_oversized_fetch(
    hass, coordinator, config_entry, aioclient_mock, monkeypatch
) -> None:
    """A URL body over the fetch cap is dropped mid-stream."""
    monkeypatch.setattr(audio_transcode, "MAX_MEDIA_FETCH_BYTES", 4096)
    device_id = await _awake_target(hass, coordinator, config_entry)
    aioclient_mock.get(REMOTE_URL, content=_wav_bytes(seconds=1.0))

    with pytest.raises(ServiceValidationError, match="fetch cap"):
        await _play_audio(hass, coordinator, device_id, REMOTE_URL)

    assert _post_order(aioclient_mock) == []


# --------------------------------------------------------------------------
# volume override + serialisation
# --------------------------------------------------------------------------


async def test_play_audio_volume_override_sets_volume_first(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """volume: 42 → /api/volume/set before the upload, via the slider route."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    _mock_upload_and_play(aioclient_mock)
    aioclient_mock.post(f"{BASE_URL}{ENDPOINT_VOLUME_SPEAKER_SET}", json={})

    await _play_audio(
        hass, coordinator, device_id, media_path, **{ATTR_VOLUME: 42}
    )

    assert _post_order(aioclient_mock) == [
        ENDPOINT_VOLUME_SPEAKER_SET,
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]
    assert _post_payloads(aioclient_mock, ENDPOINT_VOLUME_SPEAKER_SET)[0] == {
        "volume": 42
    }


async def test_play_audio_omits_the_volume_call_by_default(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """No volume override → the robot's current volume is left alone."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    _mock_upload_and_play(aioclient_mock)

    await _play_audio(hass, coordinator, device_id, media_path)

    assert ENDPOINT_VOLUME_SPEAKER_SET not in _post_order(aioclient_mock)


async def test_concurrent_utterances_are_serialised_per_robot(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """Two overlapping calls must not interleave upload/play on one robot.

    The daemon owns a single playbin, so playback requests have to
    arrive as complete upload→play pairs.
    """
    device_id = await _awake_target(hass, coordinator, config_entry)
    media_path = _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    _mock_upload_and_play(aioclient_mock)

    call = {
        "device_id": device_id,
        ATTR_MEDIA: media_path,
    }
    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await asyncio.gather(
            hass.services.async_call(
                DOMAIN, SERVICE_PLAY_AUDIO, dict(call), blocking=True
            ),
            hass.services.async_call(
                DOMAIN, SERVICE_PLAY_AUDIO, dict(call), blocking=True
            ),
        )

    assert _post_order(aioclient_mock) == [
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]


# --------------------------------------------------------------------------
# extension allow-list + transcode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ("http://tts.lan/api/tts_proxy/reply.mp3", ".mp3"),
        ("http://tts.lan/api/tts_proxy/reply.MP3?token=abc", ".mp3"),
        ("/media/ding.wav", ".wav"),
        ("/media/ding.flac", ".flac"),
        ("media-source://media_source/local/ding.ogg", ".ogg"),
        ("/media/ding.txt", None),
        ("/media/ding", None),
        ("", None),
    ],
)
def test_source_extension_maps_to_the_daemons_allow_list(
    media: str, expected: str | None
) -> None:
    assert source_extension(media) == expected


def test_upload_filename_is_content_addressed_and_allow_listed() -> None:
    name = upload_filename(b"some wav bytes")
    assert re.fullmatch(r"ha_[0-9a-f]{8}\.wav", name)
    # Same bytes → same name (replaying overwrites instead of piling up).
    assert upload_filename(b"some wav bytes") == name
    assert upload_filename(b"different bytes") != name


def test_upload_filename_refuses_a_disallowed_extension() -> None:
    with pytest.raises(ServiceValidationError, match="allowed upload extension"):
        upload_filename(b"x", ".exe")


def test_to_wav_s16_downmixes_and_resamples() -> None:
    """48 kHz stereo input lands as 16 kHz mono s16."""
    source = _wav_bytes(seconds=0.5, rate=48000, channels=2)

    encoded = to_wav_s16(source)

    with wave.open(io.BytesIO(encoded)) as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == WAV_SAMPLE_RATE
        expected = 0.5 * WAV_SAMPLE_RATE
        assert abs(handle.getnframes() - expected) < WAV_SAMPLE_RATE * 0.05


def test_to_wav_s16_keeps_canonical_input_intact() -> None:
    """A 16 kHz mono WAV passes through with its length preserved."""
    source = _wav_bytes(seconds=0.25)

    with wave.open(io.BytesIO(to_wav_s16(source))) as handle:
        assert handle.getnchannels() == WAV_CHANNELS
        assert handle.getframerate() == WAV_SAMPLE_RATE
        assert handle.getnframes() == int(0.25 * WAV_SAMPLE_RATE)


def test_to_wav_s16_rejects_non_audio() -> None:
    with pytest.raises(ServiceValidationError, match="could not decode"):
        to_wav_s16(b"\x00\x01\x02 definitely not audio")


async def test_read_media_rejects_a_missing_local_file(hass) -> None:
    with pytest.raises(ServiceValidationError, match="no such local media file"):
        await read_media(hass, "/media/does-not-exist.wav")


# --------------------------------------------------------------------------
# TTS media sources (issue #6): rendered in process, never fetched
# --------------------------------------------------------------------------

# HA's TTS entity hands integrations an id of this shape; the engine name
# and query parameters come straight from the local TTS config.
TTS_MEDIA_SOURCE_ID = (
    "media-source://tts/tts.cloud?message=Hello+captain&language=en-GB"
)


@pytest.mark.parametrize(
    ("extension", "expected"),
    [("mp3", ".mp3"), ("WAV", ".wav"), ("opus", ".opus"), ("txt", None), ("", None)],
)
def test_tts_extension_suffix_maps_to_the_daemons_allow_list(
    extension: str, expected: str | None
) -> None:
    """HA reports a render's extension without the dot — same allow-list."""
    assert tts_extension_suffix(extension) == expected


async def test_read_media_renders_a_tts_media_source_in_process(
    hass, caplog
) -> None:
    """A ``media-source://tts`` id goes to HA's audio API, not over HTTP.

    This is the issue #6 fix: rendering in process needs no self-signed
    certificate, and the bytes HA renders are what the service carries
    on to the transcoder.
    """
    rendered = AsyncMock(return_value=("mp3", _wav_bytes(seconds=0.25)))

    with patch(
        "homeassistant.components.tts.async_get_media_source_audio", rendered
    ):
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.reachy_mini.audio_transcode"
        ):
            data = await read_media(hass, TTS_MEDIA_SOURCE_ID)

    # The id is passed through verbatim, and the rendered bytes are the
    # result — nothing was fetched.
    assert rendered.await_args.args[1] == TTS_MEDIA_SOURCE_ID
    assert data == rendered.return_value[1]
    # The render's own extension is honoured (allow-list semantics),
    # rather than guessed from the reference.
    assert ".mp3" in caplog.text


async def test_read_media_wraps_a_tts_render_error_with_its_cause(
    hass,
) -> None:
    """A render failure keeps the original error text, verbatim."""
    from homeassistant.components.media_source.error import Unresolvable

    failing = AsyncMock(side_effect=Unresolvable("No message specified."))

    with patch(
        "homeassistant.components.tts.async_get_media_source_audio", failing
    ):
        with pytest.raises(ServiceValidationError) as err:
            await read_media(hass, "media-source://tts/tts.cloud")

    message = str(err.value)
    assert "could not render TTS 'media-source://tts/tts.cloud'" in message
    assert "No message specified." in message


async def test_read_media_still_resolves_non_tts_media_sources(hass) -> None:
    """Only the tts domain is render-direct; other ids resolve as before."""
    await async_setup_component(hass, "media_source", {})
    hass.config.media_dirs.setdefault("local", hass.config.path("media"))
    _write_media_file(hass, "ding.wav", _wav_bytes(seconds=0.25))
    must_not_render = AsyncMock(side_effect=AssertionError("tts render consulted"))

    with patch(
        "homeassistant.components.tts.async_get_media_source_audio", must_not_render
    ):
        data = await read_media(hass, "media-source://media_source/local/ding.wav")

    assert data.startswith(b"RIFF")
    assert not must_not_render.await_count


async def test_read_media_does_not_special_case_a_tts_proxy_url(
    hass, aioclient_mock, tmp_path
) -> None:
    """A ``/api/tts_proxy`` URL is a plain URL again — strict fetch, no disk.

    The disk shortcut is gone (issue #6): even a token this instance's
    TTS cache knows is fetched over the network with normal TLS
    verification, and the relative form still resolves through HA's own
    internal URL.
    """
    (tmp_path / "cached.mp3").write_bytes(b"cached-audio-bytes")
    hass.data["tts_manager"] = SimpleNamespace(
        cache_dir=str(tmp_path), token_to_filename={"tok123.mp3": "cached.mp3"}
    )
    hass.config.internal_url = "https://homeassistant.lan:8443"
    aioclient_mock.get(
        "https://homeassistant.lan:8443/api/tts_proxy/tok123.mp3",
        content=b"fetched-over-https",
    )

    assert (
        await read_media(
            hass, "https://homeassistant.lan:8443/api/tts_proxy/tok123.mp3"
        )
        == b"fetched-over-https"
    )
    assert (
        await read_media(hass, "/api/tts_proxy/tok123.mp3")
        == b"fetched-over-https"
    )


async def test_play_audio_renders_a_tts_media_source(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """End to end: render → transcode → upload → play_sound, no fetch."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    _mock_upload_and_play(aioclient_mock)
    rendered = AsyncMock(return_value=("mp3", _wav_bytes(seconds=0.25)))

    with patch(
        "homeassistant.components.tts.async_get_media_source_audio", rendered
    ):
        await _play_audio(hass, coordinator, device_id, TTS_MEDIA_SOURCE_ID)

    assert rendered.await_count == 1
    assert rendered.await_args.args[1] == TTS_MEDIA_SOURCE_ID
    # No GET: the only traffic is the robot's own upload + play_sound.
    assert [req for req in _requests(aioclient_mock) if req[0] == "GET"] == []
    assert _post_order(aioclient_mock) == [
        ENDPOINT_MEDIA_SOUNDS_UPLOAD,
        ENDPOINT_MEDIA_PLAY_SOUND,
    ]

    upload = _post_payloads(aioclient_mock, ENDPOINT_MEDIA_SOUNDS_UPLOAD)[0]
    name, filename, content_type, payload = await _uploaded_part(upload)
    assert name == "file"
    assert content_type == "audio/wav"
    assert re.fullmatch(r"ha_[0-9a-f]{8}\.wav", filename), filename
    assert payload.startswith(b"RIFF")


async def test_play_audio_surfaces_a_tts_render_failure(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A render failure names its cause and is never retried over HTTP."""
    device_id = await _awake_target(hass, coordinator, config_entry)
    failing = AsyncMock(
        side_effect=HomeAssistantError("Provider tts.cloud not found")
    )

    with patch(
        "homeassistant.components.tts.async_get_media_source_audio", failing
    ):
        with pytest.raises(ServiceValidationError) as err:
            await _play_audio(hass, coordinator, device_id, TTS_MEDIA_SOURCE_ID)

    message = str(err.value)
    assert "could not render TTS" in message
    assert TTS_MEDIA_SOURCE_ID in message
    assert "Provider tts.cloud not found" in message
    # Fail hard: no fallback fetch, no traffic at all.
    assert not aioclient_mock.mock_calls
