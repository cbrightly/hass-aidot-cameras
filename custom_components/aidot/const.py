"""Constants for the aidot integration."""

DOMAIN = "aidot"

# Options
CONF_SERVE_PORT_BASE = "serve_port_base"
DEFAULT_SERVE_PORT_BASE = 18600

# Connection mode: how a camera's WebRTC path is chosen.
#   - relay (default): app-parity. Keep the cloud TURN relay in the ICE config
#     (with credentials) and let ICE pick the best path - host/srflx on the LAN,
#     relay when the camera is remote / on another VLAN / behind strict NAT.
#     Works on every topology (this is exactly what the official app does:
#     iceTransportPolicy=all + credentialed TURN). Costs ~2-3s of relay
#     pre-allocation on cold start because the gather is not yet trickled.
#   - lan_direct: skip the relay pre-alloc (the old "fast connect") - connects in
#     ~1s on the LAN, but a camera NOT on the Home Assistant network cannot
#     connect at all (no relay fallback). Use only if every camera shares the HA
#     LAN and you want the faster start.
CONF_CONNECTION_MODE = "connection_mode"
CONNECTION_MODE_RELAY = "relay"
CONNECTION_MODE_LAN_DIRECT = "lan_direct"
CONNECTION_MODES = [CONNECTION_MODE_RELAY, CONNECTION_MODE_LAN_DIRECT]
DEFAULT_CONNECTION_MODE = CONNECTION_MODE_RELAY

# Legacy boolean (pre-connection_mode). Retained ONLY to migrate existing
# entries: True -> lan_direct, False -> relay. New installs use CONNECTION_MODE.
CONF_FAST_CONNECT = "fast_connect"
DEFAULT_FAST_CONNECT = True

# SDES audio: include audio (PCMA) in the camera stream. ON by default for
# app-parity (the official app plays camera audio). The library feeds the audio
# encoder a continuous silence base (anullsrc + amix) so battery-camera audio
# streams smoothly and any gaps are filled with silence. Turn off only if you
# don't want camera audio. Requires python-aidot-cameras>=0.7.34.
CONF_SDES_AUDIO = "sdes_audio"
DEFAULT_SDES_AUDIO = True

# Gain (dB) applied to the served SDES camera audio. The camera mic runs hot, so
# the default trims it; raise toward 0 (or positive) if audio is too quiet, lower
# if it clips. Passed to the library per camera (HA OS can't set env vars).
CONF_SDES_AUDIO_GAIN_DB = "sdes_audio_gain_db"
DEFAULT_SDES_AUDIO_GAIN_DB = -8

# EXPERIMENTAL (off by default): skip only the ~2s livePlayResp wait for SDES
# cameras on connect, keeping the full ICE/TURN/SCTP handshake.  May shave ~2s
# off the SDES cold start but is UNVALIDATED and could destabilise SDES sessions
# (the SCTP handshake is delicate); enable only to soak-test, and watch for the
# live view dropping to a snapshot. See python-aidot-cameras AIDOT_SDES_FAST_LIVEPLAY.
CONF_SDES_FAST_LIVEPLAY = "sdes_fast_liveplay"
DEFAULT_SDES_FAST_LIVEPLAY = True

# Adaptive fast-with-fallback for SDES: try the fast path first (skip livePlay
# waits + TURN relay pre-alloc) and fall back to the full relay path if a fast
# attempt delivers no media; a per-device cache skips the fast attempt on later
# views once it has failed.  Off by default pending real-world fast-failure-rate
# data (a fast failure costs ~40s vs ~7s saved on success).  See
# python-aidot-cameras AIDOT_SDES_ADAPTIVE.
CONF_SDES_ADAPTIVE = "sdes_adaptive"
DEFAULT_SDES_ADAPTIVE = False

# Warm-hold window (seconds) for MAINS cameras: how long the live WebRTC session
# is kept after the last viewer so a re-view is instant (app-like) instead of
# paying the full ~cold handshake.  Default 120 (= prior behaviour).  0 = never
# release (keep warm forever).  Each warm mains camera holds a concurrent-stream
# slot + keeps decrypting, so don't exceed the library's concurrent-stream cap
# (default 3).  Battery cameras ignore this (motion-prewarm preserves battery).
CONF_MAINS_IDLE_S = "mains_idle_s"
DEFAULT_MAINS_IDLE_S = 120

# Opt-in local control: route camera attribute writes (LED, motion detection,
# night vision, sensitivity, volume, PTZ tracking …) over the LAN instead of the
# cloud, for eligible mains-powered cameras. Local-first with automatic cloud
# fallback; video is unaffected. Opt-in (off by default). Battery cameras
# are excluded automatically (they don't answer unicast discovery).
CONF_ENABLE_LOCAL_CONTROL = "enable_local_control"
DEFAULT_ENABLE_LOCAL_CONTROL = False


def resolve_connection_mode(options: dict) -> str:
    """Resolve the effective connection mode from a config entry's options.

    Precedence: an explicit CONF_CONNECTION_MODE wins; otherwise a legacy
    CONF_FAST_CONNECT boolean is migrated (True -> lan_direct, False -> relay);
    otherwise the default (relay). Lets pre-existing entries keep their
    behaviour until the user opens the options form (which writes
    CONF_CONNECTION_MODE)."""
    mode = options.get(CONF_CONNECTION_MODE)
    if mode in CONNECTION_MODES:
        return mode
    if CONF_FAST_CONNECT in options:
        return CONNECTION_MODE_LAN_DIRECT if options[CONF_FAST_CONNECT] else CONNECTION_MODE_RELAY
    return DEFAULT_CONNECTION_MODE
