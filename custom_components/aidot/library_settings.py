"""The entry's options as the library's per-client stream settings.

Each config entry builds its own ``StreamSettings`` and hands it to the account
client it creates (coordinator.py), so two entries never share these switches
and nothing is written to the process environment. The integration states
every one of them explicitly, so an ``AIDOT_*`` variable for these four does
not change what an entry runs with: the publish mode is what the user set, as
it was when the integration wrote the variables itself.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from aidot_cameras import StreamSettings

from .const import (
    CONF_CAMERA_AUDIO_DEFAULT,
    CONF_PUBLISH_MODE,
    DEFAULT_CAMERA_AUDIO,
    DEFAULT_PUBLISH_MODE,
    PUBLISH_MODES,
)

_LOGGER = logging.getLogger(__name__)


def publish_mode(options: Mapping[str, Any]) -> str:
    """The entry's publish mode; an unknown stored value reads as the default."""
    mode = options.get(CONF_PUBLISH_MODE, DEFAULT_PUBLISH_MODE)
    if mode not in PUBLISH_MODES:
        _LOGGER.warning("unknown publish mode %r; using %r", mode, DEFAULT_PUBLISH_MODE)
        return DEFAULT_PUBLISH_MODE
    return mode


def camera_audio_default(options: Mapping[str, Any]) -> bool:
    """Whether a camera's live stream carries its audio before its own switch
    has said: the per-camera "Camera audio" switch starts from this."""
    return bool(options.get(CONF_CAMERA_AUDIO_DEFAULT, DEFAULT_CAMERA_AUDIO))


def stream_settings(options: Mapping[str, Any]) -> StreamSettings:
    """The library's stream settings for one entry's options."""
    direct_publish, hls_audio, hls_direct_ts = PUBLISH_MODES[publish_mode(options)]
    # Every field is stated, on or off: the library's defaults are on, so a
    # field left unset would mean on whatever the option says. The SDES video
    # offer is always pinned to H.264 (see the note in const.py): in-sync
    # audio needs the pin to feed its stream, and without it a camera that
    # flips to H.265 mid-session breaks Media Source Extensions players.
    return StreamSettings(
        direct_publish=direct_publish,
        publish_aac=hls_audio,
        hls_direct_ts=hls_direct_ts,
        sdes_video_pt=96,
    )


def of(device_client: Any) -> StreamSettings | None:
    """The settings a device client was created with, or None (a test double,
    or a client from before the library took them)."""
    value = getattr(device_client, "stream_settings", None)
    return value if isinstance(value, StreamSettings) else None
