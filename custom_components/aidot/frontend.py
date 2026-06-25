"""Auto-registration of the bundled AiDot Lovelace card(s).

The integration ships an `aidot-ptz-card` (a press-and-hold PTZ joystick) under
``www/``. Rather than make users copy it into ``config/www`` and add a Lovelace
resource by hand, we serve it from the integration and load it as a frontend
module so it "just works" after install/restart.

This is best-effort: a failure here never blocks integration setup (the rest of
the integration is unaffected if the card can't be registered).
"""

from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Bump when the card JS changes so browsers don't serve a stale cached copy.
CARD_VERSION = "1.0.0"

_CARD_FILE = "aidot-ptz-card.js"
_CARD_URL = "/aidot_frontend/aidot-ptz-card.js"
_REGISTERED_KEY = "aidot_frontend_registered"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled card and load it as a frontend module (idempotent)."""
    if hass.data.get(_REGISTERED_KEY):
        return

    www_dir = os.path.join(os.path.dirname(__file__), "www")
    card_path = os.path.join(www_dir, _CARD_FILE)
    if not os.path.isfile(card_path):
        _LOGGER.warning("AiDot card not found at %s; skipping registration", card_path)
        return

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_URL, card_path, cache_headers=False)]
        )
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("AiDot card static path not registered: %s", err)
        return

    try:
        from homeassistant.components.frontend import add_extra_js_url

        # es5=False -> loaded as an ES module; the version query busts the cache
        # whenever CARD_VERSION changes.
        add_extra_js_url(hass, f"{_CARD_URL}?v={CARD_VERSION}")
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("AiDot card module URL not added: %s", err)
        return

    hass.data[_REGISTERED_KEY] = True
    _LOGGER.debug("AiDot PTZ card registered at %s", _CARD_URL)
