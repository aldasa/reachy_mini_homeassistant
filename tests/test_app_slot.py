"""App-slot derivation and the remote-session binary sensor naming.

The sensor tracks whether a remote client (the mobile/desktop app over
WebRTC) *holds the robot app slot* — deliberately named "remote session"
rather than "WebRTC active", because a future camera entity will also
use WebRTC without taking the slot.
"""

from __future__ import annotations

from custom_components.reachy_mini.binary_sensor import BINARY_SENSORS
from custom_components.reachy_mini.coordinator import _derive_app_slot


def test_remote_session_holds_slot() -> None:
    """A remote_session lock is what makes the sensor turn on."""
    slot = _derive_app_slot("remote_session", "pollen-app")
    assert slot["remote_session_active"] is True
    assert slot["active_app"] == "pollen-app"
    assert slot["active_app_transport"] == "webrtc"


def test_local_app_is_not_a_remote_session() -> None:
    """A local Python app holds the slot without any remote session."""
    slot = _derive_app_slot("local_app", "my_app")
    assert slot["remote_session_active"] is False
    assert slot["active_app_transport"] == "local"


def test_free_slot_is_not_a_remote_session() -> None:
    """Nobody holding the slot means no remote session — not unknown."""
    assert _derive_app_slot("free", None)["remote_session_active"] is False


def test_unknown_lock_state_maps_to_none() -> None:
    """An unrecognised lock state leaves the sensor unknown."""
    assert _derive_app_slot("garbage", None)["remote_session_active"] is None


def test_binary_sensor_uses_remote_session_naming() -> None:
    """Entity key and JSON key both carry the remote-session name."""
    by_key = {d.key: d for d in BINARY_SENSORS}
    assert "webrtc_active" not in by_key
    desc = by_key["remote_session"]
    assert desc.json_key == "remote_session_active"
    assert desc.translation_key == "remote_session"
