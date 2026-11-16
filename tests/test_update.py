"""The firmware update entity: reports versions, never installs.

The entity asks the library which firmware a camera runs and which is on
offer, a couple of times a day, and shows the answer. These tests pin what it
shows, when it asks, and that an unanswered question shows nothing rather
than an error.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from homeassistant.components.update import UpdateDeviceClass, UpdateEntityFeature
from homeassistant.const import Platform

from custom_components.aidot import PLATFORMS, update
from custom_components.aidot.update import (
    FIRMWARE_CHECK_INTERVAL,
    FIRMWARE_RETRY_INTERVAL,
    INSTALL_HINT,
    AidotFirmwareUpdate,
)


def _answer(latest="2.0.0", installed="1.0.0", notes=None):
    return SimpleNamespace(
        installed_version=installed, latest_version=latest, release_notes=notes
    )


class _Client:
    """A camera client that counts firmware checks and replays answers."""

    def __init__(self, answers=(), *, firmware="1.0.0", battery=False, live=False):
        self.info = SimpleNamespace(
            dev_id="cam-1",
            model_id="A000088.x",
            name="cam-1",
            mac="",
            hw_version="",
            firmware_version=firmware,
            declared_properties=frozenset(),
        )
        self.is_battery_camera = battery
        self.has_live_session = live
        self.answers = list(answers)
        self.checks = 0

    async def async_get_firmware_info(self):
        self.checks += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Coordinator:
    def __init__(self, client, *, online=True):
        self.device_client = client
        self.camera_info = client.info
        self.data = SimpleNamespace(online=online)
        self.last_update_success = True
        self.config_entry = None
        self.tasks: list = []
        self.listeners: list = []

    def spawn_entry_task(self, coro, name):
        self.tasks.append(coro)

    def async_add_listener(self, update_callback, context=None):
        self.listeners.append(update_callback)
        return lambda: self.listeners.remove(update_callback)

    async def run_tasks(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(update, "_now", lambda: now.value)
    return now


def _entity(answers=(), *, online=True, **client_kw):
    coordinator = _Coordinator(_Client(answers, **client_kw), online=online)
    return AidotFirmwareUpdate(coordinator), coordinator


async def _refresh(entity, coordinator) -> None:
    """What a camera refresh does to the entity, less the state write."""
    entity._check_if_due()
    await coordinator.run_tasks()


def test_update_platform_is_loaded():
    assert Platform.UPDATE in PLATFORMS


def test_there_is_no_install_feature():
    entity, _ = _entity()
    assert entity.supported_features == UpdateEntityFeature(0)
    assert entity.device_class is UpdateDeviceClass.FIRMWARE
    assert entity.unique_id == "cam-1_firmware"
    assert entity.translation_key == "firmware"


async def test_an_offered_version_shows_as_an_update(clock):
    entity, coordinator = _entity([_answer("2.0.0", "1.0.0")])
    await _refresh(entity, coordinator)
    assert entity.installed_version == "1.0.0"
    assert entity.latest_version == "2.0.0"
    assert entity.state == "on"
    assert entity.release_summary == INSTALL_HINT
    assert "AiDot app" in entity.release_summary


async def test_the_same_version_on_offer_is_no_update(clock):
    entity, coordinator = _entity([_answer("1.0.0", "1.0.0")])
    await _refresh(entity, coordinator)
    assert entity.state == "off"


async def test_versions_that_differ_are_an_update_even_if_not_ordered(clock):
    # The app compares the two versions of one answer for equality only.
    entity, coordinator = _entity([_answer("build-b", "build-a")])
    await _refresh(entity, coordinator)
    assert entity.state == "on"


async def test_before_any_answer_the_device_list_version_is_shown(clock):
    entity, _ = _entity(firmware="1.0.0")
    assert entity.installed_version == "1.0.0"
    assert entity.latest_version is None
    assert entity.state is None
    assert entity.release_summary is None


@pytest.mark.parametrize("answer", [None, RuntimeError("boom")])
async def test_no_answer_shows_no_update_and_no_error(clock, answer):
    entity, coordinator = _entity([answer])
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 1
    assert entity.latest_version is None
    assert entity.state is None
    assert entity.installed_version == "1.0.0"


@pytest.mark.parametrize("latest", [None, ""])
async def test_an_answer_with_nothing_newer_reads_up_to_date(clock, latest):
    entity, coordinator = _entity([_answer(latest=latest, installed="1.0.0")])
    await _refresh(entity, coordinator)
    assert entity.installed_version == "1.0.0"
    assert entity.latest_version == "1.0.0"
    assert entity.state == "off"
    assert entity.release_summary is None


async def test_an_answer_with_nothing_newer_is_checked_at_the_slow_interval(clock):
    entity, coordinator = _entity([_answer(latest=None), _answer("2.0.0", "1.0.0")])
    await _refresh(entity, coordinator)
    clock.value += FIRMWARE_RETRY_INTERVAL.total_seconds()
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 1
    clock.value = 1000.0 + FIRMWARE_CHECK_INTERVAL.total_seconds()
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 2
    assert entity.state == "on"


async def test_an_answer_naming_no_version_at_all_is_no_answer(clock):
    entity, coordinator = _entity(
        [_answer(latest=None, installed=None), _answer()], firmware=None
    )
    await _refresh(entity, coordinator)
    assert entity.latest_version is None
    assert entity.state is None
    clock.value += FIRMWARE_RETRY_INTERVAL.total_seconds()
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 2


async def test_an_unanswered_check_is_retried_after_the_retry_interval(clock):
    entity, coordinator = _entity([None, _answer()])
    await _refresh(entity, coordinator)
    clock.value += FIRMWARE_RETRY_INTERVAL.total_seconds() - 1
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 1
    clock.value += 1
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 2
    assert entity.latest_version == "2.0.0"


async def test_an_answered_check_is_repeated_at_the_slow_interval(clock):
    entity, coordinator = _entity([_answer(), _answer("3.0.0", "1.0.0")])
    await _refresh(entity, coordinator)
    clock.value += FIRMWARE_CHECK_INTERVAL.total_seconds() - 1
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 1
    clock.value += 1
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 2
    assert entity.latest_version == "3.0.0"


def test_the_check_is_slow():
    assert FIRMWARE_CHECK_INTERVAL.total_seconds() >= 12 * 3600
    assert FIRMWARE_RETRY_INTERVAL < FIRMWARE_CHECK_INTERVAL


async def test_a_later_unanswered_check_keeps_what_was_known(clock):
    entity, coordinator = _entity([_answer(), None])
    await _refresh(entity, coordinator)
    clock.value += FIRMWARE_CHECK_INTERVAL.total_seconds()
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 2
    assert entity.latest_version == "2.0.0"


async def test_only_one_check_runs_at_a_time(clock):
    entity, coordinator = _entity([_answer()])
    entity._check_if_due()
    entity._check_if_due()
    assert len(coordinator.tasks) == 1
    await coordinator.run_tasks()


async def test_a_sleeping_battery_camera_is_asked(clock):
    # The cloud answers by itself; the camera stays asleep.
    entity, coordinator = _entity([_answer()], battery=True, live=False)
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 1
    assert entity.latest_version == "2.0.0"


async def test_an_offline_camera_is_asked_and_stays_available(clock):
    # The cloud's firmware service answers whatever the camera is doing.
    entity, coordinator = _entity([_answer()], online=False)
    assert entity.available is True
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 1
    assert entity.latest_version == "2.0.0"
    assert entity.available is True


async def test_it_stays_available_through_a_failed_refresh(clock):
    entity, coordinator = _entity([_answer()])
    await _refresh(entity, coordinator)
    coordinator.last_update_success = False
    coordinator.data = None
    assert entity.available is True
    assert entity.latest_version == "2.0.0"


async def test_removal_cancels_a_check_in_flight(clock):
    entity, coordinator = _entity()
    asked = asyncio.Event()

    async def _hang():
        asked.set()
        await asyncio.sleep(3600)

    coordinator.device_client.async_get_firmware_info = _hang
    coordinator.spawn_entry_task = lambda coro, name: asyncio.ensure_future(coro)
    entity.hass = object()
    writes: list = []
    entity.async_write_ha_state = lambda: writes.append(1)
    entity._check_if_due()
    task = entity._check_task
    await asked.wait()

    await entity.async_will_remove_from_hass()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert writes == []
    # And nothing starts a new one on an entity that is gone.
    entity._check_if_due()
    assert entity._check_task is None


async def test_an_answer_that_lands_after_removal_is_dropped(clock):
    """A check that could not be cancelled must not write to a removed entity."""
    entity, coordinator = _entity([_answer()])
    entity.hass = object()
    writes: list = []
    entity.async_write_ha_state = lambda: writes.append(1)
    entity._check_if_due()
    await entity.async_will_remove_from_hass()
    await coordinator.run_tasks()
    assert writes == []
    assert entity.latest_version is None


async def test_a_firmware_change_drops_the_answer_and_asks_again(clock):
    entity, coordinator = _entity([_answer("2.0.0", "1.0.0"), None])
    await _refresh(entity, coordinator)
    assert entity.state == "on"
    coordinator.device_client.info.firmware_version = "2.0.0"
    await _refresh(entity, coordinator)
    assert coordinator.device_client.checks == 2
    # The old answer said nothing about the firmware now running.
    assert entity.installed_version == "2.0.0"
    assert entity.latest_version is None
    assert entity.state is None


async def test_an_answer_without_its_own_installed_version_uses_the_device_list(
    clock,
):
    entity, coordinator = _entity([_answer("1.2.0", None)], firmware="1.10.0")
    await _refresh(entity, coordinator)
    assert entity.installed_version == "1.10.0"
    assert entity.latest_version == "1.2.0"
    # Strings from two places are ordered, not merely compared.
    assert entity.state == "off"


async def test_notes_follow_the_install_hint_and_are_cut_to_fit(clock):
    entity, coordinator = _entity([_answer(notes="Fixes. " * 100)])
    await _refresh(entity, coordinator)
    summary = entity.release_summary
    assert summary.startswith(INSTALL_HINT + " Fixes.")
    assert len(summary) <= 255


async def test_setup_makes_one_entity_per_camera_the_library_can_ask(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot.const import DOMAIN

    able = _Coordinator(_Client())
    unable = SimpleNamespace(
        device_client=SimpleNamespace(info=_Client().info), config_entry=None
    )
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    listeners: list = []
    entry.runtime_data = SimpleNamespace(
        camera_coordinators={"cam-1": able, "cam-2": unable},
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    added: list = []
    await update.async_setup_entry(hass, entry, added.extend)
    assert [type(e) for e in added] == [AidotFirmwareUpdate]
    assert added[0].coordinator is able
    # A later account refresh does not make it twice.
    for cb in listeners:
        cb()
    assert len(added) == 1
