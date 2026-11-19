"""Firmware update entity for Aidot cameras: reports, never installs.

The AiDot app asks the cloud which firmware a camera runs and which is on
offer when its live page opens. This entity asks the same question a couple
of times a day and shows the answer. It has no install feature on purpose:
firmware is installed from the AiDot app, where the camera's own progress and
failure handling live.

An unanswered question is not an error. The entity then shows no update, and
asks again later. An answer that names no newer version is an answer: the
camera is up to date.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import (
    AidotEntity,
    AidotEntityDescription,
    async_add_camera_entities,
    described_entities,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

#: How often a camera's firmware is checked once a check has been answered.
#: Firmware is published rarely, and each check is one request to the cloud.
FIRMWARE_CHECK_INTERVAL = timedelta(hours=12)

#: How soon to ask again after a check that got no answer.
FIRMWARE_RETRY_INTERVAL = timedelta(hours=1)

#: Shown with an available update. There is no install button here.
INSTALL_HINT = "Install this update from the AiDot app."

#: Home Assistant cuts a release summary off at this length.
_SUMMARY_MAX = 255


def _now() -> float:
    """A clock that does not jump; its own function so tests can move it."""
    return time.monotonic()


@dataclass(frozen=True, kw_only=True)
class AidotUpdateDescription(UpdateEntityDescription, AidotEntityDescription):
    """Describes an Aidot camera update entity."""


#: Not gated on a profile property: the app asks for the version list of every
#: camera, whatever its model declares.
FIRMWARE_UPDATE = AidotUpdateDescription(
    key="firmware",
    translation_key="firmware",
    device_class=UpdateDeviceClass.FIRMWARE,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one firmware update entity per camera."""

    def _collect(cameras, registered) -> list[UpdateEntity]:
        return described_entities(
            cameras,
            registered,
            (FIRMWARE_UPDATE,),
            AidotFirmwareUpdate,
            hass=hass,
            platform="update",
        )

    async_add_camera_entities(entry, async_add_entities, _collect)


class AidotFirmwareUpdate(AidotEntity, UpdateEntity):
    """The firmware a camera runs and the firmware on offer for it.

    Read-only: no install feature is declared, so Home Assistant shows the
    versions and no install button. The check rides the camera's regular
    refresh: each refresh looks at whether a check is due and, if so, runs one
    in the background. The cloud answers by itself, so a sleeping battery
    camera is asked like any other and is not woken.
    """

    entity_description: AidotUpdateDescription

    def __init__(
        self,
        coordinator: AidotCameraUpdateCoordinator,
        description: AidotUpdateDescription = FIRMWARE_UPDATE,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        #: The last usable answer, or None.
        self._firmware: Any = None
        #: When the last check was started, and whether it was answered.
        self._checked_at: float | None = None
        self._answered = False
        #: The device-list firmware version the last answer was for.
        self._answer_for: str | None = None
        self._checking = False
        #: The check in flight, so removal can cancel it.
        self._check_task: asyncio.Task[None] | None = None
        self._removed = False

    @property
    def available(self) -> bool:
        """Available while the device exists.

        The answer comes from the cloud's firmware service, which gives it
        whether the camera is online, asleep or offline, and the versions it
        named stay true through all three.
        """
        return True

    # -- what Home Assistant shows -------------------------------------------

    def _reported_version(self) -> str | None:
        """The firmware version the cloud's device list reports."""
        return getattr(self.device_client.info, "firmware_version", None) or None

    @property
    def installed_version(self) -> str | None:
        """The running firmware: from the check's answer, else the device list."""
        answer = self._firmware
        if answer is not None and answer.installed_version:
            return answer.installed_version
        return self._reported_version()

    @property
    def latest_version(self) -> str | None:
        """The firmware on offer, or None when no check has been answered.

        An answer that names nothing newer says the camera is up to date:
        the latest version is then the one it runs.
        """
        answer = self._firmware
        if answer is None:
            return None
        return answer.latest_version or self.installed_version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Whether ``latest_version`` is an update, as the app decides it.

        The app offers an update when the two versions in one answer differ;
        it does not order them. Without the answer's own installed version the
        two strings come from different places, so they are ordered instead.
        """
        answer = self._firmware
        if answer is not None and answer.installed_version:
            return latest_version != installed_version
        return super().version_is_newer(latest_version, installed_version)

    @property
    def release_summary(self) -> str | None:
        """Where to install the update, plus the notes when the cloud has any."""
        answer = self._firmware
        if answer is None or not answer.latest_version:
            return None
        notes = (getattr(answer, "release_notes", None) or "").strip()
        summary = f"{INSTALL_HINT} {notes}" if notes else INSTALL_HINT
        return summary[:_SUMMARY_MAX]

    # -- when to check -------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Check once the entity is in place."""
        await super().async_added_to_hass()
        self._removed = False
        self._check_if_due()

    async def async_will_remove_from_hass(self) -> None:
        """Stop a check in flight: it has nowhere to put its answer.

        Removal covers the entity being disabled and the entry unloading.
        The flag is for a check that cannot be cancelled (it is past its
        last await, or the task handle was not ours to keep).
        """
        self._removed = True
        task, self._check_task = self._check_task, None
        if task is not None and not task.done():
            task.cancel()
        self._checking = False
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Look at whether a check is due on every camera refresh."""
        self._check_if_due()
        super()._handle_coordinator_update()

    def _stale_answer(self) -> bool:
        """Whether the kept answer is about firmware the camera no longer runs."""
        return (
            self._firmware is not None and self._answer_for != self._reported_version()
        )

    def _check_due(self) -> bool:
        """Whether a check should start now."""
        # Not gated on the camera being online: the cloud answers for an
        # offline or sleeping camera as it does for any other.
        if self._checking or self._removed:
            return False
        if self._checked_at is None:
            return True
        if self._stale_answer():
            # The camera's firmware changed since the answer: ask again.
            return True
        interval = (
            FIRMWARE_CHECK_INTERVAL if self._answered else FIRMWARE_RETRY_INTERVAL
        )
        return _now() - self._checked_at >= interval.total_seconds()

    @callback
    def _check_if_due(self) -> None:
        if not self._check_due():
            return
        if self._stale_answer():
            # An answer about the firmware the camera ran before says nothing
            # about the one it runs now.
            self._firmware = None
        self._checking = True
        self._checked_at = _now()
        self._answered = False
        dev_id = self.device_client.info.dev_id
        task = self.coordinator.spawn_entry_task(
            self._async_check(), f"aidot_firmware_check_{dev_id}"
        )
        self._check_task = task if isinstance(task, asyncio.Task) else None

    async def _async_check(self) -> None:
        """Ask once; keep the answer, or keep what was known."""
        reported = self._reported_version()
        # A failed check is not an error: nothing is shown, and it is retried.
        try:
            answer = await self.device_client.async_get_firmware_info()
        except Exception as exc:
            _LOGGER.debug(
                "%s: firmware check failed: %r", self.device_client.info.dev_id, exc
            )
            answer = None
        finally:
            self._checking = False
            self._check_task = None
        if self._removed or answer is None:
            return
        if not getattr(answer, "latest_version", None):
            # The cloud answered and named nothing newer. That is "up to
            # date", not silence: show the running version as the latest and
            # ask again at the regular interval. With no version at all there
            # is nothing to show, and it is asked again sooner.
            if not (getattr(answer, "installed_version", None) or reported):
                return
        self._firmware = answer
        self._answered = True
        self._answer_for = reported
        if self.hass is not None:
            self.async_write_ha_state()
