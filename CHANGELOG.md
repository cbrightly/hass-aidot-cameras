## [2.9.11]

### Changed
- **Rollback to the last combination with confirmed working video.** This is the
  2.9.6 integration code with the library pinned EXACTLY to 0.12.8, published as a
  new version so it can be installed normally.

  Why an exact pin: 2.9.6 asked for `>=0.12.8`, so pip kept whatever newer library
  was already present. That is why rolling the integration back on its own did not
  restore video - the regression was in the library, and the floor let it stay.

  What this gives up, deliberately and temporarily: the idle-release work
  (0.12.9-0.12.12), the concurrency-cap sizing, and the offline-stop behaviour.
  Cameras will keep streaming when nobody is watching, as they did before. That is
  the trade for having video back while the DTLS media path is diagnosed off the
  live system.

# Changelog

All notable changes to the AiDot Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); versions
match the `version` in `custom_components/aidot/manifest.json`.

## [2.9.6]

### Fixed
- **Cameras beyond the third never played.** The library caps how many camera
  serves run at once (host protection, default 3) and a camera holds its slot for
  the life of its serve - so on an account with more cameras than the cap, the
  extras did not take turns, they never streamed and nothing said why. Confirmed
  on this fleet: with 4 DTLS cameras the fourth logged "waiting for a stream slot
  (cap reached)" every time, and it was exactly the camera that would not play.
  The integration now sizes the cap to its own camera count on every device-list
  sync, so every camera can hold a slot. Setting `AIDOT_MAX_CONCURRENT_STREAMS`
  still overrides it for anyone protecting a small host.

## [2.9.5]

### Fixed
- **Requires library 0.12.7, which restores live video and camera audio.** Two
  independent causes had to be fixed. First, camera audio was costing the video: the
  SDP advertises two payload types per media line and ffmpeg binds the first, so the
  camera's PCMA audio was discarded in favour of the advertised PCMU - and since the
  mpegts mux withholds its PAT/PMT until every mapped stream has produced a packet, a
  viewer got an accepted connection and then nothing at all, with signaling
  perfectly healthy. The audio payload type is now narrowed to the one in use, with
  a video-only fallback so audio can never cost the picture - and the serve waits
  for the session's first media before launching, rather than a deadline shorter
  than the camera's own cold start, which is what made audio intermittent. Second,
  the MQTT
  password the cameras sign in to the broker with rotates on every account login,
  and it was being persisted, so a dead credential came back from disk on every
  start and the broker refused it forever - which killed WebRTC signaling while
  snapshots kept working. See the library changelog for the full set of fixes;
  this raises the requirement floor so an existing install actually gets them.
- **A bulb that drops off the LAN no longer republishes its last-known state as
  current.** The "we have heard from this device" flag was set on the first LAN
  push and never cleared, so when the six-hourly device-list refresh carried the
  cloud's reachability flag back on, the light became available again and showed
  its stale on/brightness/colour - with every command failing, since control is
  LAN-only. The flag now follows the LAN session in both directions, so an
  unreachable bulb reads as unknown rather than confidently wrong.
- **A light coordinator that fails to start now logs why.** Its failure handler
  read an attribute that exists only on the camera client, so it raised a second
  error inside the handler and lost the original cause.
- **A camera-looking device the library cannot build a camera client for is now
  skipped rather than driven with camera calls.** The integration and the library
  each had their own idea of what a camera is, and the library's is what decides
  which class comes back; on a disagreement the first camera-only call failed,
  taking down a whole platform's setup rather than one device. The library's
  answer now decides, and a mismatch is logged.
- **A rejected login now says so.** The library maps only one of the server's
  several bad-credential codes to a typed error; the others escaped the config
  flow as an opaque "Unknown error" plus a traceback in the log, so a wrong
  password looked like a bug. Such a login is now reported as invalid
  authentication, with the underlying error logged.
- **Bulbs that are online in the app but missing from Home Assistant.** The
  light filter required `type == "light"` exactly and indexed `aesKey` blindly -
  an empty list raised IndexError out of the device-list filter, which dropped
  every remaining light rather than the odd one. Devices are now accepted on
  advertised light capability as well as type, the aesKey check is defensive,
  and any cloud device that becomes neither camera nor light is logged so a
  missing entity is diagnosable instead of silent.
- Library floor raised to `python-aidot-cameras[webrtc]>=0.12.2`, which
  re-fetches the MQTT password when the broker rejects it (any other login
  rotates it) instead of retrying a dead credential forever, and stops leaking
  go2rtc stream registrations.

## [2.9.3]

### Fixed
- **A light that is not reachable over the LAN no longer reports a made-up
  "off".** These devices are controlled on the LAN only, so with no LAN session
  the library holds default values, not the device's real state. A bulb that was
  powered on but off-LAN therefore showed as "off" - and tapping it returned an
  opaque HTTP 500. Such a light now reports its state as unknown, and a command
  it cannot deliver raises a clear "not reachable over the local network"
  message instead of a server error.
- Library floor raised to `python-aidot-cameras[webrtc]>=0.12.1`, which stops
  logging one warning per unsupported device (Zigbee sub-devices, remotes) on
  every device-list refresh.

## [2.9.2]

### Fixed
- **AiDot lights stayed unavailable after the 2.9.0 upgrade.** Non-camera devices
  now run on upstream's device client, which only marks a device online after a
  successful LAN login - so a bulb that is reachable through the cloud but whose
  LAN control port was not reached (not yet discovered, different VLAN, asleep)
  never became available. The coordinator now carries the cloud's reachability
  flag onto those devices, which is what the camera path already did.

## [2.9.1]

### Fixed
- **Lights came back unavailable after the 2.9.0 upgrade, and reloading the
  integration failed.** 2.9.0 moved non-camera devices onto upstream's own device
  client, but the coordinator still called helpers that only the camera client
  provides (`set_status_fresh_cb`, `connect_and_login`, `device_id`), so every
  light coordinator raised `AttributeError` on setup and the config entry could
  not unload (`'DeviceClient' object has no attribute 'set_status_fresh_cb'`).
  The coordinator now speaks upstream's own API for those devices - assigning
  `on_status_update`, reading `info.dev_id`, and treating upstream's
  `DeviceState.AUTHENTICATED` as connected - while camera clients keep using
  their own helpers.

## [2.9.0]

### Changed

- **Library floor raised to `python-aidot-cameras[webrtc]>=0.12.0`, which now
  extends the upstream `python-aidot` library instead of forking it.** The library's
  import name changed from `aidot` to `aidot_cameras`, so every import here moved
  with it. Non-camera devices (lights, plugs, switches) are now handled by
  upstream's own code, with the camera layer out of their path entirely; camera
  behavior is unchanged.
- **Debug logging now covers both namespaces.** The manifest declares `aidot` and
  `aidot_cameras` as loggers, so setting either to debug captures the full picture
  (upstream device/protocol logs under `aidot`, camera and account logs under
  `aidot_cameras`).

## [2.8.14]

### Fixed
- **RGBW+CCT bulbs no longer show a stale color when resting in white/color-temp
  mode** (e.g. a bulb set to 3000K that kept displaying its last purple in HA).
  These bulbs report unambiguous single-field deltas but an ambiguous login-sync
  that returns both color registers, so the active mode was unknown at startup
  and the entity surfaced the retained RGB color. The light now defaults dual-mode
  bulbs to color temperature at startup and follows the device's active color mode
  (from the library's `active_color_mode`, floor raised to
  `python-aidot-cameras[webrtc]>=0.11.14`); an unambiguous color/CCT change flips
  the mode correctly.

## [2.8.13]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.13`.** Fixes an
  SDES (battery) camera stream break: during a key-restart (when the camera's
  answer SRTP key differs from the offer) the ffmpeg bridge's process-observe
  loop could tear itself down on the old process's exit and starve the restarted
  ffmpeg, turning a fast restart into a 40-60s reconnect. Also makes the DTLS
  serve-open timeout tunable via `AIDOT_DTLS_SERVE_OPEN_TIMEOUT_S` (default 30,
  unchanged).

## [2.8.12]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.12`.** Quiets an
  idle DTLS camera (model IPC.A000088) that the cloud reports online but that
  never answers WebRTC, which flooded the log and burned CPU. The serve path no
  longer runs a discarded H.264 decode (the sole source of a
  172-warning-per-11min flood; a keyframe/gap canary at DEBUG replaces it),
  `aioice.ice` / `aioice.turn` are capped at WARNING, the vendored h264
  decode-failure warning is rate-limited on the live-view path, an expected
  ffmpeg teardown (signal -9) is logged at DEBUG instead of WARNING, and retries
  for a persistently-unreachable idle camera back off to a slow probe (default
  after 5 consecutive failures, `AIDOT_DTLS_SLOW_PROBE_THRESHOLD`).

## [2.8.11]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.11`.** Fixes a
  camera-signaling correctness bug: the per-stream MQTT session now ends promptly
  (with a warning) when the broker terminally drops it after connecting - e.g. the
  account's persistent client reclaiming the same client id ("session taken over")
  or revoked credentials - instead of polling a dead connection until the stream's
  duration deadline and silently starving the camera of signaling. Also removes a
  redundant, log-flooding aioice debug override on the DTLS path.

## [2.8.10]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.10`.** Turning on
  debug logging for the integration no longer floods the log with per-packet lines
  from `aioice` (the STUN/TURN engine). The 0.11.9 flood cap only quieted the
  vendored aiortc RTP loggers; the external `aioice.ice` / `aioice.turn` loggers
  are now capped the same way, so `aidot` debug stays useful (DTLS, ICE state, and
  data-channel setup still log) without the packet firehose that can strain the
  recorder on a microSD host. An explicit log level you set yourself still wins.

## [2.8.9]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.9`.** A camera that
  connects but never sends video (NO_MEDIA - e.g. a battery or PTZ camera whose
  media cannot traverse the cloud relay) no longer fills the log with repeated
  `ffmpeg SDES stderr` WARNINGs; that expected empty-output case now logs at
  debug, while a genuine ffmpeg error still warns. Added a Troubleshooting entry
  for cameras that connect with a blank picture.

## [2.8.8]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.8`.** A camera the
  cloud reports offline (e.g. an unplugged or dead camera) no longer drips
  repeated "open failed" warnings into the log; those expected failures now log at
  debug while the device is offline. A genuine failure on an online camera still
  warns.

## [2.8.7]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.7`.** This ships
  two fixes surfaced from a live log review:
  - a blocking file read (the SDES sprop cache) no longer runs on the event loop,
    so Home Assistant no longer flags a blocking call "causing stability issues";
  - the vendored aiortc per-packet RTP loggers are capped by default, so enabling
    debug logging on the `aidot` logger no longer floods the log with thousands of
    packet lines per second (useful DTLS/ICE/SCTP debug still flows).

## [2.8.6]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.11.6`.** This ships
  the camera-module correctness hardening from a whole-module library audit:
  - a snapshot taken during a live view no longer briefly interrupts that view;
  - a transient MQTT broker drop no longer ends a camera stream's signaling;
  - a cancelled cold open no longer leaks sockets/threads/temp files (which could
    eventually reach "Too many open files");
  - event-video and thumbnail fetches now refresh an expired token and retry;
  - truncated H.264 parameter sets are no longer cached corrupt.

  The two concurrency/reconnect fixes were verified live against real cameras.

### Fixed
- The hub **Reload** button now shows an `mdi:reload` icon.

## [2.8.5]

### Fixed
- **The integration no longer reloads itself on every token refresh.** The
  library persists a refreshed account token back into the config entry
  (`async_update_entry`), and the update listener fired on that data write too,
  reloading the whole integration on each token refresh. Every reload churned
  all entities, **re-primed the camera motion poll** - so a motion event landing
  in the window right after a reload was dropped and occupancy reset to off - and
  interrupted active streams. The listener now reloads only when the *options*
  actually change. (This is why motion/occupancy could appear stuck and why the
  integration seemed to restart on its own.)

### Added
- **A Reload button on the AiDot hub device.** One-click reload of the
  integration (unload + set up again) - handy after a network change or to
  re-prime the motion poll without restarting Home Assistant. The entry's
  built-in Reload (its `...` menu) continues to work as well.

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
