"""Experimental: a notification tap opens the clip inside the Home Assistant app.

By default a tap opens the clip's direct link, which the iPhone app hands to
Safari (confirmed 2026-10-06). A media-browser deep link does not survive the
app either: it escapes the link's percent-encoding again, and the media browser
then cannot parse it ("Invalid media source URI"). So the option points the tap
at a small panel of this integration, /aidot-clip/<device>/<event>, whose path
needs no encoding at all, and the panel plays the clip in the app.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aidot.const import CONF_NOTIFICATIONS, DOMAIN
from custom_components.aidot.notify_dispatch import build_payload, resolve_camera_config

DEV = "eeee4444ffff5555"


def _payload(open_in_app):
    return build_payload(
        dev_id=DEV,
        camera_name="Porch",
        kind="person",
        event={
            "eventCode": "4",
            "picUrl": "https://cdn/x.jpg",
            "eventUuid": "v1:ab-12",
        },
        title_template="{camera}",
        message_template="{event}",
        camera_entity_id="camera.porch",
        open_in_app=open_in_app,
    )


def test_off_the_tap_opens_the_clip_link_as_before():
    d = _payload(False)["data"]
    assert d["url"].startswith("/api/aidot/video?") and d["clickAction"] == d["url"]


def test_on_the_tap_opens_the_clip_panel_with_a_path_that_needs_no_encoding():
    d = _payload(True)["data"]
    assert d["url"] == f"/aidot-clip/{DEV}/v1:ab-12" == d["clickAction"]
    assert "%" not in d["url"] and "?" not in d["url"]
    assert d["video"].startswith("/api/aidot/video?")  # Android still animates it
    assert d["image"] == "https://cdn/x.jpg"


@pytest.mark.parametrize(
    ("stored", "expected"), [({}, False), ({"open_in_app": True}, True)]
)
def test_the_option_is_read_from_the_notification_settings(stored, expected):
    options = {CONF_NOTIFICATIONS: {**stored, "cameras": {DEV: {"events": "all"}}}}
    assert resolve_camera_config(options, DEV).open_in_app is expected


async def test_the_option_is_offered_and_saved(hass: HomeAssistant, mock_setup_entry):
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "notifications"}
    )
    assert "open_in_app" in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"targets": [], "open_in_app": True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NOTIFICATIONS]["open_in_app"] is True


async def test_the_clip_panel_is_registered_hidden_once(hass: HomeAssistant):
    from custom_components.aidot.clip_panel import async_register_clip_panel

    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch(
        "custom_components.aidot.clip_panel.async_register_built_in_panel"
    ) as reg:
        await async_register_clip_panel(hass)
        await async_register_clip_panel(hass)  # a second entry, or a reload
    assert reg.call_count == 1
    assert hass.http.async_register_static_paths.await_count == 1
    (cfg,) = hass.http.async_register_static_paths.await_args.args[0]
    assert cfg.url_path == "/aidot_static" and cfg.path.endswith("frontend")
    kwargs = reg.call_args.kwargs
    assert kwargs["frontend_url_path"] == "aidot-clip"
    assert kwargs.get("sidebar_title") is None  # not in the sidebar
    assert kwargs["require_admin"] is False
    panel = kwargs["config"]["_panel_custom"]
    assert panel["name"] == "aidot-clip-panel"
    assert panel["module_url"].startswith("/aidot_static/aidot-clip-panel.js")


async def test_the_panel_script_is_cached_and_a_new_version_gets_a_new_url(
    hass: HomeAssistant,
):
    # Served without cache headers, the script was revalidated on every panel
    # open once the browser's heuristic freshness ran out - a round trip before
    # a tapped notification could show anything. It is cached for good instead,
    # and the URL carries the file's modification time, so an update (HACS
    # rewrites the file) is a new URL the browser has never cached.
    from pathlib import Path

    from custom_components.aidot.clip_panel import async_register_clip_panel

    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch(
        "custom_components.aidot.clip_panel.async_register_built_in_panel"
    ) as reg:
        await async_register_clip_panel(hass)
    (cfg,) = hass.http.async_register_static_paths.await_args.args[0]
    assert cfg.cache_headers is True
    js = Path(cfg.path) / "aidot-clip-panel.js"
    url = reg.call_args.kwargs["config"]["_panel_custom"]["module_url"]
    assert url == f"/aidot_static/aidot-clip-panel.js?v={js.stat().st_mtime_ns}"
