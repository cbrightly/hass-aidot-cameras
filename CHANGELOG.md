# Changelog

All notable changes to the AiDot Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); versions
match the `version` in `custom_components/aidot/manifest.json`.

## [2.8.1]

### Removed
- **Removed the `sdes_fast_liveplay` option from the integration settings.** It
  was on by default with no fallback and could silently drop a camera's live view
  to a snapshot on delicate SDES sessions. Runtime behavior is unchanged - the
  library still applies its own default (on, with the built-in per-model
  correctness exclusions); only the user-facing toggle is gone. Existing entries
  that set the option keep working; the stored value is simply ignored.

### Changed
- Normalized non-ASCII typography (em/en dashes, arrows, ellipses, bullets) to
  plain ASCII across comments, docstrings, display strings and docs. No
  behavioral change.

## [2.8.0]

### Fixed
- **Recording-clip transcode could corrupt a cached MP4 under concurrent
  requests.** The per-event proxy lock was evicted using `Lock.locked()`, which
  reads `False` while a waiter is still queued, so a second request created a
  fresh lock and two ffmpeg transcodes wrote the same cache file at once
  (realistic when motion prewarm and a user tap hit the same clip). Locks are now
  refcounted and removed only when the last holder/waiter leaves.
- **Token refresh wiped stored credentials.** `token_fresh_cb` replaced the whole
  config entry with the library's `login_info`, dropping the password and country
  code the flow stores as extra keys - which broke re-authentication (`KeyError`
  on the country code) and the headless re-login. The refreshed token is now
  merged into the existing entry data instead of replacing it.
- **Re-authentication didn't save the new password and could rebind the entry to
  a different account.** The reauth step now persists the freshly entered password
  and aborts if the login resolves to a different AiDot account (matching the
  reconfigure flow).
- **A failed snapshot refresh blocked retries for 5 minutes.** `_base_snapshot`
  stamped its 5-minute cache timestamp before the network fetch and never rolled
  it back on failure. The timestamp is now only advanced on a successful fetch.
- **Cleanup ran even when a platform failed to unload**, leaving live entities
  pointing at a stopped client. The client/stream teardown now runs only on a
  successful platform unload, and the media-source provider is removed too.
- **Dual-mode (RGBW + colour-temp) bulbs showed a stale colour mode** after an
  external change. `color_mode` is now re-derived on every status update.
- **Orphaned per-device coordinators kept polling** after a device left the
  account; they are now shut down on removal.
- **Cache eviction could delete a clip a concurrent request was about to serve**
  (TOCTOU); eviction now skips the just-finalized file.
- Motion-event entities now report availability tied to their camera instead of
  always appearing available.
- Hardened the recording proxy: dropped plaintext `http` from the ffmpeg protocol
  whitelist and serialised the hardware-encoder disable flag under its lock.
- Declared `ffmpeg` in the manifest `after_dependencies`, removed invalid keys
  from `hacs.json`, and unquoted placeholders in two exception strings so hassfest
  and HACS validation pass.

### Changed
- **Entity icons moved out of Python into `icons.json`** (icon translations), so
  the integration genuinely satisfies the `icon-translations` quality rule.
- **Service names/descriptions are now translatable** via a `services` block in
  `strings.json`/`translations/en.json`; `services.yaml` describes structure only.
- **CI now enforces quality**: ruff, Pyright (strict), hassfest and HACS
  validation in addition to the tests, and the test job installs the same library
  floor the integration ships (`>=0.10.0`) instead of an older pin.
- `quality_scale.yaml` was corrected to reflect reality (`brands` exempt for a
  custom integration; `test-coverage` in progress).

### Added
- A comprehensive test suite reaching **100% line coverage** (up from ~38%),
  enforced by a CI `--cov-fail-under=100` gate: every entity platform, the config
  flow (incl. reconfigure/DHCP), the proxy view's signature/auth boundary,
  transcode and cache logic, coordinator device-sync/token/cleanup/auth paths, the
  media source, diagnostics, and full integration setup/unload. The
  `test-coverage` quality-scale rule is now met.

## [2.7.1]

### Changed
- **Raised the `python-aidot-cameras` floor to `>=0.10.0`** to pull in the
  library's security hardening: CSPRNG-generated media-keying material, an
  SDP-rewrite DoS guard, opt-in DTLS certificate pinning and playback TLS
  verification, and credential-key separation. No integration code change; the
  fix installs on restart.

### Documentation
- **Listed L2 battery cameras (A001513)** in the Supported devices table
  alongside the M3 Pro (A000088) and PTZ (A001064).

## [2.6.4]

### Fixed
- **Corrected the recommended dashboard card config.** The README recommended the
  Advanced Camera Card with `live.provider: go2rtc`. For a non-Frigate camera the
  card resolves no go2rtc stream unless `live.go2rtc.url` and `live.go2rtc.stream`
  are set by hand, so tiles pinned to bare `go2rtc` start inconsistently or never
  start. The guidance now uses `provider: ha` - the native Home Assistant path
  this integration already wires to go2rtc WebRTC - and adds a static `16:9`
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
  `.../hass-aidot-cameras-cameras` URL. The rewrite now optionally consumes an
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
  was gated only by the unguessable event id - replayable indefinitely. The
  media source now mints a URL signed with an HMAC over `device + event +
  expiry` (per-process secret, never persisted), verified in the view with a
  constant-time compare and a 6 h expiry.

### Changed
- **`iot_class`** corrected from `cloud_polling` to `cloud_push` (the
  integration receives MQTT signaling and cloud motion-event push), and
  **`loggers: ["aidot"]`** added so HA can surface the library logger in the UI.
- **Coordinator background tasks** (LAN-control attach, stop-streaming /
  stop-motion on device removal, per-coordinator init) moved from
  `hass.async_create_task` to `config_entry.async_create_background_task` - now
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
control entities (siren, floodlight, night vision, motion sensitivity, ...).
