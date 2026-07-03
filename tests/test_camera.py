"""Camera entity: availability and image plumbing over the stream client."""

from __future__ import annotations

from custom_components.reachy_mini.camera import ReachyMiniCamera
from custom_components.reachy_mini.stream import StreamUnavailableError

FAKE_JPEG = b"\xff\xd8fake\xff\xd9"


class FakeStreamClient:
    """Records acquire/release; serves a canned image or error."""

    def __init__(self, image: bytes = FAKE_JPEG, error: Exception | None = None):
        self._image = image
        self._error = error
        self.acquires = 0
        self.releases = 0

    async def acquire(self) -> None:
        self.acquires += 1

    async def release(self) -> None:
        self.releases += 1

    async def async_get_image(self, timeout: float = 10.0) -> bytes:
        if self._error is not None:
            raise self._error
        return self._image

    async def async_shutdown(self) -> None:
        pass


async def test_available_only_while_daemon_runs(
    hass, coordinator, config_entry
) -> None:
    camera = ReachyMiniCamera(coordinator, config_entry, FakeStreamClient())
    coordinator.async_set_updated_data({"daemon_state": "running"})
    assert camera.available is True
    coordinator.async_set_updated_data({"daemon_state": "stopped"})
    assert camera.available is False


async def test_camera_image_acquires_streams_and_releases(
    hass, coordinator, config_entry
) -> None:
    client = FakeStreamClient()
    camera = ReachyMiniCamera(coordinator, config_entry, client)
    assert await camera.async_camera_image() == FAKE_JPEG
    assert client.acquires == 1
    assert client.releases == 1


async def test_camera_image_none_when_stream_unavailable(
    hass, coordinator, config_entry
) -> None:
    client = FakeStreamClient(error=StreamUnavailableError("asleep"))
    camera = ReachyMiniCamera(coordinator, config_entry, client)
    assert await camera.async_camera_image() is None
    assert client.releases == 1  # released even on failure


async def test_setup_entry_registers_working_unload_callback(
    hass, coordinator, config_entry
) -> None:
    """Unload must hand HA a coroutine, not an already-running Task.

    The double-wrapped variant (a lambda calling async_create_task)
    makes HA's unload processing raise TypeError and strands the
    entry in FAILED_UNLOAD.
    """
    from custom_components.reachy_mini import camera as camera_platform
    from custom_components.reachy_mini.const import DOMAIN

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    added: list = []

    def add_entities(entities, update_before_add=False):
        added.extend(entities)

    await camera_platform.async_setup_entry(hass, config_entry, add_entities)
    assert len(added) == 1
    # Raises TypeError with the buggy double-wrapping; must be clean.
    await config_entry._async_process_on_unload(hass)
