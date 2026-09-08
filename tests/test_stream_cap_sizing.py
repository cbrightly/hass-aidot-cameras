"""The integration must size the library's concurrent-serve cap to its fleet.

The library defaults the cap to 3 as host protection, and a camera holds its slot
for the life of its serve. So on an account with more cameras than the cap, the
extras never stream - not "slowly", never - and nothing surfaces an error. Seen
live on a 4-camera fleet: the fourth logged "waiting for a stream slot (cap
reached)" on every attempt and was exactly the camera that would not play.
"""
from unittest.mock import MagicMock, patch

from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator


def _manager():
    c = object.__new__(AidotDeviceManagerCoordinator)
    c.hass = MagicMock()
    c.config_entry = MagicMock()
    c.config_entry.options = {}
    c.camera_coordinators = {}
    c._sync_coordinators = MagicMock()
    return c


def test_cap_is_sized_to_the_camera_count():
    coord = _manager()
    cams = {f"dev{i}": {"id": f"dev{i}"} for i in range(4)}
    with patch("custom_components.aidot.coordinator.configure_stream_limits") as cfg:
        coord._sync_camera_coordinators(cams)
    cfg.assert_called_once_with(4)


def test_no_cameras_means_no_tuning_call():
    coord = _manager()
    with patch("custom_components.aidot.coordinator.configure_stream_limits") as cfg:
        coord._sync_camera_coordinators({})
    cfg.assert_not_called()


def test_a_failing_tuning_call_never_breaks_the_refresh():
    # It is an optimisation; a device-list refresh must not die for it.
    coord = _manager()
    cams = {"dev0": {"id": "dev0"}}
    with patch("custom_components.aidot.coordinator.configure_stream_limits",
               side_effect=RuntimeError("boom")):
        coord._sync_camera_coordinators(cams)   # must not raise
