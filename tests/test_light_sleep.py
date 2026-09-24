"""Light sleep (pose-only) and its wake counterpart.

WAKEWORD-PLAN.md P2a / owner decision 2026-09-23 20:49. "Light sleep" is
the SDK's own pose-only sleep: the robot's head goes to the sleep pose and
the daemon cuts torque at the end of the trajectory, but the backend, the
media server and the :8443 signalling server all keep running, so the mic
stays hot and a wake word is still audible. That is exactly what the
"Go to sleep" button must not be used for — it stops the daemon backend
and takes the media stack (and the robot's hearing) with it.

Verified live against daemon 1.10.0 (see P2-LIGHT-SLEEP.md):
``POST /api/move/play/goto_sleep`` moved the head to the sleep pose
(z -0.046 m, pitch 27 degrees), the daemon itself switched
``motor_control_mode`` to ``disabled`` at move end, and both
``/api/daemon/status`` ("running") and ``/api/media/status``
("available") stayed healthy throughout — with mic frames flowing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.reachy_mini.button import (
    ReachyMiniLightSleepButton,
    ReachyMiniLightWakeButton,
)
from custom_components.reachy_mini.const import (
    ENDPOINT_DAEMON_STOP_SLEEP,
    ENDPOINT_MOVE_GOTO_SLEEP,
)
from custom_components.reachy_mini.coordinator import _derive_sleep_state

from .conftest import BASE_URL


def _mock_poll_endpoints(
    aioclient_mock, state: str, motor_mode: str | None = None
) -> None:
    """Mock the coordinator's GET fan-out for a given daemon state."""
    backend = None if motor_mode is None else {"motor_control_mode": motor_mode}
    aioclient_mock.get(
        f"{BASE_URL}/api/daemon/status",
        json={
            "state": state,
            "version": "1.10.0",
            "hardware_id": "57e54c2866a72263",
            "robot_name": "reachy_mini",
            "backend_status": backend,
        },
    )
    aioclient_mock.get(f"{BASE_URL}/api/daemon/robot-app-lock-status", status=503)
    aioclient_mock.get(f"{BASE_URL}/api/state/doa", status=503)
    aioclient_mock.get(f"{BASE_URL}/api/volume/current", status=503)
    aioclient_mock.get(f"{BASE_URL}/api/volume/microphone/current", status=503)


def _posts(aioclient_mock) -> list[tuple[str, str, str]]:
    """(method, path, query-string) of every request the mock saw."""
    return [
        (method, url.path, url.query_string)
        for method, url, *_ in aioclient_mock.mock_calls
    ]


def test_light_sleep_endpoint_is_the_pose_move_not_daemon_stop() -> None:
    """Guard against someone repointing light sleep at the deep path."""
    assert ENDPOINT_MOVE_GOTO_SLEEP == "/api/move/play/goto_sleep"
    assert ENDPOINT_MOVE_GOTO_SLEEP != ENDPOINT_DAEMON_STOP_SLEEP
    assert "stop" not in ENDPOINT_MOVE_GOTO_SLEEP


async def test_light_sleep_is_pose_only_and_never_stops_the_daemon(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """Light sleep = enable torque + the pose move; daemon and media stay up."""
    coordinator.async_set_updated_data(
        {"daemon_state": "running", "motor_mode": "enabled"}
    )
    aioclient_mock.post(f"{BASE_URL}/api/motors/set_mode/enabled", json={})
    aioclient_mock.post(
        f"{BASE_URL}{ENDPOINT_MOVE_GOTO_SLEEP}", json={"uuid": "u-1"}
    )
    button = ReachyMiniLightSleepButton(coordinator, config_entry)

    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await button.async_press()

    assert _posts(aioclient_mock) == [
        ("POST", "/api/motors/set_mode/enabled", ""),
        ("POST", "/api/move/play/goto_sleep", ""),
    ]
    # The whole point: nothing that stops the daemon or gives up the media
    # stack (which is what kills the mic and port 8443) is reached for.
    paths = [path for _, path, _ in _posts(aioclient_mock)]
    assert "/api/daemon/stop" not in paths
    assert "/api/media/release" not in paths
    assert not any("stop_sound" in path for path in paths)


async def test_light_sleep_enables_torque_first_when_robot_is_limp(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """A limp robot (previous light sleep, crashed app) still gets the move.

    The sleep move is a position trajectory, so torque has to be on or the
    robot only hears the sound. Enable is idempotent and pins the targets
    to the measured pose first, so it cannot snap.
    """
    coordinator.async_set_updated_data(
        {"daemon_state": "running", "motor_mode": "disabled"}
    )
    aioclient_mock.post(f"{BASE_URL}/api/motors/set_mode/enabled", json={})
    aioclient_mock.post(
        f"{BASE_URL}{ENDPOINT_MOVE_GOTO_SLEEP}", json={"uuid": "u-2"}
    )
    button = ReachyMiniLightSleepButton(coordinator, config_entry)

    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await button.async_press()

    paths = [path for _, path, _ in _posts(aioclient_mock)]
    assert paths.index("/api/motors/set_mode/enabled") < paths.index(
        "/api/move/play/goto_sleep"
    )


async def test_light_sleep_errors_when_daemon_is_stopped(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """From deep sleep the move routes 503; the button explains instead."""
    coordinator.async_set_updated_data({"daemon_state": "stopped", "motor_mode": None})
    button = ReachyMiniLightSleepButton(coordinator, config_entry)

    with pytest.raises(HomeAssistantError, match="deep sleep"):
        await button.async_press()

    assert not aioclient_mock.mock_calls


async def test_light_wake_restores_motors_then_plays_wake_move(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """Waking from light sleep: torque back on, then the wake move."""
    coordinator.async_set_updated_data(
        {"daemon_state": "running", "motor_mode": "disabled"}
    )
    aioclient_mock.post(f"{BASE_URL}/api/motors/set_mode/enabled", json={})
    aioclient_mock.post(f"{BASE_URL}/api/move/play/wake_up", json={"uuid": "u-3"})
    button = ReachyMiniLightWakeButton(coordinator, config_entry)

    with patch.object(coordinator, "async_request_refresh", AsyncMock()):
        await button.async_press()

    assert _posts(aioclient_mock) == [
        ("POST", "/api/motors/set_mode/enabled", ""),
        ("POST", "/api/move/play/wake_up", ""),
    ]


async def test_light_wake_errors_when_daemon_is_stopped(
    hass, coordinator, config_entry, aioclient_mock
) -> None:
    """Nothing to resume once the backend is stopped — say so, no HTTP."""
    coordinator.async_set_updated_data({"daemon_state": "stopped", "motor_mode": None})
    button = ReachyMiniLightWakeButton(coordinator, config_entry)

    with pytest.raises(HomeAssistantError, match="Wake up"):
        await button.async_press()

    assert not aioclient_mock.mock_calls


async def test_light_buttons_are_new_entities_beside_the_deep_pair(
    hass, coordinator, config_entry
) -> None:
    """Setup registers light_sleep/light_wake without touching the old pair."""
    from custom_components.reachy_mini import button as button_platform
    from custom_components.reachy_mini.const import DOMAIN

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    added: list = []

    def add_entities(entities, update_before_add=False):
        added.extend(entities)

    await button_platform.async_setup_entry(hass, config_entry, add_entities)

    keys = {
        entity.entity_description.key
        for entity in added
        if getattr(entity, "entity_description", None) is not None
    }
    # The two new ones exist...
    assert {"light_sleep", "light_wake"} <= keys
    # ...and the existing deep-sleep pair is still registered untouched.
    assert {"wake_up", "goto_sleep"} <= keys

    by_key = {
        entity.entity_description.key: entity
        for entity in added
        if getattr(entity, "entity_description", None) is not None
    }
    assert isinstance(by_key["light_sleep"], ReachyMiniLightSleepButton)
    assert isinstance(by_key["light_wake"], ReachyMiniLightWakeButton)
    # Distinct entities, not a repointed deep pair: the deep buttons keep
    # their own identity and the four unique ids are all different.
    assert type(by_key["goto_sleep"]).__name__ == "ReachyMiniGotoSleepButton"
    assert type(by_key["wake_up"]).__name__ == "ReachyMiniWakeUpButton"
    unique_ids = {entity.unique_id for entity in added}
    assert len(unique_ids) == len(added)
    assert by_key["light_sleep"].unique_id.endswith("_light_sleep")


@pytest.mark.parametrize(
    ("motor_mode", "daemon_state", "expected"),
    [
        ("enabled", "running", "awake"),
        ("gravity_compensation", "running", "awake"),
        ("disabled", "running", "light_sleep"),
        ("anything", "stopping", "deep_sleep"),
        ("anything", "stopped", "deep_sleep"),
        ("anything", None, None),
        # Consistent with _derive_awake: a backend that is not (yet)
        # running is definitively asleep, not unknown — during boot the
        # robot cannot hear anything, so "deep_sleep" is the truth.
        (None, "starting", "deep_sleep"),
    ],
)
def test_derive_sleep_state(motor_mode, daemon_state, expected) -> None:
    """The tri-state sensor must tell light sleep (ear hot) from deep."""
    assert _derive_sleep_state(motor_mode, daemon_state) == expected
