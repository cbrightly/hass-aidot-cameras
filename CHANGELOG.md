# Changelog

All notable changes to the AiDot Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); versions
match the `version` in `custom_components/aidot/manifest.json`.

## [2.8.4]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.5`.** This ships
  two library improvements to users:
  - **Battery cameras stay awake for the whole live view.** A battery model's
    low-power timer used to return it to sleep ~25 s into a view, dropping the
    stream mid-view; the library now renews the cloud keep-alive every 20 s for
    the duration of a battery stream (app parity). Mains cameras are unaffected.
  - The library's `paho-mqtt` floor was lowered to `>=1.6.1`, which resolves the
    dependency conflict with Home Assistant's custom-component test tooling
    (`pytest-homeassistant-custom-component` pins `paho-mqtt==1.6.1`).

## [2.8.3]

### Added
- **CI now runs HACS + hassfest validation.** A `hacs/action` job (default
  branch / weekly schedule - the action validates the published default branch,
  not a feature ref) and a hassfest job keep the custom-repo structure and the
  manifest honest. The `brands` check is skipped by design: the `aidot` brand
  belongs to the core integration this repo overrides. The README and CI now
  document that domain override explicitly.

### Fixed
- **Two exception messages were invalid per Home Assistant's translation rules.**
  `ptz_failed` and `talk_no_audio` wrapped their `{direction}`/`{media}`
  placeholders in single quotes, which HA reserves for literal-quote escaping;
  hassfest rejected them. Unquoted (surfaced by the new hassfest job).
- **`ffmpeg` is now declared in `after_dependencies`.** The clip-transcode proxy
  uses Home Assistant's `ffmpeg` component but never declared it - hassfest flags
  undeclared component use.
- **`hacs.json` no longer carries manifest-only keys.** It had `integration_type`
  and `config_flow` (which belong in `manifest.json`), so HACS flagged the file
  as invalid. Reduced to the valid minimal form.

## [2.8.2]

> Version note: this release deliberately numbers **above** the abandoned
> `2.8.0`/`2.8.1` tags (an old, since-superseded branch that was renumbered back
> to the 2.7.x line). Those orphans sit at higher semver than 2.7.5, so HACS -
> which resolves by version, not publish date - could offer them as "newest"
> even though they predate and lack every 2.7.2->2.7.5 fix. Releasing the
> current code as 2.8.2 makes the newest semver the current, correct code for
> everyone, without deleting history.

### Security
- **Signed clip URL no longer leaked to the debug log.** The clip-transcode
  proxy logged the full ffmpeg argv at debug, which included the signed,
  time-limited CloudFront/HLS media URL (a bearer credential). The URL is now
  redacted in that log, matching the integration's existing signed-URL
  redaction policy.

### Fixed
- **Clip cache could be corrupted by a lock-eviction race.** The per-event
  transcode lock was evicted on a bare `not lock.locked()` check, which can
  drop a lock while a woken waiter is still queued (release wakes it before
  `locked()` flips) - letting two ffmpeg encodes race the same cache file. The
  eviction now also requires no queued waiter.
- **Custom stream-port base is cleared when unset.** `AIDOT_SERVE_PORT_BASE`
  was applied from the option but never cleared, so removing the option left
  the stale process-global port until a Home Assistant restart. It is now
  cleared on reload when the option is absent.

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.3`**, pulling in
  the library's hardening pass (mqttPassword no longer logged; atomic 0600
  token-cache write; corrupt-cache and several DTLS/SDES/playback error-path
  fixes).

## [2.7.5]

### Fixed
- **A token refresh could silently fail to persist.** `token_fresh_cb`
  persisted `login_info` into the config entry via a shallow `.copy()`.
  `login_info` also doubles as the account-shared cache for the
  persistent-MQTT connection and its guarding `asyncio.Lock` (persistent
  MQTT is on by default), so once that connection existed, the same live
  Lock ended up in `config_entry.data` too - which Home Assistant later
  serializes to JSON when it persists config entries to disk. This is the
  exact bug `python-aidot-cameras` 0.11.2 fixed for its own standalone CLI;
  found here as the natural next place the same `login_info` object gets
  persisted. Now uses `AidotClient.serializable_login_info()` (new in
  0.11.2, hence the bumped floor below) instead.
- **The SDES push serve toggle now has a proper label and description.** The
  `sdes_push` option shipped in 2.7.4 without a `strings.json`/`en.json`
  entry, so it rendered in the options dialog as a bare `sdes_push` key with
  no explanation. Added its label and description alongside the other SDES
  options.

### Changed
- Bumped the `python-aidot-cameras` floor to `>=0.11.2` - now required, not
  just recommended, since `serializable_login_info()` only exists there.

## [2.7.4]

### Fixed
- **SDES cameras (A001513/A001064) stream reliably under Home Assistant: serve
  mode is now PUSH.** The legacy pull chain - a single-connection ffmpeg
  `-listen` socket behind the serve-port relay that go2rtc PULLs - could jam:
  an eager go2rtc pull dialed during the 25-70 s SDES cold window, went stale
  in ffmpeg's one connection slot, ffmpeg died on the stale disconnect, the
  watchdog restarted cold, and the two sides kept missing each other - no
  viewer ever got media (reproduced live on HA 2026.7.1; go2rtc could not pull
  a single frame in any mode while the library logged healthy media). SDES
  cameras now PUBLISH into HA's go2rtc over RTSP (`sdes_push`, default on):
  ffmpeg pushes outbound - no listen slot, no relay, no pull-timing race - and
  go2rtc natively fans out to every viewer. Validated live end-to-end on a
  real A001513: H264 (1280x960) + PCMA tracks in go2rtc and frame grabs within
  seconds. DTLS cameras keep the proven pull serve.
- go2rtc only creates streams that have a source and rejects publish to
  unknown names (verified live), so push mode still registers the legacy
  serve URL as an inert placeholder - nothing listens on it, a consumer
  attach costs one instantly-refused dial - and the RTSP publish feeds the
  stream.
- Note: in push mode the library cannot observe viewer connections, so the
  no-viewer idle release does not apply - the session stays warm until
  stopped. Ideal for powered cameras; turn `sdes_push` off for battery-only
  SDES cameras if standby drain matters more than view latency.

## [2.7.3]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.1`.** Pulls in the
  offline-keepalive fix: reconnect loops no longer chase cloud-offline cameras on
  the normal backoff cadence (each dead-camera retry held an open-gate slot for
  the full 30 s signaling timeout; observed live, two unpowered cameras pushed a
  healthy camera's cold open past two minutes). Retries pause while a device is
  cloud-offline and resume within ~30 s of it coming back online.

## [2.7.2]

### Fixed
- **Platinum quality scale re-earned.** Strict typing had drifted since v2.4.7
  (13 pyright-strict errors against HA 2026.2.3 + library 0.11.0): import
  locations that moved (`TALK_PCM_*` from `aidot.camera.constants`,
  `CameraDeviceInformation`/`CameraStatusData` from `aidot.camera.models`,
  `MediaClass`/`SirenEntityFeature`/`async_process_play_media_url` from their
  defining Home Assistant submodules), `resolve_connection_mode` now accepts any
  `Mapping` (config-entry options are a `MappingProxyType`), and the camera-only
  floodlight/siren properties narrow `coordinator.data` to `CameraStatusData`
  before touching camera attributes. pyright strict: 0 errors; 56 tests pass.
  `quality_scale.yaml` marks `strict-typing: done` and the manifest claims
  **platinum**.

## [2.7.1]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.0`.** Pulls in two
  fixes validated live: relay-only SDES (battery) cameras stream again (the
  late-arriving relay candidates are now nominated - library #112), and the
  deferred security-review hardening (self-scoped DTLS 1.0 floor, MQTT command
  truthfulness, opt-in playback TLS, LAN de-eligibility - library #113).

### Fixed
- **Quality-scale claim restored.** The integration earned its way up the quality
  ladder (Bronze -> Silver -> Gold, with Platinum strict typing done in v2.4.7),
  but the manifest claim stayed `bronze` and the `quality_scale.yaml` checklist
  was lost in a repository re-plant. The checklist is restored with truthful
  statuses (`repair-issues` exempt - its flow was removed; `strict-typing` todo -
  drifted since v2.4.7) and the manifest now claims **gold**.

## [2.7.0]

### Fixed
- **Concurrent cold-open failures on startup.** Mains-powered cameras are warmed
  in the background at setup (staggered; battery cameras excluded), so multiple
  cameras loading at once no longer serialize through the library's open gate
  past Home Assistant's stream deadline. (#52)

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
