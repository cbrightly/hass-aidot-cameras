"""Constants for the aidot integration."""

from collections.abc import Mapping
from typing import Any

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
# There is deliberately NO relay-only mode here. It was built and measured
# 2026-08-24: with only the relay candidate on offer - and even with c=/m=
# moved to the relay allocation and the WAN permission pre-installed - both
# camera families still dialed our host address directly, because the
# firmware learns it from our own ICE probes and replies to wherever packets
# came from. An option that says "relay only" while sessions run direct is a
# control that lies. The library keeps the mode as an env-only experimental
# knob (AIDOT_SDES_CONNECTION_MODE=relay) with the limitation documented.
CONNECTION_MODES = [CONNECTION_MODE_RELAY, CONNECTION_MODE_LAN_DIRECT]
DEFAULT_CONNECTION_MODE = CONNECTION_MODE_RELAY

# Legacy boolean (pre-connection_mode), read only by async_migrate_entry:
# True -> lan_direct, False -> relay.
CONF_FAST_CONNECT = "fast_connect"

# The per-camera "Camera audio (in HA stream)" switch starts here on a camera
# it has never been set for. Not on the options form: it replaced the global
# "SDES camera audio" option (sdes_audio), and is written only by the options
# migration, so an entry that had that option off seeds new switches off too.
CONF_CAMERA_AUDIO_DEFAULT = "camera_audio_default"
DEFAULT_CAMERA_AUDIO = True

# Trim (dB) on the served SDES camera audio, applied after the library's level
# normalizer (1.0.0rc52 runs the same one on SDES as on the M3 Pros), so 0 is
# "the same loudness as an M3 Pro". Raise if a camera is too quiet, lower if it
# clips. Passed to the library per camera (HA OS can't set env vars). Was -8
# through 2.37.9, sized for raw A-law before the normalizer existed on this
# path; measured 2026-10-14, that floored both SDES models to digital zero.
CONF_SDES_AUDIO_GAIN_DB = "sdes_audio_gain_db"
DEFAULT_SDES_AUDIO_GAIN_DB = 0

# On by default: skip the livePlayResp wait for SDES cameras on connect and go
# straight to the video handshake (the full ICE/TURN/SCTP handshake is kept).
# This is what the official app does, and it starts SDES cameras about 4.5 s
# sooner. The PTZ camera is excluded automatically (its handshake needs the
# slower path). The option stays on the form so a camera whose live view drops
# to a snapshot can be put back on the slower path. See python-aidot-cameras
# AIDOT_SDES_FAST_LIVEPLAY.
CONF_SDES_FAST_LIVEPLAY = "sdes_fast_liveplay"
DEFAULT_SDES_FAST_LIVEPLAY = True

# Warm-hold window (seconds) for MAINS cameras: how long the live WebRTC session
# is kept after the last viewer so a re-view is instant (app-like) instead of
# paying the full ~cold handshake.  Default 300 (5 min): the common "glance,
# step away, glance again" pattern then stays instant, closing most of the gap
# to the app (which keeps a persistent session).  0 = never release (keep warm
# forever).  Each warm mains camera holds a concurrent-stream slot + keeps
# decrypting, so don't exceed the library's concurrent-stream cap (default 3) -
# only genuinely-viewed cameras hold a slot (preview/thumbnail refreshes do
# NOT warm a stream, by design, to avoid slot churn on multi-camera dashboards).
# Battery cameras ignore this: they are never pre-warmed and stream only
# while someone is watching, releasing after BATTERY_IDLE_S below.
CONF_MAINS_IDLE_S = "mains_idle_s"
# 0 = never release. Mains cameras cost nothing to keep warm, and idle-release is
# kept for BATTERY cameras, which is where dormancy actually saves something.
# Before this was tuned, idle-release could never fire at all (the check watched a
# shared port that always had a peer), so every camera streamed 24/7; making it
# work correctly is what turned a warm fleet into a fleet that was always asleep.
#
# RE-MEASURED 2026-09-07. This comment used to justify the default with "a cold
# wake on these measures 16-22s - long enough that a live view looks broken".
# That is no longer true: across 11 real cold opens on the live fleet the serve
# bound in 2.6-3.6s (median 2.9s), SDES and DTLS alike. The cold-start work is
# what closed the gap - 1.0.0rc8 alone took SDES first media from 6702ms to
# 2695ms. The number is left out rather than restated because "seconds to serve"
# is not "seconds to a picture", and nobody has measured the latter recently.
#
# Why it matters: keeping a mains camera warm is not free for every camera. One
# that stops sending media when unwatched (the A001064 PTZ does - its ICE agent
# drops the transport) cannot be held warm at all, so a 0 window leaves the
# watchdog restarting it indefinitely, and each teardown emits a burst of
# non-monotonic DTS that a strict consumer such as HA's mp4 recorder rejects.
# The library says exactly this in its own log line. Revisiting this default is
# a real option now that its stated cost is ~6x smaller than recorded.
DEFAULT_MAINS_IDLE_S = 0

# How long (s) a battery camera keeps streaming after its last viewer leaves.
# Short so a closed view stops the camera instead of draining it: with the
# library's 120 s default, a manual view kept a battery camera streaming 129 s
# (HLS dialog) to 159 s (WebRTC card) after it closed - the 120 s window plus
# Home Assistant's own 30 s stream linger and a 10 s viewer-check interval.
# Long enough that switching from a dashboard card to the more-info dialog
# finds the session still warm.
BATTERY_IDLE_S = 15.0

# The SDES video offer is always pinned to H.264 (payload type 96).
#
# The offer advertises "96 97" (H264 first). Some cameras honour that and some
# disregard it: an A001064 answered H264 nine times and H265 twice across eleven
# otherwise identical sessions, and the codec it chose also changed the
# resolution (H264 -> 1280x720, H265 -> 2560x1440, 11 of 11). A consumer that
# cannot decode a sudden 2560x1440 H265 stream shows roughly one frame per
# keyframe -- which is what Media Source Extensions playback looks like on iOS
# when the flip happens. Pinning the OFFER removes the choice; measured 4 of 4
# sessions producing h264 1280x720. It was the "sdes_pin_h264" option, on by
# default since 2.35.0 and forced on by in-sync audio whatever it said; no
# owned model encodes H.265 only, so the option no longer exists.

# How each camera's media is published, and how Home Assistant's HLS dialog and
# recordings get its audio. One choice in place of three interdependent
# switches (direct_publish, hls_audio, hls_direct_ts; see async_migrate_entry),
# so a combination where one switch silently disables another can no longer
# be saved. Each mode is its (direct publish, AAC track, in-sync TS) triple,
# which library_settings hands to the library's StreamSettings.
#
#   direct publish  every camera's media goes straight into go2rtc from the
#                   library, with no ffmpeg in the path (library
#                   AIDOT_DIRECT_PUBLISH). DTLS cameras publish only when this
#                   integration can reach go2rtc's API; with Home Assistant's
#                   bundled go2rtc they keep the serve (camera._push_mode).
#                   SDES cameras publish whatever the mode.
#   AAC track       an AAC copy of each published camera's audio next to its
#                   G.711 (library AIDOT_PUBLISH_AAC), so the HLS dialog and
#                   recordings, which drop G.711, have sound.
#   in-sync TS      the HLS dialog and recordings of standard cameras read a
#                   library-muxed MPEG-TS on one clock instead of go2rtc's RTSP,
#                   where a recording that joined a running stream had its
#                   sound 0.1-0.75 s late (library AIDOT_HLS_DIRECT_TS). Needs
#                   the other two.
#
# All three on by default since 2.35.0, validated on every camera model.
CONF_PUBLISH_MODE = "publish_mode"
#: How many of a card's newest recordings get a thumbnail per listing. The
#: camera returns nothing to a request naming more than ~20-25 recordings
#: (measured 2026-11-13), so this stays well under that; a screenful is what
#: the app asks for, and only the few most-recent recordings have a picture
#: anyway.
SD_THUMBNAIL_CAP = 15
PUBLISH_MODE_IN_SYNC = "in_sync"
PUBLISH_MODE_GO2RTC = "go2rtc"
PUBLISH_MODE_NO_HLS_AUDIO = "no_hls_audio"
PUBLISH_MODE_FFMPEG = "ffmpeg"
PUBLISH_MODE_FFMPEG_NO_HLS_AUDIO = "ffmpeg_no_hls_audio"
#: mode -> (direct publish, AAC track, in-sync TS). The form lists them in
#: this order.
PUBLISH_MODES: dict[str, tuple[bool, bool, bool]] = {
    PUBLISH_MODE_IN_SYNC: (True, True, True),
    PUBLISH_MODE_GO2RTC: (True, True, False),
    PUBLISH_MODE_NO_HLS_AUDIO: (True, False, False),
    # Off direct publish an SDES camera still publishes, so whether it carries
    # the AAC track is still a choice there.
    PUBLISH_MODE_FFMPEG: (False, True, False),
    PUBLISH_MODE_FFMPEG_NO_HLS_AUDIO: (False, False, False),
}
DEFAULT_PUBLISH_MODE = PUBLISH_MODE_IN_SYNC

# Opt-in local control: route camera attribute writes (LED, motion detection,
# night vision, sensitivity, volume, PTZ tracking ...) over the LAN instead of the
# cloud, for eligible mains-powered cameras. Local-first with automatic cloud
# fallback; video is unaffected. Opt-in (off by default). Battery cameras
# are excluded automatically (they don't answer unicast discovery).
CONF_ENABLE_LOCAL_CONTROL = "enable_local_control"
DEFAULT_ENABLE_LOCAL_CONTROL = False

# Motion push notifications. Everything lives under ONE options key so the
# dispatcher (notify_dispatch.py) can read it as a unit and the reload-on-options
# listener can ignore it: a notification edit must never reload the entry, since
# a reload tears down every camera session.
#
#   options["notifications"] = {
#       "targets":    ["mobile_app_phone"],   # notify.* service names, no prefix
#       "cooldown_s": 60,                     # 0 = no cooldown
#       "title":      "{camera}: {event_title}",
#       "message":    "{event_title} detected at {time}",
#       "cameras":    {"<dev_id>": {"events": "off"|"all"|"person",
#                                   "targets": [...]}},   # [] = use global
#   }
CONF_NOTIFICATIONS = "notifications"
CONF_NOTIFY_TARGETS = "targets"
CONF_NOTIFY_COOLDOWN_S = "cooldown_s"
CONF_NOTIFY_TITLE = "title"
CONF_NOTIFY_MESSAGE = "message"
CONF_NOTIFY_CAMERAS = "cameras"
CONF_NOTIFY_EVENTS = "events"
#: Experimental: a tap opens the clip in the app's own clip panel instead of
#: the clip's direct link (which the iPhone app hands to Safari).
CONF_NOTIFY_OPEN_IN_APP = "open_in_app"
CLIP_PANEL_URL_PATH = "aidot-clip"
NOTIFY_EVENTS_OFF = "off"
NOTIFY_EVENTS_ALL = "all"
NOTIFY_EVENTS_PERSON = "person"
NOTIFY_EVENT_CHOICES = [NOTIFY_EVENTS_OFF, NOTIFY_EVENTS_ALL, NOTIFY_EVENTS_PERSON]
DEFAULT_NOTIFY_COOLDOWN_S = 60
# Placeholders: {camera} {event} {event_title} {time} {device_id}. An unknown
# placeholder renders as its own name rather than failing the notification.
DEFAULT_NOTIFY_TITLE = "{camera}: {event_title}"
DEFAULT_NOTIFY_MESSAGE = "{event_title} detected at {time}"


def resolve_connection_mode(options: Mapping[str, Any]) -> str:
    """The effective connection mode from a config entry's options.

    A stored ``fast_connect`` was converted by async_migrate_entry."""
    mode = options.get(CONF_CONNECTION_MODE)
    return mode if mode in CONNECTION_MODES else DEFAULT_CONNECTION_MODE


# Warm every mains camera's WebRTC session at startup so the first live view is
# instant.  ON by default since 2.9.12: mains cameras cost no battery, and a
# cold first view is the complaint users actually have.  The cost is real
# though - it opens a session on every mains camera at every restart and
# reload with no viewer present, each holding a concurrency slot and keeping
# the camera decrypting - so it is in the options form to turn off.  (This
# comment said "OFF by default" for eight releases after the default flipped.)
CONF_STARTUP_PREWARM = "startup_prewarm"
DEFAULT_STARTUP_PREWARM = True

# Open a mains camera's session when it reports motion, so a live view opened
# soon after starts warm. OFF by default. What it was for - a
# notification tap - opens the cloud clip, not a live view, and a cold open on a
# mains camera is about 3 s (2.6-3.6 s, 2026-09-07). Over 27 hours on the
# reference installation (2026-11-03 to -04) nobody opened a live view by hand,
# and each mains camera streamed 10-20 minutes to nobody, holding its one
# stream slot each time. The card list it also refreshed (the piggyback after a
# pre-warm) still rides the other pre-warms - the start-up warm-up, the re-warm
# after a card playback - and the pre-connect when it is on; a session the
# warm-hold keeps open opens no new pre-warm, so the button refreshes it then.
CONF_MOTION_PREWARM = "motion_prewarm"
DEFAULT_MOTION_PREWARM = False

# Cloud recording browser.
#
# The server caps a page at 10 items whatever pageSize asks for - measured
# 2026-08-11 across seven cameras, and the vendor's own client hard-overwrites
# the field. Asking for more does not get more; it only hides the truncation.
CLOUD_PAGE_SIZE = 10
# How far back the browser offers day folders.
CLOUD_LOOKBACK_DAYS = 7
# A busy camera holds hundreds of events a day (1517 in a 30-day window on the
# reference fleet), and each page is a request. Cap the work per day folder -
# and say so in the folder title, because a silent cap reads as "this is
# everything" when it is not.
MAX_EVENTS_PER_DAY = 200

# How long an on-device listing stays good enough to serve a browse from.
# Listing costs a WebRTC session (15-21 s DTLS, 25-70 s SDES cold) and wakes the
# camera, so browsing NEVER refreshes - a piggyback refresh rides a session
# opened for some other reason once the cache is older than this.
SD_CACHE_TTL_S = 900

# How long an UNANSWERED listing stays good enough. Much shorter, because
# silence is not a reading: it is the same "we learned nothing" as having no
# session at all, and that case leaves the cache untouched so the next session
# retries. Caching silence for the full TTL made the least informative answer
# the most durable one - measured 2026-08-13, a camera holding 108 recordings
# read "the camera did not answer" for eight minutes after a restart caught it
# mid-something, and answered on the very next attempt.
#
# Deliberately not zero. A listing on a camera that never answers costs two AVIO
# requests and up to 23 s of timeouts, and pre-warms (the start-up warm-up, the
# re-warm after a card playback, the motion pre-connect when it is on) open
# sessions that each ask for a stale-only listing, so "always stale" would
# re-interrogate a genuinely mute camera on every open - exactly what
# only_if_stale exists to prevent.
SD_UNANSWERED_TTL_S = 120

# The window an on-device listing asks for, matching CLOUD_LOOKBACK_DAYS so the
# two halves of a camera's folder cover the same stretch of time.
SD_LOOKBACK_DAYS = 7

# How long the refresh button waits for the session it asked for before giving
# up.  Opening a session schedules the handshake and returns immediately - the
# session itself lands 15-21 s later on DTLS and 25-70 s later on a cold SDES
# camera - so a listing taken the instant the open call returns is taken with no
# session at all.  Anything shorter than the slowest cold open is the same as
# not waiting for the battery cameras, which are never warm to begin with.
SD_SESSION_WAIT_S = 75.0

# How often that wait re-asks.  Asking with no session costs nothing: the
# library answers "no session" before it sends anything, so this is a poll and
# not a request per second.
SD_SESSION_POLL_S = 1.0

# --- Playing a recording off the card ----------------------------------------
#
# The models a recording may be played from. Each has played end to end
# (2026-10-27): the A001064 and the A001513 over SDES, the A000088 over DTLS. A
# model nobody has played stays off - its card listing is still shown - until
# one has.
SD_PLAYBACK_MODELS = ("A001064", "A001513", "A000088")

# A battery camera below this charge is not woken to play a recording: a play
# holds it awake for the whole clip (the A001513's are 15 s plus 5 s of
# pre-roll). Unknown charge is allowed - most of the fleet reports none.
SD_PLAYBACK_BATTERY_FLOOR_PCT = 20

# The longest a play may hold a camera, whatever the clip says. The camera's
# reply gives the clip's length (60 s on an A001064, 15 s on an A001513); this
# is the backstop for a reply that never comes or a clip end that is missed.
SD_PLAYBACK_CAP_S = 120.0

# Captured beyond the clip length the camera reports: its pre-roll (1-5 s
# measured) plus slack for a camera that delivers a little slower than real
# time.
SD_PLAYBACK_MARGIN_S = 8.0

# A capture ends this long after the recording stops growing, once anything
# has arrived: the clip ended, or the camera stopped, and waiting longer only
# keeps it awake.
SD_PLAYBACK_IDLE_S = 5.0

# Short of the clip's reported length, a stall is waited out this long
# instead: over the cloud relay an A000088 play stopped arriving twice at the
# same point, ~19 s into a 60 s clip (2026-10-28), and the next play of the
# same clip ran through. Ending on the short idle cut those plays in three.
SD_PLAYBACK_STALL_S = 20.0

# How long a played recording stays in the clip cache. Longer than a cloud
# clip's day: replaying a card recording costs a session and, on a battery
# camera, a wake, where a cloud clip costs one HTTP fetch.
SD_CLIP_MAX_AGE_S = 7 * 24 * 3600
