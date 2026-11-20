"""Fix flows for this integration's repair issues.

Only the DTLS certificate issue (pin_issue.py) has a fix: trusting the
camera's new certificate. Every other issue here is informational and is not
offered as fixable, so it never reaches this module.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from . import pin_issue


class TrustNewCertificateFlow(RepairsFlow):
    """Drop a standard camera's pin so its next connection learns the new one."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._dev_id = str(data.get("dev_id") or "")
        self._name = str(data.get("name") or self._dev_id)

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if not self._dev_id or not pin_issue.forget_pin(self.hass, self._dev_id):
                return self.async_abort(reason="camera_not_loaded")
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"name": self._name},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """The fix flow for one of this integration's fixable issues."""
    if issue_id.startswith(f"{pin_issue.ISSUE_KEY}_"):
        return TrustNewCertificateFlow(dict(data or {}))
    return ConfirmRepairFlow()
