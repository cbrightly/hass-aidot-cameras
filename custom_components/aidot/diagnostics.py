"""Diagnostics support for Aidot."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from .coordinator import AidotConfigEntry


#: The distributions worth naming in a bug report: this integration's camera
#: library and the upstream lights library it extends.
_REPORTED_PACKAGES = ("python-aidot-cameras", "python-aidot")


def _package_versions(names: "tuple[str, ...]" = _REPORTED_PACKAGES) -> dict:
    """Resolved versions of the Python packages behind this integration.

    The upstream requirement is a RANGE, not a pin -- upstream shipped two
    incompatible shapes of the private API this attaches to and both are live
    -- so the manifest cannot answer "which upstream is this running". Only the
    installed distribution can, and two users on the same integration version
    can legitimately differ.

    This is also the answer to the fork banner: GitHub shows the library repo
    as "N commits behind" upstream forever, because it is a fork in GitHub's
    sense while not being one in any sense that matters -- no upstream file is
    edited, upstream arrives as a pip dependency. The number that actually
    describes an install is the one below.

    A missing distribution reports None rather than raising: a partially
    installed environment is exactly when someone reaches for diagnostics.
    """
    from importlib.metadata import PackageNotFoundError, version

    out = {}
    for name in names:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
        except Exception:  # pragma: no cover - diagnostics must not raise
            out[name] = None
    return out


async def _stream_health(device_client: Any) -> Any:
    """Snapshot the active stream's connection health, or None if not streaming.

    Reads the live WebRTCSession's ``get_stats()`` (nominated ICE path + inbound
    RTP loss/jitter).  Fully guarded: diagnostics must never raise, and the SDES
    path / a closed session simply yield None.
    """
    session = getattr(device_client, "_stream_session", None)
    get_stats = getattr(session, "get_stats", None)
    if get_stats is None:
        return None
    try:
        return await get_stats()
    except Exception:  # pragma: no cover - diagnostics must not raise
        return None


#: Candidate fields that are raw "host:port" strings. The types and the
#: transport beside them are the relay-vs-direct signal this block exists for,
#: and they carry no address, so they stay.
_ADDRESS_FIELDS = ("local", "remote")


def _redact_stream_health(health: Any) -> Any:
    """Strip candidate addresses from a stream-health snapshot.

    Diagnostics is the file a reporter attaches to a PUBLIC issue, and the
    nominated ICE pair is built from `f"{candidate.host}:{candidate.port}"`, so
    an unredacted dump publishes their LAN address and the WAN address of the
    relay or peer they reached.

    Copies rather than edits: the object belongs to a session that is still
    running, and diagnostics must not reach into it. Anything unexpected is
    returned untouched rather than raising - a odd shape is not worth a failed
    download.
    """
    if not isinstance(health, dict):
        return health

    def _redact_pair(pair: Any) -> Any:
        if not isinstance(pair, dict):
            return pair
        out_pair = dict(pair)
        for field in _ADDRESS_FIELDS:
            if field in out_pair:
                out_pair[field] = REDACTED
        return out_pair

    out = dict(health)
    if isinstance(out.get("nominated"), dict):
        out["nominated"] = _redact_pair(out["nominated"])
    # Every LIST of pairs too, not just the nominated one. `ice` carries the
    # full candidate-pair list with the same host:port strings, and redacting
    # only `nominated` published four LAN addresses in a real diagnostics
    # download - the exact thing this function exists to prevent. Sweeping all
    # list values keeps a future key from reopening the same hole.
    for key, value in list(out.items()):
        if isinstance(value, list):
            out[key] = [_redact_pair(item) for item in value]
    return out


def _lan_control_state(device_client: Any, *, attempted: bool) -> dict[str, Any]:
    """Whether local (LAN) control attached for this camera, and where.

    Local control otherwise has no observable surface: it attaches in a
    background task, logs one INFO line on success and DEBUG on every failure,
    and appears in no entity or attribute. That makes "is local control working
    here" unanswerable from a bug report, from CI, or from the box itself.

    `attempted` is reported separately from `attached` on purpose. Not-attached
    covers several unrelated causes - the subnet sweep never saw the camera, it
    does not advertise localCtrFlag, it is battery powered and sleeps through
    unicast discovery, or the account is a shared-home member and the device
    refuses the login. A camera the sweep considered and rejected must not look
    like one it never reached.
    """
    lan = getattr(device_client, "_lan_client", None)
    return {
        "attempted": bool(attempted),
        "attached": lan is not None,
        "ip": getattr(lan, "ip", None) if lan is not None else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    lights = []
    for dc in coordinator.device_coordinators.values():
        info = dc.device_client.info
        lights.append({
            "model_id": info.model_id,
            "hw_version": info.hw_version,
            "online": dc.data.online if dc.data else None,
        })

    cameras = []
    for dc in coordinator.camera_coordinators.values():
        cam_info = dc.camera_info
        cam_data = dc.camera_data
        cameras.append({
            "model_id": cam_info.model_id,
            "hw_version": cam_info.hw_version,
            "ptz_directions": cam_info.ptz_directions,
            "is_sdes": getattr(dc.device_client, "is_sdes_camera", None),
            "streaming": dc.device_client.stream_rtsp_url is not None,
            "online": cam_data.online if cam_data else None,
            "battery": cam_data.battery_remaining if cam_data else None,
            "sd_card_status": cam_data.sd_card_status if cam_data else None,
            "wifi_rssi": cam_data.wifi_rssi if cam_data else None,
            "motion_detection": cam_data.motion_detection if cam_data else None,
            "night_vision_mode": cam_data.night_vision_mode if cam_data else None,
            # Live connection health: the nominated ICE path (relay-vs-direct)
            # and inbound RTP loss/jitter, present only while a stream is open.
            "lan_control": _lan_control_state(
                dc.device_client,
                attempted=dc.device_client.device_id
                in getattr(coordinator, "_lan_attempted", ()),
            ),
            "stream_health": _redact_stream_health(
                await _stream_health(dc.device_client)
            ),
        })

    return {
        # First, because it is the first question every bug report needs
        # answered and the fork banner on the library repo cannot answer it.
        "versions": _package_versions(),
        "lights": lights,
        "cameras": cameras,
    }
