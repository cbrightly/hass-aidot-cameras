# Changelog

All notable changes to the AiDot Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); versions
match the `version` in `custom_components/aidot/manifest.json`.

## [2.6.4]

### Fixed
- **Corrected the recommended dashboard card config.** The README recommended the
  Advanced Camera Card with `live.provider: go2rtc`. For a non-Frigate camera the
  card resolves no go2rtc stream unless `live.go2rtc.url` and `live.go2rtc.stream`
  are set by hand, so tiles pinned to bare `go2rtc` start inconsistently or never
  start. The guidance now uses `provider: ha` — the native Home Assistant path
  this integration already wires to go2rtc WebRTC — and adds a static `16:9`
  `dimensions` block so tiles keep a fixed size instead of ballooning when a
  stream reconnects. Documentation only; no code change.

## [2.6.3]

### Fixed
- **Choppy camera audio under packet loss (DTLS cameras).** Raises the
  `python-aidot-cameras` floor to `>=0.9.2`, which locks the camera audio stream
  to its RTP clock and conceals lost packets with silence instead of letting the
  audio timeline compress and drift ahead of the video. No integration code
  change; the fix lives in the library and installs on restart.

## [2.6.2]

### Fixed
- **Documentation / Report-issue links in the distributed integration were
  broken.** The publish tooling's repo-slug rewrite was not idempotent: once the
  source already used the public slug, it double-appended, so the shipped
  `manifest.json` (the **Documentation** / **Report issue** buttons in Home
  Assistant) and the HACS install step pointed at a non-existent
  `…/hass-aidot-cameras-cameras` URL. The rewrite now optionally consumes an
  existing `-cameras` suffix, and the published links resolve correctly.

## [2.6.1]

Hygiene, robustness, and a security fix; no change to streaming behaviour.

### Fixed
- **User-facing repo links pointed at a stale URL.** `documentation` and
  `issue_tracker` (the **Documentation** / **Report issue** buttons in Home
  Assistant) and the HACS custom-repo install step in the README resolved to a
  repository slug that 404s; they now point at the correct public distribution
  repository.

### Security
- **Clip-playback proxy URLs are now signed.** The `/api/aidot/video` view is
  unauthenticated (the media-browser `<video>` element sends no HA auth), so it
  was gated only by the unguessable event id — replayable indefinitely. The
  media source now mints a URL signed with an HMAC over `device + event +
  expiry` (per-process secret, never persisted), verified in the view with a
  constant-time compare and a 6 h expiry.

### Changed
- **`iot_class`** corrected from `cloud_polling` to `cloud_push` (the
  integration receives MQTT signaling and cloud motion-event push), and
  **`loggers: ["aidot"]`** added so HA can surface the library logger in the UI.
- **Coordinator background tasks** (LAN-control attach, stop-streaming /
  stop-motion on device removal, per-coordinator init) moved from
  `hass.async_create_task` to `config_entry.async_create_background_task` — now
  tracked, named in diagnostics, and cancelled on entry unload.
- **Library floor raised to `>=0.9.1`** (camera-log fix + dependency floors).

### Internal
- Added unit tests for the camera entity (serve-port math, connection options,
  the `stream_source()` state machine, the stale-stream eviction watchdog, the
  status-overlay TTL) and for the signed-URL helpers.
- CI now installs the library at the shipped manifest floor.

## [2.6.0]

Baseline release prior to this changelog: go2rtc WebRTC streaming for AiDot /
Leedarson cameras (DTLS + SDES paths), L2 battery-camera support via cloud
pre-connect, two-way audio, PTZ, cloud event-clip playback, and the camera
control entities (siren, floodlight, night vision, motion sensitivity, …).
