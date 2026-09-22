"""Service handlers for the Reachy Mini integration.

Two services today:

- ``reachy_mini.play_recorded_move`` — play a move from one of the
  daemon's emotion/dance libraries.
- ``reachy_mini.play_audio`` — play a file, URL or TTS clip on the
  robot's speaker through the daemon's REST surface (route A′ of
  DESIGN.md §5): decode to WAV with PyAV, upload to
  ``/api/media/sounds/upload``, then ``POST /api/media/play_sound``.

Registration is idempotent across multiple config entries, and each
service is removed only when the last entry unloads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import aiohttp
import voluptuous as vol
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .audio_transcode import (
    ensure_within_upload_cap,
    read_media,
    to_wav_s16,
    upload_filename,
)
from .const import (
    ATTR_KEEPALIVE,
    ATTR_MEDIA,
    ATTR_TRANSPORT,
    ATTR_VOLUME,
    ATTR_WAIT,
    DAEMON_STATE_RUNNING,
    DOMAIN,
    ENDPOINT_MEDIA_PLAY_SOUND,
    ENDPOINT_VOLUME_SPEAKER_SET,
    ERR_ROBOT_ASLEEP,
    SERVICE_PLAY_AUDIO,
    SERVICE_PLAY_RECORDED_MOVE,
    TRANSPORT_AUTO,
    TRANSPORT_REST,
    TRANSPORT_WEBRTC,
    TRANSPORTS,
    WAV_CHANNELS,
    WAV_SAMPLE_RATE,
)
from .coordinator import ReachyMiniCoordinator

_LOGGER = logging.getLogger(__name__)

PLAY_RECORDED_MOVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required("dataset"): cv.string,
        vol.Required("move"): cv.string,
    }
)

# DESIGN.md §5.3. Target by device or by any entity on the robot
# (entity → device → config entry). `media` is required; a media-source
# picker in the UI, but plain strings (URLs, /media paths) are accepted.
PLAY_AUDIO_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_MEDIA): cv.string,
        vol.Optional(ATTR_TRANSPORT, default=TRANSPORT_AUTO): vol.In(TRANSPORTS),
        vol.Optional(ATTR_VOLUME): vol.All(vol.Coerce(int), vol.Range(0, 100)),
        vol.Optional(ATTR_KEEPALIVE, default=True): cv.boolean,
        vol.Optional(ATTR_WAIT, default=True): cv.boolean,
    }
)

# One lock per robot. The daemon owns a single speaker and a single
# playbin (DESIGN.md §4.4: mixing playback routes is undefined), so two
# utterances must never be in flight against the same robot — not even
# when an automation targets several robots at once.
_UTTERANCE_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(coordinator: ReachyMiniCoordinator) -> asyncio.Lock:
    """Return the per-robot utterance lock, creating it on first use."""
    return _UTTERANCE_LOCKS.setdefault(coordinator.base_url, asyncio.Lock())


def _coordinators_for_devices(
    hass: HomeAssistant, device_ids: Iterable[str]
) -> list[ReachyMiniCoordinator]:
    """Map HA device IDs back to their owning Reachy Mini coordinators."""
    registry = dr.async_get(hass)
    coordinators: list[ReachyMiniCoordinator] = []
    seen: set[str] = set()
    for device_id in device_ids:
        device = registry.async_get(device_id)
        if device is None:
            continue
        for entry_id in device.config_entries:
            if entry_id in seen:
                continue
            seen.add(entry_id)
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator is not None:
                coordinators.append(coordinator)
    return coordinators


def _coordinators_for_entities(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> list[ReachyMiniCoordinator]:
    """Map entity IDs back to their owning coordinators via the registry.

    Any entity of the device will do — the caller doesn't have to know
    which platform happens to be the "audio" one.
    """
    registry = er.async_get(hass)
    device_ids: list[str] = []
    for entity_id in entity_ids:
        entity = registry.async_get(entity_id)
        if entity is not None and entity.device_id is not None:
            device_ids.append(entity.device_id)
    return _coordinators_for_devices(hass, device_ids)


def _coordinators_for_call(
    hass: HomeAssistant, call: ServiceCall
) -> list[ReachyMiniCoordinator]:
    """Resolve device and entity targets of *call* to coordinators."""
    resolved = _coordinators_for_devices(
        hass, call.data.get(ATTR_DEVICE_ID) or []
    ) + _coordinators_for_entities(hass, call.data.get(ATTR_ENTITY_ID) or [])

    unique: list[ReachyMiniCoordinator] = []
    seen: set[str] = set()
    for coordinator in resolved:
        if coordinator.base_url in seen:
            continue
        seen.add(coordinator.base_url)
        unique.append(coordinator)
    return unique


def _ensure_awake(coordinator: ReachyMiniCoordinator) -> None:
    """Fail fast when the daemon backend is not running.

    Mirrors the camera's availability check (``camera.py``): a sleeping
    robot has its backend fully stopped, so every ``/api/media/*`` route
    answers 503. Better to say so up front than to make the caller wait
    for a timeout — and it keeps the error the same whichever endpoint
    happens to be hit first.
    """
    data = coordinator.data or {}
    if data.get("daemon_state") != DAEMON_STATE_RUNNING:
        raise ServiceValidationError(ERR_ROBOT_ASLEEP)


async def _handle_play_audio(hass: HomeAssistant, call: ServiceCall) -> None:
    """Decode the requested media and play it on each targeted robot."""
    coordinators = _coordinators_for_call(hass, call)
    if not coordinators:
        raise ServiceValidationError("No Reachy Mini device targeted")

    transport = call.data.get(ATTR_TRANSPORT, TRANSPORT_AUTO)
    if transport == TRANSPORT_WEBRTC:
        # TODO(route A, DESIGN.md §5.1/§5.4): the WebRTC uplink (live PCM
        # over the camera session's audio m-line) is a later phase along
        # with the PushAudioTrack media stream. Until then, say so
        # rather than silently playing over REST.
        raise ServiceValidationError(
            "transport 'webrtc' is not implemented in this build; use 'rest' "
            "or 'auto'"
        )
    if transport == TRANSPORT_AUTO:
        # `auto` resolves to REST unless the caller supplies live PCM,
        # which only route A can carry (DESIGN.md §5.1).
        transport = TRANSPORT_REST

    for coordinator in coordinators:
        _ensure_awake(coordinator)

    media = call.data[ATTR_MEDIA]
    volume = call.data.get(ATTR_VOLUME)

    # TODO(route A, DESIGN.md §5.4): `keepalive` and `wait` are only
    # meaningful for the WebRTC uplink (session warming, end-of-utterance
    # detection). play_sound is fire-and-forget — it returns once the
    # playbin starts — so both fields are accepted for API stability and
    # ignored here. Barge-in is handled by /api/media/stop_sound.
    _LOGGER.debug(
        "play_audio: transport=%s keepalive=%s wait=%s",
        transport,
        call.data.get(ATTR_KEEPALIVE),
        call.data.get(ATTR_WAIT),
    )

    # Fetch and transcode once, then fan out to every targeted robot —
    # the CPU-bound decode must not run on the event loop (DESIGN.md §6).
    source = await read_media(hass, media)
    wav = await hass.async_add_executor_job(
        to_wav_s16, source, WAV_SAMPLE_RATE, WAV_CHANNELS
    )
    ensure_within_upload_cap(wav)
    filename = upload_filename(wav)

    for coordinator in coordinators:
        async with _lock_for(coordinator):
            try:
                if volume is not None:
                    # Same route the speaker-volume slider uses, so the
                    # robot's own clamp/round rules apply unchanged.
                    await coordinator.async_post(
                        ENDPOINT_VOLUME_SPEAKER_SET, body={"volume": volume}
                    )
                await coordinator.async_upload_sound(wav, filename=filename)
                await coordinator.async_post(
                    ENDPOINT_MEDIA_PLAY_SOUND, body={"file": filename}
                )
            except aiohttp.ClientResponseError as err:
                if err.status == 503:
                    # Raced with a sleep, or the backend stopped between
                    # the coordinator's last poll and this call.
                    raise ServiceValidationError(ERR_ROBOT_ASLEEP) from err
                raise


async def _handle_play_recorded_move(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Play a recorded move on the targeted robots."""
    device_ids = call.data.get(ATTR_DEVICE_ID) or []
    dataset = call.data["dataset"]
    move = call.data["move"]

    coordinators = _coordinators_for_devices(hass, device_ids)
    if not coordinators:
        raise ServiceValidationError("No Reachy Mini device targeted")

    for coordinator in coordinators:
        # Pre-validate against known catalogs. Custom datasets we
        # haven't enumerated bypass the check — the daemon will
        # 404 if the move doesn't exist.
        known = coordinator.move_lists.get(dataset)
        if known is not None and move not in known:
            raise ServiceValidationError(
                f"Move '{move}' not found in dataset '{dataset}'"
            )
        await coordinator.async_play_recorded_move(dataset, move)


def async_register_services(hass: HomeAssistant) -> None:
    """Register Reachy Mini services. Safe to call multiple times."""
    if not hass.services.has_service(DOMAIN, SERVICE_PLAY_RECORDED_MOVE):

        async def _handle_play(call: ServiceCall) -> None:
            await _handle_play_recorded_move(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAY_RECORDED_MOVE,
            _handle_play,
            schema=PLAY_RECORDED_MOVE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_PLAY_AUDIO):

        async def _handle_audio(call: ServiceCall) -> None:
            await _handle_play_audio(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAY_AUDIO,
            _handle_audio,
            schema=PLAY_AUDIO_SCHEMA,
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove Reachy Mini services. Called when the last entry unloads."""
    for service in (SERVICE_PLAY_RECORDED_MOVE, SERVICE_PLAY_AUDIO):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
    _UTTERANCE_LOCKS.clear()
