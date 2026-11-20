"""A hidden panel that plays one event clip inside the Home Assistant app.

A notification's tap can only name a path. The clip's own link opens in Safari
on an iPhone, and a media-browser deep link does not survive the app (it
escapes the link's percent-encoding a second time). This panel lives at
``/aidot-clip/<device id>/<event uuid>`` - a path with nothing to encode - and
resolves and plays the clip through the integration's media source, as the
media browser would. It is not in the sidebar.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.core import HomeAssistant

from .const import CLIP_PANEL_URL_PATH, DOMAIN

_STATIC_URL = "/aidot_static"
_WEBCOMPONENT = "aidot-clip-panel"
_JS = "aidot-clip-panel.js"
_REGISTERED = f"{DOMAIN}_clip_panel_registered"


async def async_register_clip_panel(hass: HomeAssistant) -> None:
    """Serve the panel's script and register the panel, once per process."""
    if hass.data.get(_REGISTERED):
        return
    from homeassistant.components.http import StaticPathConfig

    directory = Path(__file__).parent / "frontend"
    # Cached for good: the script's URL carries its modification time, so an
    # update is a new URL, and a tap needs no round trip to revalidate it.
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_STATIC_URL, str(directory), cache_headers=True)]
    )
    version = (directory / _JS).stat().st_mtime_ns if (directory / _JS).exists() else 0
    async_register_built_in_panel(
        hass,
        component_name="custom",
        frontend_url_path=CLIP_PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": _WEBCOMPONENT,
                "module_url": f"{_STATIC_URL}/{_JS}?v={version}",
                "embed_iframe": False,
                "trust_external": False,
            }
        },
        require_admin=False,
    )
    hass.data[_REGISTERED] = True
