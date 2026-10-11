"""Cloud devices that become neither camera nor light are visible, once."""

import logging

from custom_components.aidot import coordinator as cm

HUB = {
    "id": "hub1",
    "modelId": "LK.hub.B1",
    "type": "gateway",
    "name": "B1 Plus Hub",
    "aesKey": [None],
    "product": {"serviceModules": []},
}
PLUG = {
    "id": "plug1",
    "modelId": "LK.plug.A9",
    "type": "plug",
    "name": "Plug",
    "aesKey": ["k" * 16],
    "product": {"serviceModules": [{"identity": "control.switch"}]},
}


def test_summarise_unclaimed_keeps_what_a_report_needs_and_not_the_key():
    rows = cm._summarise_unclaimed([HUB, PLUG])
    assert rows == [
        {
            "id": "hub1",
            "model_id": "LK.hub.B1",
            "type": "gateway",
            "name": "B1 Plus Hub",
            "aes_key": "missing",
            "service_modules": [],
        },
        {
            "id": "plug1",
            "model_id": "LK.plug.A9",
            "type": "plug",
            "name": "Plug",
            "aes_key": "ok",
            "service_modules": ["control.switch"],
        },
    ]
    assert "k" * 16 not in str(rows)


def test_the_set_change_detector_reports_only_changes():
    seen = set()
    assert cm._unclaimed_changed(seen, [HUB]) is True
    assert cm._unclaimed_changed(seen, [HUB]) is False
    assert cm._unclaimed_changed(seen, [HUB, PLUG]) is True
    assert cm._unclaimed_changed(seen, []) is True
    assert cm._unclaimed_changed(seen, []) is False


def test_the_log_line_fires_when_the_set_changes_and_not_otherwise(caplog):
    caplog.set_level(logging.INFO, logger="custom_components.aidot.coordinator")
    m = object.__new__(cm.AidotDeviceManagerCoordinator)
    m._unclaimed_seen = set()
    m._note_unclaimed([HUB])
    m._note_unclaimed([HUB])
    lines = [
        r.getMessage()
        for r in caplog.records
        if "neither camera nor light" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "(listed under Diagnostics)" in lines[0]
    assert m.unclaimed[0]["id"] == "hub1"
    m._note_unclaimed([HUB, PLUG])
    assert (
        sum("neither camera nor light" in r.getMessage() for r in caplog.records) == 2
    )


def test_the_log_says_so_once_when_the_last_unclaimed_device_goes(caplog):
    """A hub that becomes claimed empties the set; that change is logged too."""
    caplog.set_level(logging.INFO, logger="custom_components.aidot.coordinator")
    m = object.__new__(cm.AidotDeviceManagerCoordinator)
    m._unclaimed_seen = set()
    m._note_unclaimed([HUB])
    m._note_unclaimed([])
    m._note_unclaimed([])
    cleared = [r for r in caplog.records if "now claimed" in r.getMessage()]
    assert len(cleared) == 1
    assert cleared[0].levelno == logging.INFO
    assert m.unclaimed == []


def test_an_empty_list_from_the_start_logs_nothing(caplog):
    caplog.set_level(logging.INFO, logger="custom_components.aidot.coordinator")
    m = object.__new__(cm.AidotDeviceManagerCoordinator)
    m._unclaimed_seen = set()
    m._note_unclaimed([])
    assert not [r for r in caplog.records if "claimed" in r.getMessage()]


async def test_the_diagnostics_download_carries_the_unclaimed_list():
    from custom_components.aidot.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    class _Coordinator:
        device_coordinators: dict = {}
        camera_coordinators: dict = {}
        unclaimed = cm._summarise_unclaimed([HUB, PLUG])

    class _Entry:
        runtime_data = _Coordinator()

    class _Hass:
        async def async_add_executor_job(self, target, *args):
            return target(*args)

    diag = await async_get_config_entry_diagnostics(_Hass(), _Entry())
    assert diag["unclaimed"] == _Coordinator.unclaimed
    assert [row["id"] for row in diag["unclaimed"]] == ["hub1", "plug1"]
    assert "k" * 16 not in str(diag)
