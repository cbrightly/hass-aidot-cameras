"""The entry's options as the library's per-client stream settings.

Each config entry builds its own ``StreamSettings`` and hands it to the account
client it creates (coordinator.py), so two entries never share these switches
and nothing is written to the process environment. The integration states
every one of them explicitly, so an ``AIDOT_*`` variable for these four does
not change what an entry runs with: the option is what the user set, as it
was when the integration wrote the variables itself.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from aidot_cameras import StreamSettings

from .const import (
    CONF_DIRECT_PUBLISH,
    CONF_HLS_AUDIO,
    CONF_HLS_DIRECT_TS,
    CONF_SDES_PIN_H264,
    DEFAULT_DIRECT_PUBLISH,
    DEFAULT_HLS_AUDIO,
    DEFAULT_HLS_DIRECT_TS,
    DEFAULT_SDES_PIN_H264,
)

_LOGGER = logging.getLogger(__name__)


def stream_settings(options: Mapping[str, Any]) -> StreamSettings:
    """The library's stream settings for one entry's options."""
    direct_publish = bool(options.get(CONF_DIRECT_PUBLISH, DEFAULT_DIRECT_PUBLISH))
    hls_audio = bool(options.get(CONF_HLS_AUDIO, DEFAULT_HLS_AUDIO))
    hls_direct_ts = bool(options.get(CONF_HLS_DIRECT_TS, DEFAULT_HLS_DIRECT_TS))
    # Pin the SDES video offer to H.264 when asked - or when in-sync audio for
    # recordings is on and can run: an SDES camera feeds that stream only with
    # its offer pinned (an H.265 answer keeps the ffmpeg serve), so without the
    # pin every SDES camera would quietly stay on go2rtc.
    in_sync = hls_direct_ts and direct_publish and hls_audio
    pin = in_sync or bool(options.get(CONF_SDES_PIN_H264, DEFAULT_SDES_PIN_H264))
    if hls_direct_ts and not (direct_publish and hls_audio):
        _LOGGER.warning(
            "In-sync audio for recordings needs 'Direct publish' and "
            "'Audio in HLS and recordings' on; it stays off until they are"
        )
    # Every field is stated, on or off: the library's defaults are on, so a
    # field left unset would mean on whatever the option says.
    return StreamSettings(
        direct_publish=direct_publish,
        publish_aac=hls_audio,
        hls_direct_ts=hls_direct_ts,
        sdes_video_pt=96 if pin else "none",
    )


def of(device_client: Any) -> StreamSettings | None:
    """The settings a device client was created with, or None (a test double,
    or a client from before the library took them)."""
    value = getattr(device_client, "stream_settings", None)
    return value if isinstance(value, StreamSettings) else None
