"""Camera entity for Reachy Mini.

A thin HA-facing layer over :class:`.stream.ReachyMiniStreamClient`:
still images for thumbnails/snapshots/automations and an MJPEG live
view for dashboards. All viewers share the client's single robot
session; the entity only does acquire/release bookkeeping around it.

No `CameraEntityFeature.STREAM`: the robot's producer can't accept the
frontend's WebRTC offer (see stream.py), so the live view is HA's
MJPEG fallback — which every dashboard card and the more-info dialog
handle natively.
"""

from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.camera import Camera, async_get_still_stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAMERA_MJPEG_FPS, DAEMON_STATE_RUNNING, DOMAIN
from .coordinator import ReachyMiniCoordinator
from .entity import ReachyMiniEntity
from .stream import ReachyMiniStreamClient, StreamUnavailableError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the camera and its shared stream client."""
    coordinator: ReachyMiniCoordinator = hass.data[DOMAIN][entry.entry_id]
    client = ReachyMiniStreamClient(
        coordinator.host, session=async_get_clientsession(hass)
    )
    # The bound coroutine method, called with no args at unload time,
    # returns a coroutine that HA schedules itself. Do NOT wrap it in
    # async_create_task: HA would then hand the already-running Task to
    # asyncio.Task(...) and unload fails with TypeError (FAILED_UNLOAD).
    entry.async_on_unload(client.async_shutdown)
    async_add_entities([ReachyMiniCamera(coordinator, entry, client)])


class ReachyMiniCamera(ReachyMiniEntity, Camera):
    """Live view of the robot's head camera."""

    _attr_translation_key = "camera"

    def __init__(
        self,
        coordinator: ReachyMiniCoordinator,
        entry: ConfigEntry,
        client: ReachyMiniStreamClient,
    ) -> None:
        """Wire the entity to the shared stream client."""
        ReachyMiniEntity.__init__(self, coordinator, entry, "camera")
        Camera.__init__(self)
        self._client = client

    @property
    def available(self) -> bool:
        """Camera exists only while the daemon backend runs (awake)."""
        data = self.coordinator.data or {}
        return super().available and data.get("daemon_state") == DAEMON_STATE_RUNNING

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """One JPEG still; HA scales it if width/height were requested."""
        await self._client.acquire()
        try:
            return await self._client.async_get_image()
        except StreamUnavailableError as err:
            _LOGGER.debug("Reachy Mini camera image unavailable: %s", err)
            return None
        finally:
            await self._client.release()

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """MJPEG live view; holds one consumer ref for its lifetime."""
        await self._client.acquire()
        try:
            return await async_get_still_stream(
                request,
                self._still_for_stream,
                "image/jpeg",
                1 / CAMERA_MJPEG_FPS,
            )
        finally:
            await self._client.release()

    async def _still_for_stream(self) -> bytes | None:
        try:
            return await self._client.async_get_image()
        except StreamUnavailableError:
            return None  # ends the MJPEG stream
