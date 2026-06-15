"""Constants for the aidot integration."""

DOMAIN = "aidot"

# Options
CONF_SERVE_PORT_BASE = "serve_port_base"
DEFAULT_SERVE_PORT_BASE = 18600

# LAN-direct fast connect: skip the ~2.5s livePlay/ICE-config waits and TURN
# relay so a camera on the local network connects in ~1s instead of ~5s.
# ON by default (cameras normally share the LAN with Home Assistant); turn off
# only for a camera reachable only via relay (off-LAN / strict NAT from the host).
CONF_FAST_CONNECT = "fast_connect"
DEFAULT_FAST_CONNECT = True

# Opt-in SDES audio: transcode PCMA->AAC so the TS stream includes audio.
# Off by default because battery/weak-signal cameras send PCMA too sparsely to
# keep the AAC encoder fed, stalling the mpegts PMT and delivering zero bytes.
# Enable only for mains-powered cameras with a strong, dense PCMA stream.
CONF_SDES_AUDIO = "sdes_audio"
DEFAULT_SDES_AUDIO = False

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
