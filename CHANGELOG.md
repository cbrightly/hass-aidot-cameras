# Changelog

All notable changes to the AiDot Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); versions
match the `version` in `custom_components/aidot/manifest.json`.

## [2.17.5]

### Fixed

- **Fewer stalls on a camera with a weak signal.** Requires library 1.0.0b25.

  When a video packet goes missing the camera is asked to send it again. A
  replacement that arrives quickly is slotted back into place and the picture
  is repaired -- but one that arrives too late cannot be, and forwarding it
  anyway only disordered the stream and made the stall worse. Late replacements
  are now discarded.

## [2.17.4]

### Fixed

- **A camera whose live view stopped after a few minutes.** Requires library
  1.0.0b24. Two separate faults, both in the SDES serve.

  A camera that answers one video codec and sends another had its serve killed
  at startup: when video had not arrived before the serve launched, the stream
  description was built from what the camera *said* it would send rather than
  what it was told to send, so the serve waited for a codec that never came and
  exited immediately. The H.264 pin option was on the whole time and this path
  ignored it.

  Separately, and this is the one that produced the "the publisher dies every
  three minutes" reports: a serve that Home Assistant stopped on purpose was
  being logged as an unexpected crash, with a stack of scary-looking output
  attached. Nothing was crashing. The camera was being released normally when
  nobody was watching it, and the log said otherwise.

- **The auto-tracking switch could go missing from the camera that needs it.**
  2.17.3 gated the switch on pan/tilt capability, but the platform marks a
  camera "done" the moment any of its switches is created -- and eight ungated
  ones always are. A PTZ camera whose direction codes had not arrived yet at
  setup would therefore never be reconsidered, and lost the switch until Home
  Assistant restarted. Capability-gated switches now keep their own record, the
  way the PTZ buttons always have.

### Changed

- Diagnostics also reports **which upstream shape** is installed, not only the
  version. Upstream shipped the same private API under 0.3.53 and 0.3.56 with a
  five-day excursion to an incompatible one in between, so the version string
  cannot answer the question the field exists for; the library detects the shape
  by capability and now publishes it.
- The "can this camera pan or tilt" rule lives in one place shared by the PTZ
  buttons and the auto-tracking switch, instead of a copy in each. Two copies of
  a hardware-capability rule drift, and the failure is silent in the direction
  that matters -- a control that does nothing.

## [2.17.3]

### Fixed

- **The auto-tracking switch no longer appears on cameras that cannot pan or
  tilt.** It shipped on every camera and did nothing on most of them.

  Measured over the local control channel, with the owning account, on all
  three A000088 units here: writing `trackingMode=1` is acknowledged and the
  camera keeps its own value -- read-back returns 0 every time. These cameras
  have no motor, so there is nothing for auto-tracking to move.

  That is the same behaviour the IR-light switch was removed for in **2.15.0**:
  a control that flips in the UI, reports nothing wrong, and has no effect. The
  switch platform applied no capability gate at all, while PTZ *buttons* have
  been gated on the camera's advertised direction codes all along; auto-tracking
  now uses the same gate, with the same model-id fallback for the window where
  the direction codes have not arrived yet.

  Cameras that really do pan or tilt are unaffected. A leftover
  `switch.<camera>_auto_tracking` on a fixed camera can be deleted.

## [2.17.2]

### Added

- **Diagnostics now reports which library versions you are running** --
  `python-aidot-cameras` and the upstream `python-aidot` it extends, both as
  resolved on your machine.

  The upstream requirement is a range rather than a pin, so two people on the
  same integration version can legitimately be running different upstream
  releases, and nothing showed which. The library's GitHub page cannot answer
  it either: it is recorded as a fork of upstream, so it permanently displays
  "N commits behind" even though no upstream code is merged -- upstream is an
  ordinary dependency.

  Settings -> Devices & Services -> AiDot -> **Download diagnostics**. Please
  include it in bug reports.

## [2.17.1]

### Fixed

- **A camera whose live view stalls or updates only every few seconds, while
  the others are fine.** Requires library 1.0.0b23.

  This supersedes the fix claimed in **2.17.0**. That release addressed a real
  problem -- a decoder configuration carried over from an earlier session -- but
  it was not what was breaking this camera, and the symptom outlived it. The
  cause is below, measured on the wire this time.

  The camera was losing about 1-2% of its video packets on the air and nothing
  was asking for them back, so the picture arrived with pieces missing. A
  browser's WebRTC decoder hides that; the Media Source Extensions player
  treats a damaged keyframe as fatal and stops. Cameras with large keyframes
  are hit hardest -- on the affected model roughly two thirds of keyframes
  arrived damaged.

  The library now asks the camera to resend what went missing. Measured on the
  affected camera: 98.4% of lost packets recovered, against none at all before
  the change. Browser playback improved with it, though that camera's link is
  erratic enough that how often it plays cleanly varies hour to hour. A camera
  on a healthy link is unaffected -- it loses nothing, so nothing is requested.

  Not a complete cure on a very poor link: a resend that arrives too late is
  still discarded, so an occasional stall remains. The underlying fix for that
  is signal, not software. The requests can be turned off with
  `AIDOT_SDES_NACK=0`.

## [2.17.0]

### Fixed

- **A camera whose live view updated only every few seconds, while the others
  were fine.** Requires library 1.0.0b22.

  Some cameras change their H.264 parameter sets between sessions -- one model
  does it on entering night mode, because the declared frame rate is part of
  those parameters. go2rtc builds its decoder configuration when a camera's
  track is first published and then reuses it, so every later session was
  served a configuration captured from an earlier one. A player using Media
  Source Extensions reads that configuration once, so it could no longer decode
  anything between keyframes and the picture updated roughly once every few
  seconds. Playing the same camera over WebRTC was unaffected, which is why it
  looked like a player bug rather than a stream one.

  Affected cameras now get a fresh stream definition each time, so the decoder
  configuration is rebuilt from the session actually being played. This happens
  before the camera starts publishing, so the stream is never interrupted.

  Only cameras actually seen to change their parameter sets are treated this
  way; everything else registers exactly as before.

## [2.16.1]

### Documentation

- **Troubleshooting: one camera updating only every few seconds while the
  others are fine.** This is a decoder problem on the Media Source Extensions
  playback path, not a camera fault, and the fix is to view that camera with a
  WebRTC card.

  Some cameras change their H.264 SPS between sessions -- and at least one
  changes it on entering night mode, because the declared frame rate lives in
  the SPS. MSE reads its decoder configuration once from the init segment, so a
  later mismatch makes every inter-frame fail and the picture updates only on
  keyframes. WebRTC reads parameter sets in-band and is unaffected. The README
  now records the signature, the console error to look for, and the fix.

## [2.16.0]

### Added

- **"Pin camera video to H.264" option**, for a camera whose live view is very
  choppy while the others are fine.

  The stream offer asks for H.264 first, but some cameras disregard that and
  answer with H.265 -- and the codec they pick also changes the frame size
  (H.264 gives 1280x720, H.265 gives 2560x1440). A player that cannot decode
  the H.265 stream shows roughly one frame per keyframe, which on a camera with
  a ~4 second keyframe interval looks like a picture that updates every few
  seconds. The Home Assistant iOS app, which plays through Media Source
  Extensions, is one such player.

  Turning this on removes the camera's choice, so it always sends H.264.

  Off by default. It affects every SDES camera, and a camera that could only
  encode H.265 would be left with nothing to send, so it is opt-in rather than
  assumed. Leave it off unless one camera is stuttering badly; the H.265 mode is
  otherwise a higher-resolution stream at a lower bitrate.

## [2.15.7]

### Changed

- **Requires library 1.0.0b21.** SDES (battery) cameras now carry audio to
  browsers that use Media Source Extensions.

  These cameras publish G.711 (`PCMA/8000`). WebRTC carries that natively, so
  a WebRTC card always had sound -- but fMP4 / MSE has no mapping for G.711, so
  anything on the MSE path negotiated video only and played silence. The stream
  now publishes AAC, so both paths carry audio from the same publish. Video is
  unchanged.

  A camera whose audio payload type was never observed still publishes video
  only, and turning **SDES camera audio** off still gives the previous
  audio-free stream.

## [2.15.6]

### Changed

- **Requires library 1.0.0b20.** Home Assistant no longer restarts a camera
  stream every minute or so with "Timestamp discontinuity detected".

  The library used to hand its muxed MPEG-TS through a `-c copy` ffmpeg before
  go2rtc. That hop was the only component in the chain that lost timestamps --
  8 video frames with no timestamp at all in 12,729, against zero in 64,981
  straight from the muxer. go2rtc turned those into timestamp 0, and Home
  Assistant read the result as a jump backwards and tore the stream down. The
  hop is gone; the muxer now feeds go2rtc directly.

  Measured on four cameras for about 20 minutes each: no bad timestamps and no
  discontinuity errors, with audio unchanged and frame rate nominal.

  The floor moves because Home Assistant installs a requirement only when the
  version already present does not satisfy it -- permitting a newer library is
  not the same as fetching it.

## [2.15.5]

### Changed

- **Requires library 1.0.0b18**, up from `>=1.0.0b14`. The floor is what
  actually delivers the fix: Home Assistant installs a requirement only when
  the version already present does not satisfy it, so a floor that merely
  permits a newer library never fetches it.

### What 1.0.0b18 fixes

- **A live view no longer arrives in bursts.** The camera re-sends runs of
  frames it has already sent -- 41% and 46% of everything reaching the muxer on
  the two cameras measured here. Each repeat was written a single tick after
  the frame before it, so up to 41 already-served frames landed with no
  presentation time between them: a burst, roughly twice a second, for the
  player to work through.

  Measured on the served stream, before and after:

  | | before | after |
  |---|---:|---:|
  | packets per second | 25.46 | 15.01 |
  | frames collapsed to a single tick | 41.1% | 0.0% |
  | bursts | 75 (longest 38 frames) | 0 |
  | effective frame rate | 15.00 | 15.00 |

  The same video, 41% fewer packets muxed and pushed, and no bursts. Confirmed
  on three further cameras. Audio and lip sync are unaffected: video arrived at
  15.01/s with no gap over 116 ms, audio at 46.89/s with no gap over 21 ms,
  nothing above 0.5 s on either, and the audio-to-video offset moved from 65 ms
  to 82 ms across 90 seconds.

- **A camera gets long enough to answer.** A serve attempt re-publishes its
  offer at 15 s and again at 30 s, while the attempt itself timed out at 30 s --
  so the second resend went out as the attempt died. Over 0.93 hours and 122
  opens, 14 answers arrived between 30.7 s and 99.5 s, every one of them an
  accepted session that had nowhere to go. The timeout is now 75 s. On a
  healthy camera this changes nothing: 56 opens against idle cameras answered
  with a median of 0.42 s and a maximum of 1.48 s.

- **A slow open no longer blocks the other cameras twice over.** An open waits
  for signalling and then for ICE, and both read the same timeout, so raising
  it doubled the worst case while holding the shared open gate. ICE now has its
  own budget, clamped to the open timeout so lowering that still fails fast.

### Known issue

Home Assistant's stream worker still logs an occasional "Timestamp
discontinuity" and restarts the stream -- roughly once every 19 seconds on one
camera here. The packet responsible carries a fixed negative timestamp and is
not produced by this integration's muxer, which starts at zero on a keyframe
and never goes backwards. It is being tracked separately.

## [2.15.4]

### Changed

- Documentation only. The README listed an IR-light control that was removed in
  2.15.0 and omitted the four switches added in 2.14.0, so it described a
  control that does nothing and hid four that work. PTZ is no longer advertised
  as "pan / tilt / zoom" outright - the buttons follow what each camera
  advertises, and the reference spotlight is pan-only. A troubleshooting entry
  covers the leftover `switch.<camera>_ir_light` entity.

  The library floor stays at `>=1.0.0b14`: the matching library release is
  documentation too, and there is no reason to make every install fetch it.

## [2.15.3]

### Fixed

- **Diagnostics published LAN addresses.** `_redact_stream_health` masked the
  nominated ICE pair but never touched `stream_health.ice`, the full candidate
  list carrying the same `host:port` strings. A real diagnostics download from
  a live system contained four LAN addresses - in the file a reporter attaches
  to a public issue, which is precisely what that function exists to prevent.
  Every list of pairs is now swept, not just the nominated one, so a future key
  cannot reopen the same hole.

## [2.15.2]

### Fixed

- Requires `python-aidot-cameras` 1.0.0b13. A cancelled camera open no longer
  orphans its MQTT session thread for an hour - Home Assistant cancels opens
  routinely (idle release, stream-worker timeouts), and executor workers are
  finite, so those orphans accumulate into opens that never return.

## [2.15.1]

### Fixed

- Requires `python-aidot-cameras` 1.0.0b12, which removes an open that could
  hang with no success, no error, and no response to cancellation - a camera
  that never loads and never says why. The floor moves rather than merely
  permitting the new version, because a floor that permits a release does not
  fetch it: an existing install stays where it is until the floor forces the
  upgrade.

## [2.15.0]

### Removed

- **The IR-light switch.** `nightVisionIRLight` acks the write and keeps its own
  value - confirmed by read-back on both A000088 units on 2026-08-14, so it is
  model behaviour rather than one bad camera, and no model on the reference
  fleet honours it. It shipped as a switch you could toggle with no effect and
  no error, which is exactly why `SdcardRecord_Enable` never became one. The
  camera's reported state is still parsed; only the control is gone.
- `sd_playback.py`, which nothing imported. Three helpers reachable only from
  their own tests, shipped in every HACS snapshot, left over from an SD-playback
  stage that is closed as not feasible. The SD browser is unaffected - it lists
  what the card holds and marks the items `can_play=False`, as before.

## [2.14.0]

### Added

- Switches for four camera settings that were readable but not controllable:
  the timestamp overlay, HDR, floodlight automation and voice prompts. Each was
  toggled against live hardware and read back before it earned a switch - on
  this firmware a write is acked whether or not it takes, and eight of the
  fourteen attributes probed acked and kept their own value.

### Changed

- Requires `python-aidot-cameras` 1.0.0b9, which carries the setters these
  switches call and fixes the standalone `aidot-go2rtc <id> -` producer that
  never ran. The producer is not used by this integration - it manages go2rtc
  itself - so that fix matters only if you also run the CLI by hand.

## [2.13.3]

### Added

- Diagnostics reports whether local (LAN) control attached for each camera, and
  the address it attached to. Local control previously had no observable
  surface at all: it attaches in a background task, logs one line at INFO on
  success and DEBUG on every failure, and appeared in no entity, attribute or
  diagnostic, so "is local control working here" could not be answered from a
  bug report or from the box itself. `attempted` is reported separately from
  `attached`, because not-attached covers several unrelated causes - the subnet
  sweep never saw the camera, it does not advertise localCtrFlag, it is battery
  powered and sleeps through unicast discovery, the account is a shared-home
  member and the device refuses the login, or the option is simply off.

### Fixed

- The recordings day-count zip is strict. `asyncio.gather` returns one result
  per input in order, so the lengths are equal by construction and it cannot
  truncate today; strict turns a silently short day list into a loud failure if
  that ever changes.

## [2.13.2]

### Fixed

- The diagnostics download no longer publishes the user's IP addresses. The
  nominated ICE pair is built from `host:port` candidate strings, and
  diagnostics is the file a reporter attaches to a public issue. The addresses
  are redacted; `local_type` / `remote_type` / `transport` stay, because
  relay-vs-direct is the signal the block exists for.
- Camera entities can go unavailable again. A failing cloud refresh was
  swallowed and the last known status returned, so through an outage every
  sensor and the occupancy binary_sensor kept serving frozen values and stayed
  green. It now raises after three consecutive failures; one blip still does
  not flap the fleet, and any success resets the count.
- The "SD card" sensor says whether there is a card instead of echoing the raw
  `SDcardStatus` cloud property. That property is not card presence - on an
  A000088 it reads inverted, so the camera holding 125 recordings showed 0 and
  the one with an empty slot showed 1. It now reports Card in slot / Slot empty
  / Unknown, and Unknown stays Unknown for the cameras that report neither
  cloud key.

### Changed

- The library floor moves to `python-aidot-cameras>=1.0.0b8`. A floor of b7 is
  satisfied by an installed b7, so Home Assistant never fetched the newer
  library and existing installs stayed put; permitting a version is not
  requiring it.

## [2.13.1]

Documentation only - no code change from 2.13.0.

### Fixed

- **The README did not mention recordings in any form.** Browsing recordings -
  cloud clips and the listing of what a camera holds on its own SD card - is the
  headline of 2.13.0, and the one place a prospective user looks did not say the
  feature existed.
- **The wiki's Cloud recordings page gave the wrong path.** It still said clips
  appear under `AiDot -> <camera name>`; each camera now splits into `Cloud` and
  `On device`, so that navigation had been wrong since the split shipped. A new
  **On-device recordings** page covers the SD side, including why browsing never
  refreshes the list and what each kind of empty folder means.

## [2.13.0]

**The first full release since 2.12.2.** Everything between the two was marked
pre-release, so an ordinary install has been several weeks behind. If you are
coming from 2.12.2, this carries every fix listed under 2.12.3 through 2.13.0b4,
including two that matter more than the rest:

- **Encryption keys are no longer written to the log.** Several messages on the
  camera path printed real key material, and `home-assistant.log` is a file
  people paste into public issue reports.
- **A camera showing nothing could report itself healthy indefinitely**, so the
  session was never given up on and the picture never recovered on its own.

### Fixed

- **A moment's silence from a camera no longer sticks for fifteen minutes.** A
  listing that the camera did not answer was cached exactly as long as a real
  one, and nothing would re-list while that cache was "fresh". A restart that
  caught a camera at a bad moment could leave "The camera did not answer when
  asked what it holds" on the folder of a camera holding a hundred recordings,
  which answered fine seconds later. An unanswered listing now expires far
  sooner, and repeated silence backs off so a camera that genuinely does not
  report its card is not re-asked on every connection.

### Changed

- Requires `python-aidot-cameras >= 1.0.0b7`.

## [2.13.0b4]

See 2.13.0.

## [2.13.0b1]

Pre-release, and on the library's beta line.

### Added

- **Recordings held on a camera's own SD card are now browsable.** Each camera
  in the media browser splits into `Cloud` and `On device`, and the second lists
  what the camera says is on its card, grouped by day in your timezone. This is
  the whole story for anyone running these cameras without internet, and until
  now there was no surface for it at all.

  On-device recordings are listed, not played. Pulling video off the card is a
  protocol nothing here has exercised yet, so these entries are not playable.

- **A "Refresh on-device recordings" button on every camera.** Listing what is
  on a card requires an open connection to the camera - 15 to 21 seconds on a
  mains camera and up to 70 on a battery one, and it wakes the camera - where
  reading the cloud is a fraction of a second. So browsing the folder never
  does it. Instead the list is taken whenever a connection is already open for
  some other reason (a live view, a motion event) and is at least 15 minutes
  old, and this button is there for when you want it now. The folder shows the
  time of the listing it is displaying.

### Fixed

- **An empty `On device` folder now says which kind of empty it is.** A camera
  that answered "nothing on my card" and a camera that did not answer at all
  both produce an empty list, and telling the second group their card is empty
  would be inventing an answer the camera never gave. The four cases - never
  listed, no answer, a partial list, and a genuinely empty card - each say so.

### Changed

- Requires `python-aidot-cameras >= 1.0.0b5` for the on-device listing. The
  browser calls it directly, so an older library would fail at runtime rather
  than at install.

## [2.12.7b1]

Pre-release, and on the library's beta line.

### Fixed

- **The recordings browser showed the newest ten recordings and nothing else.**
  The cloud caps a listing page at 10 items whatever page size is asked for -
  measured across seven cameras - and the browser asked for 30, received 10,
  and rendered that as though it were the whole library. On a busy camera that
  is about 3% of what exists, with nothing on screen to say so.

  Recordings are now grouped by day. Each day folder shows the day's true
  count, obtained in a single request rather than by paging, and opening one
  pages through that day in full. A day with more recordings than the browser
  will fetch says so - "newest 200 of 1517" - because a list that stops without
  saying it stopped reads as the whole day.

- **An empty folder now says why it is empty.** Four different situations
  produce no recordings and a user cannot tell them apart: the account cannot
  see cloud events (they are visible to the home owner's account, not to a
  shared-home member), the recording plan has lapsed, the day's recordings
  could not be loaded, or the day holds nothing playable. Each says which,
  rather than opening onto a blank folder.

- **The plan expiry date is shown in the timezone Home Assistant is configured
  for**, not the host's. A container running in UTC could otherwise show a date
  a day out.

### Changed

- Requires `python-aidot-cameras >= 1.0.0b4` for the count and plan reads the
  day folders are built on. With an older library the browser still works and
  still explains itself; it simply cannot show per-day counts.

## [2.12.6b1]

Pre-release, and on the library's beta line.

### Changed
- Requires `python-aidot-cameras >= 1.0.0b3`. What it brings here:

  - **A camera in the same house as Home Assistant could fail to start
    streaming.** Cameras reach the vendor's relay server from the same public
    address this host does, and the library's check for "is this address me"
    compared the address without the port - so it mistook the camera for the
    host. The reply that completes the camera's connectivity check was then
    never sent, and the camera never began sending video: the live view sat and
    timed out. Most visible on a camera on a separate network at the same site,
    such as an IoT SSID.

    The check now compares address and port, so it still refuses this host's own
    address and no longer refuses the camera.

  - **Two-way audio no longer opens a second camera session.** The live-view
    session is now opened able to carry talk, so pressing to talk reuses it. A
    camera holds a viewer slot for about two minutes after a session ends, so
    the extra session had a cost.

## [2.12.5b1]

Pre-release, and on the library's beta line.

### Changed
- Requires `python-aidot-cameras >= 1.0.0b2`. What it brings here:

  - **The `aidot.talk` service could report success while nothing played.** On
    the SDES cameras (the battery L2 and the pan-tilt spotlight) the library
    returned success from starting two-way audio whenever the camera's
    acknowledgement wait expired - so a talk request could be accepted when the
    speaker command had never been sent at all, and the service call still
    succeeded. It is fixed: the speaker command must actually have been
    dispatched. A second fix, requiring the session to still be running before
    it counts as talk-capable, closes the same class of error on a path that
    nothing currently reaches - stated that way because it was not verified as
    something users had hit.

    Two-way audio now passes on every camera on the test fleet. It had never
    been verified on hardware before; nothing checked it, which is how this
    survived.

## [2.12.4]

Pre-release, and on the library's beta line.

### Changed
- Requires `python-aidot-cameras >= 0.17.3b2`, which closes the audit backlog
  opened on 2026-08-07. What it brings here:

  - **A camera showing nothing could report itself healthy indefinitely.** The
    library counted every packet it forwarded as delivered media, including ones
    that failed decryption and were then discarded downstream. Those counters
    are what the background keepalive reads, so a black picture kept a session
    alive and the give-up ceiling could never fire. Counting is now gated on the
    packet being usable by the time it is read.

  - **A camera that changed its encryption key mid-session went dark for the
    rest of that session.** The component doing the decryption kept the first
    key it saw and had no way to be handed another. It now follows the key the
    rest of the connection negotiates.

  - **Cancelling a snapshot now actually cancels it**, rather than starting
    another ffmpeg and returning normally.

  - **Encryption keys are no longer written to the log.** Several messages on
    the camera path printed real key material, and `home-assistant.log` is a
    file people paste into public issue reports. They now print a fingerprint
    that tells two keys apart without revealing either.

- **Local control: check which account you are signed in as.** It works, but
  only for the account that OWNS the devices. A member of a shared home receives
  a full device list from the cloud - passwords and keys included - and is then
  refused by the device itself, with an error that blames the password rather
  than the account. The README explains this, with the per-model codes.

## [2.12.3]

### Changed
- Requires `python-aidot-cameras >= 0.17.2`. What it brings to this integration:

  - **A device that could not log in over the LAN was retried several times a
    second, for as long as the integration stayed loaded.** The retry had no
    delay and no ceiling, so the period was the device's own round-trip time,
    not the minute the code appeared to ask for. Measured on one 25-minute run:
    15,376 failed logins across six devices, ~7.6 attempts per second for a
    single light, stopping only when the process did. The only visible symptom
    was a very large log, but every one of those attempts was real network
    traffic and real work on the device.

    Retries are now exponential from 1 s, capped at 60 s, and stop after six
    consecutive failures; the device is tried again the next time something
    asks for it. A login that succeeds clears the count, so a device that drops
    occasionally still recovers on the first prompt retry. Measured on the same
    fleet after the fix: 135 failed logins where the same run had produced
    26,229.

    This affects lights and plugs, not cameras - cameras do not use the LAN
    login path at all. It does not make a device that rejects the login start
    working; it stops the hammering.

  - **A device that went silent mid-login held its socket open indefinitely.**
    A device that completed the TCP handshake and then stopped answering left
    the attempt parked with no timeout, which also blocked every later attempt,
    so the device silently stopped being managed. One socket was observed held
    for 21 minutes. Attempts are now bounded at 20 s.

  - **A camera that had gone dormant could answer "connection refused".** When
    the background keepalive gave up on a camera that was delivering no media,
    it left the stream registered in go2rtc against a port with nothing
    listening. Opening that camera failed noisily instead of simply finding
    nothing. It now tears the registration down, as the idle path already did.

## [2.12.2]

### Changed
- Requires `python-aidot-cameras >= 0.17.1`. What it brings to this integration:

  - **A camera's cached decoder parameters could be filled with random bytes,
    and the damage outlived the session.** The library mined the video decoder's
    setup data out of packets without checking they had decrypted. On an install
    without the optional SRTP dependency that is every packet, and the values it
    looks for turn up in random data within seconds. The result was written to
    disk and injected into every later stream, so a decoder was initialised from
    noise - and installing the dependency afterwards did not clear the file.
    Fixed, and the healthy path is unchanged.

  - **A battery camera is no longer woken forever for streams that cannot
    work.** If a camera opens a session but no video ever arrives, the
    background keepalive used to reconnect indefinitely, spending charge each
    time. It now stops after five consecutive attempts. A live view still opens
    a session on demand - the ceiling applies only to the background loop.

  - **Two kinds of "the camera is awake" evidence now count.** The wake gate
    was watching for a message topic one letter different from the one the
    client subscribes to, and ignoring the camera's own wake announcement
    because it identifies itself by a field nothing read.

  - **The stream request now carries two fields the vendor app sends** that the
    SDES half of the fleet was omitting.

  - **The negotiated video profile is recorded once per stream**, so a bitrate
    figure can finally be compared against another one. Nothing recorded it
    before, which is why past measurements on these cameras were not comparable.

### Fixed
- Nothing in the integration itself; this release exists to raise the library
  floor so installs actually receive the above.

## [2.12.1]

### Changed
- Requires `python-aidot-cameras >= 0.17.0`, which carries several fixes that
  are worth having on this integration specifically:

  - **A camera that briefly says it has no free session is retried after 20
    seconds instead of 5 minutes.** Measured: a camera refuses a reopen 2
    seconds after a close and accepts one after 8. The old five-minute wait made
    an ordinary momentary refusal look like a camera that had stopped working,
    and it is the likeliest reason battery cameras appeared to need long rests
    between views.
  - **Two-way audio reports whether the camera actually opened its speaker**,
    and no longer sends microphone audio during the round trip.
  - **Attribute writes** - LED, motion detection, night vision, sensitivity,
    volume, PTZ tracking - **match the camera's acknowledgement to the command
    that sent it.** Home Assistant writes these in bursts when several controls
    move at once, and previously one control could be reported as having landed
    on the evidence of another.

## [2.12.0]

### Fixed
- **The leftover `select.*_resolution` entities are cleaned up on upgrade.**
  Removing the resolution select in 2.11.9 stopped the integration creating it,
  but Home Assistant keeps the registry row for an entity that stops appearing -
  so every install upgrading from 2.11.8 or earlier still shows one unavailable
  `select.<camera>_resolution` per camera, in the UI and in anything that
  referenced it. They are now removed on setup, alongside the pre-migration
  siren and floodlight switches. Only this integration's own rows are touched.

  Automations or dashboards pointing at those entity IDs should be repointed;
  they had already stopped doing anything in 2.11.9.

### Changed
- Requires `python-aidot-cameras >= 0.16.0`.

### Notes
- **Why the resolution select was really removed, corrected.** 2.11.9 said the
  cameras ignore the command. They do not. The library can now read the camera's
  replies, and measured on 2026-08-07 an A000088 acknowledges `SETSTREAMCTRL`
  within 0.03 s and reports the new value back when asked - 5 (MIDDLE) at
  session start, 5 after `sd`, 1 after `hd`.

  What does not change is the video: with the setting verified by read-back
  first and the recording read frame by frame, quality 1 gave 728 frames at
  1280x720 and 2592 bytes per frame, quality 5 gave 651 frames at 1280x720 and
  2682 bytes per frame. Every earlier check had been made in the `sd` direction,
  which sends the value the camera is already on - so those checks only showed
  that setting a camera to its current value changes nothing.

  The removal stands, and now for a reason that has been measured in the
  direction that actually moves the camera.

## [2.11.9]

### Removed
- **The camera resolution (HD/SD) select.** Changing it changed nothing you
  could see. The entity accepted a value, restored it across restarts and
  reported a setting with no effect on the video, and a control that lies
  persistently is worse than no control.

  > **Corrected in 2.12.0.** This entry originally said "the cameras ignore it".
  > They do not: the camera acknowledges the command and reports the new value
  > back when asked. It simply encodes the same video either way. See the
  > 2.12.0 notes - the removal stands, the reason given here did not.

  `async_set_resolution` remains in the library - the command is correct, and a
  future firmware may act on it.

## [2.11.8]

### Fixed
- **Battery cameras could never be served over HLS.** `stream_source()` waited
  up to 30 s for the RTSP publisher to reach go2rtc, but Home Assistant wraps the
  HLS entry point in a 10 s timeout - so any camera whose cold open is longer
  than that failed every attempt. Measured 2026-08-06: both A001513s returned
  "Timeout getting stream source" on every `camera/stream` call while streaming
  perfectly well over WebRTC, where no such timeout applies. Mains cameras passed
  only because startup prewarm keeps them warm; battery cameras are excluded from
  that on purpose, to save battery, so they are always cold here.

  The wait now fits inside HA's timeout (8.5 s, `AIDOT_PUSH_PUBLISHER_WAIT_S`),
  the publisher poll and the readiness fallback share that one budget instead of
  each starting their own, and the session keeps warming in the background.
  Verified against a live cold camera that Home Assistant's stream worker
  recovers when handed a URL early: `camera/stream` returned in 0.2 s and the
  HLS playlist had segments 5 s later.

  The cost is on the WebRTC path, which has no timeout and could previously wait
  the full 30 s: a genuinely cold open may now hand the go2rtc provider a URL
  before the publisher lands, and that provider does not retry - one bad first
  click, after which the camera is warm.

## [2.11.7]

### Fixed
- **Packet loss no longer ends the stream**, via python-aidot-cameras 0.15.8.
  Both ends of a camera path are usually wireless, so RTP arrives with real
  gaps; the serve had no reordering headroom and no bounded wait, so it stalled
  on a missing packet until it died and dropped every viewer. It now tolerates
  the gap instead. Requires library `>=0.15.8`.

## [2.11.6]

### Fixed
- **Phantom packet loss on PTZ and L2 cameras**, via python-aidot-cameras
  0.15.7. The library's TUTK-to-RTP bridge reset its sequence counters on every
  packet, so every packet it synthesized carried sequence number 1 - which
  ffmpeg reads as constant discontinuity and reports as `RTP: missed N packets`
  for loss that never happened. Requires library `>=0.15.7`.

## [2.11.5]

### Fixed
- **Dropped camera packets under load**, via python-aidot-cameras 0.15.6. The
  library's media sockets ran on the OS default receive buffer (208 KB), while a
  PTZ keyframe is 146-190 KB in a single burst - so when several cameras opened
  at once the kernel dropped the tail of a keyframe and the stream stalled.
  Measured on the reference box: 0 overflows on a settled camera, 176 in the two
  minutes after a reload. Requires library `>=0.15.6`, which asks for 4 MB.

## [2.11.4]

### Fixed
- **Camera logs name the camera**, via python-aidot-cameras 0.15.4. The SDES
  serve and bridge lines carried no device id, so on an install with several
  SDES cameras a burst of ffmpeg exits could not be attributed to any one of
  them. Requires library `>=0.15.4`, which adds the id.

## [2.11.3]

### Fixed
- **SDES push live view failed with `DESCRIBE ... 404`** (e.g. the A001064 PTZ),
  via python-aidot-cameras 0.15.3. The camera's serve ffmpeg had its stderr left
  unread, so it filled the pipe buffer and blocked, killing the RTSP publisher;
  go2rtc was then left with no producer and every viewer got a 404. Requires
  library `>=0.15.3`, which drains that stderr so the publisher stays alive.

## [2.11.2]

### Fixed
- **Changing a camera's resolution usually did nothing** (via
  python-aidot-cameras 0.15.2). The setting is delivered over the live streaming
  session, so it could only arrive while the camera happened to be streaming. At
  any other time it was discarded and nothing re-sent it - so for a camera that
  is idle more often than it is watched, the ordinary result of changing
  resolution was no change at all, while this integration went on showing the
  new value as though it had been applied.

  The choice is now remembered and applied when a session next starts, which is
  what makes displaying it here truthful.

## [2.11.1]

### Fixed
- **Requires python-aidot-cameras 0.15.1**, which stops the remembered video
  decoder being read from disk while a camera stream is being set up. Home
  Assistant detected that as a blocking call on its event loop and reported it
  as a stability problem. The reading now happens once in the background, where
  it belongs. Affects 2.11.0 only, and the effect was a few milliseconds per
  stream setup rather than anything visible.

## [2.11.0]

Verified on the live installation before release: every reachable camera
streamed through Home Assistant's own live view, including the one that had
been showing a black picture.

### Added
- **The video decoder each machine can actually use is now worked out at
  startup, in the background, and used where it genuinely helps.** Where a
  machine has video decoding hardware that works, it is used; where it does
  not, software decoding is used exactly as before.

  The choice cannot be read off the decoders ffmpeg reports, because that list
  describes what the program was built with rather than what the machine can do.
  A Raspberry Pi 4 lists decoders for graphics hardware it does not have, and
  one for its own video hardware that fails to start. On Apple machines, and on
  Linux machines using VAAPI, there is no decoder to name at all - so both forms
  are tried, and every candidate has to decode a sample before it is used.

  Hardware decoding is not always faster and is not assumed to be. Candidates
  are ranked by measured speed on the machine itself, and the faster one wins
  whichever it turns out to be.

  Detection never delays startup. It costs a Raspberry Pi 4 about six seconds,
  once; a machine with no video hardware is answered in about three
  milliseconds without starting anything. To override, set
  `AIDOT_VIDEO_DECODER` to a decoder name, or to `hwaccel:` followed by an
  acceleration method, or `AIDOT_DISABLE_HWACCEL=1` to keep to software.

### Fixed
- **A camera could show a permanent black picture while its stream was perfectly
  healthy** (via python-aidot-cameras 0.15.0). The decoder was set up from
  parameter sets remembered from an earlier session, and one camera model
  changes them between sessions - so the decoder was described a stream that no
  longer matched and decoded nothing, while the camera stayed connected and
  megabytes kept arriving. Such a camera is now remembered and the stale copy is
  neither kept nor used.

### Changed
- Requires python-aidot-cameras 0.15.0.

## [2.11.0b4] - pre-release

### Changed
- **Requires python-aidot-cameras 0.15.0b5, a pre-release.** Working out which
  decoder to use no longer costs a small machine much of a minute. On a
  Raspberry Pi 4 it took 54 seconds of wall clock and 26 seconds of processor
  time; it now takes about 6 and 5.

  Hardware is no longer tried when the machine plainly does not have it, H.265
  is no longer worked out when only H.264 is used, and software decoding is no
  longer measured when nothing has beaten it. On a machine with no video
  hardware at all - most container installations - the question is answered in
  about three milliseconds without starting anything.

  This work already ran in the background and never delayed startup; it now
  takes far less processor time away from streaming on machines with little to
  spare.

## [2.11.0b3] - pre-release

### Changed
- **Requires python-aidot-cameras 0.15.0b4, a pre-release.** That library version
  fixes hardware decoding being unreachable on Apple machines and on Linux
  machines using VAAPI, which between them are most desktop installations. The
  detection looked for decoders by name, and on those platforms there is no
  decoder to name - ffmpeg offers the decoding side only as an acceleration
  method - so they quietly used software while having working hardware decoding
  available.

  It also sizes the detection sample to what the cameras actually send. The
  previous sample was small enough that fixed hardware setup cost dominated,
  which could make a decoder that wins on real frames look like a loser.

  Hardware decoding is not always faster and is no longer assumed to be:
  candidates are ranked by measured time on the machine itself, so the faster
  path wins whichever it turns out to be.

  Naming the pre-release in the requirement is what allows it to be installed at
  all; it is not selected by default. This integration release is therefore also
  a pre-release, and HACS only offers it to installations that have explicitly
  enabled betas for this repository.

## [2.11.0b2] - pre-release

### Changed
- **Requires python-aidot-cameras 0.15.0b3, a pre-release.** That library version
  detects the fastest video decoder each machine can actually use, rather than
  assuming one from the list ffmpeg reports - that list describes what the
  program was built with, not what the machine can do. A Raspberry Pi 4 lists
  decoders for graphics hardware it does not have, and lists one for its own
  video hardware that fails to start; each candidate is now required to prove
  itself by decoding a sample first. It also carries a fix for a camera showing a
  permanent black picture while its stream was healthy.

  Detection runs in the background when the integration starts, so it never
  delays startup, and the result is remembered per machine. Where a machine has
  video decoding hardware that genuinely works it is used; where it does not,
  nothing changes.

  Naming the pre-release in the requirement is what allows it to be installed at
  all; it is not selected by default. This integration release is therefore also
  a pre-release, and HACS only offers it to installations that have explicitly
  enabled betas for this repository.

## [2.11.0b1] - pre-release

### Changed
- **Requires python-aidot-cameras 0.15.0b1, a pre-release.** That library version
  reports a camera already serving its maximum viewers in about two seconds
  instead of about twenty-six, and carries fixes for three faults found while
  reviewing that work: a late camera answer could be discarded so the picture
  never appeared, one camera being refused could abort a different camera's
  healthy connection, and an aborted connection could leave a signalling session
  running that later interfered with other cameras on the account.

  Naming the pre-release in the requirement is what allows it to be installed at
  all; it is not selected by default. This integration release is therefore also
  a pre-release, and HACS only offers it to installations that have explicitly
  enabled betas for this repository.

## [2.10.2]

### Changed
- **Documentation now matches measured behaviour.** The README quoted first-view
  times of "15-21 s" for mains cameras and "~70 s" for battery models, and called
  later views "sub-second". Those figures pre-dated several streaming fixes and
  were wrong in both directions. They are replaced with times measured end to end
  through Home Assistant's own live-view signalling on a seven-camera fleet: 1.4-6.7 s
  for mains cameras, 0.9-2.1 s for SDES models, and around 9 s for battery models
  opening from idle.
- The troubleshooting entry for "camera connects but the picture stays blank" no
  longer attributes that symptom to your network first. On versions before 2.10.0
  the usual cause was a bug in this integration, and the entry now says so.

## [2.10.1]

### Fixed
- **One camera failing to open could blank every other camera's live view.**
  When a camera could not be served - go2rtc unreachable, or its session failed
  to start - the integration reported "no stream" to Home Assistant. That reads
  like the gentle option: show the still image and retry. It is not. Home
  Assistant's go2rtc provider responds to "no stream" by tearing down *every*
  live-view session it is holding, for every camera, not just the one that
  failed. So a single unreachable camera took the whole dashboard's video with
  it.

  Such a camera now hands back its stream address anyway. If it genuinely cannot
  be served, that one camera's view fails on its own and every other camera keeps
  playing. The error is still surfaced on the affected camera through its status
  overlay.

## [2.10.0]

### Fixed
- **SDES cameras never completed a live view.** Opening one produced a spinner
  and then nothing, on the first view and on every view after it, while DTLS
  cameras worked normally.

  Each view re-registered the camera's stream with go2rtc. That registration
  call replaces a stream's definition outright, which drops whatever is
  currently publishing to it - and it does so even when the registration is
  identical to what was already there. SDES cameras publish their media into
  go2rtc, so every view tore down the publisher the previous view had
  established, leaving a stream nothing was feeding. The camera-side publisher
  kept running throughout, so from the outside the session looked healthy right
  up until playback failed.

  A view now re-registers only when there is nothing publishing - which is what
  the registration was there to repair in the first place. DTLS cameras are
  unaffected either way: their registered source *is* the live one, so replacing
  it changes nothing.

  Verified against go2rtc 1.9.9 in a two-instance reproduction of the reference
  install: with the old behaviour a second view returned "404 Not Found"; with
  the fix the same sequence delivers 1280x720 H.264 with audio.

- **The first view of a cold SDES camera failed, and only a second attempt
  worked.** Opening a camera that had no session yet gave up after seven seconds
  and handed Home Assistant a stream address that could not work yet, which
  surfaced as a failed live view. The session carried on opening in the
  background, so pressing play again a few seconds later succeeded - the
  "press play twice" behaviour.

  A cold camera needs roughly twenty seconds to negotiate and start sending, and
  the seven-second limit was there to fit a Home Assistant timeout that does not
  apply to the live-view path (it applies only to the scrubber/recording
  pipeline). A live view now waits for the camera's stream to actually reach
  go2rtc before it hands over the address, so the first view shows a spinner that
  resolves into video instead of an error. A camera that is already streaming is
  unaffected and still opens immediately; if the stream never arrives, the
  address is handed over anyway rather than reporting no stream at all, which
  would have closed every *other* camera's live view as a side effect.

  Verified end to end against go2rtc 1.9.9 with a twenty-second cold start: the
  old behaviour failed at seven seconds, the new one plays 1280x720 H.264 with
  audio.

## [2.9.24]

### Fixed
- **Cameras could end up with no stream at all when go2rtc was not reachable
  over the network.** During setup the integration registers each camera's local
  serve with go2rtc and hands Home Assistant the resulting URL; when that
  registration failed it handed back nothing, and Home Assistant concluded the
  camera had no stream - dropping it to HLS-only and then serving nothing.

  That is not an edge case. Registration needs a go2rtc whose API is reachable
  over the network, while Home Assistant's own bundled go2rtc listens on a local
  socket instead - so on an install without a second go2rtc, every camera was
  affected. Measured on the reference fleet: with no reachable go2rtc, all seven
  cameras went dark.

  The camera's local serve URL is now handed to Home Assistant instead. If its
  go2rtc accepts that source it registers it and still serves WebRTC; if not,
  Home Assistant's HLS pipeline reads the same URL. Either way the camera has a
  stream. Behaviour is unchanged whenever go2rtc *is* reachable.

  The exception is SDES push mode, where nothing binds the local serve because
  the media is published into go2rtc instead - there is genuinely no source to
  offer, so that case is untouched.

## [2.9.23]

### Fixed
- **A camera that stopped publishing stayed broken until Home Assistant was
  restarted.** Once a stream session had started, the integration treated it as
  live for good - so when the publisher died underneath it (the SDES bridge's
  ffmpeg exiting), every later view reused that dead session, handed go2rtc a
  stream nobody was publishing to, and the live view failed with nothing in the
  log to explain it. Opening a view now checks with go2rtc whether a publisher is
  actually attached and restarts the session when it is not.

  Battery cameras were hit hardest: they are deliberately excluded from the
  startup warm-up, so nothing else ever gave them a session and they could show
  no video at all. Mains cameras mostly masked the problem, because the startup
  warm-up leaves them running.

  If go2rtc cannot be reached the session is left alone, so an unreachable go2rtc
  cannot tear down a working stream.

- **SDES cameras could serve nothing at all, indefinitely.** Requires
  python-aidot-cameras 0.14.0. The SDP handed to ffmpeg advertises both video
  codecs and must be narrowed to the one the camera actually sends; when a
  session's media started late there was nothing to narrow on, so ffmpeg bound
  the wrong decoder and go2rtc rejected the published stream outright. The codec
  is now taken from the camera's own negotiated answer in that case.

  The floor is a hard `>=0.14.0`: Home Assistant only installs a requirement it
  does not already satisfy, so leaving the old floor would keep 0.13.2 in place
  and the fix inert.

## [2.9.22]

### Fixed
- **Mains cameras get the mains warm-hold again.** Requires
  python-aidot-cameras 0.13.2, which fixes a misclassification where every
  mains A000088 was treated as a battery camera (the cloud reports
  `batteryMode: '2'` on mains cameras too, with none of the corroborating
  battery telemetry). Because this integration picks the stream idle window
  from `is_battery_camera`, those cameras were given the battery window
  instead of the mains "never release" warm hold - so re-views were paying a
  cold start that the warm hold exists to avoid.

  The floor is a hard `>=0.13.2` rather than `>=0.13.1`: Home Assistant only
  installs a requirement it does not already satisfy, so leaving the old floor
  would have left 0.13.1 in place and the fix inert.

## [2.9.21]

### Fixed
- **Battery cameras kept a stream session alive forever, and the first view of
  one would fail.** The integration withheld go2rtc's URL from the library to
  avoid a duplicate stream registration (which re-points the source mid-stream
  and had previously left mains cameras showing nothing). But in SDES push mode
  that URL is the *only* way to tell whether anyone is watching: the serve port
  is go2rtc's shared RTSP port, where every camera's own publisher is connected,
  so the library correctly refuses to guess from it and answers "unknown" -
  and "unknown" never releases.

  A battery camera therefore renewed its keepalive indefinitely against a camera
  nobody was watching - draining it, and leaving the camera parked in a
  permanently-retrying state so that opening its live view landed on a session
  that never delivered a frame. Refreshing appeared to fix it because a retry
  happened to be mid-flight with media.

  The library now supports asking go2rtc who is watching *without* registering
  the stream (`go2rtc_register=False`, requires 0.13.1), so both problems are
  avoided at once: no duplicate registration, and real viewer detection.

## [2.9.20]

### Fixed
- **The log is readable again, and no longer contains device credentials.** The
  underlying device library logs routine chatter at WARNING - every ping, every
  received frame, every command and connect, the full login response, and, on
  each device client's construction, the entire raw device record *including
  that device's password and AES key*. On a normal fleet that is a few lines
  every 30 seconds per device, which buries everything else and writes
  credentials to disk in plaintext.

  Home Assistant's `logger:` option can only set a level per logger, and a level
  is the wrong instrument here: of the library's nine WARNING call sites, exactly
  one is worth keeping - `connect device error`, the only signal that a device
  failed to connect. Turning that logger down to `error` silences the flood by
  throwing away precisely the message that tells you something is wrong.

  So the noise is filtered by message instead. Real failures are untouched,
  `connect device error` still comes through, and anything unrecognised is
  always emitted - if the library rewords a line, the worst case is that some
  noise returns, never that a new failure is swallowed. Set
  `aidot.device_client: debug` to bring the protocol chatter back when actually
  debugging. The credential dump is dropped at every level, including debug.

  If you added a `logger:` override to work around this, remove it - an override
  of `error` will still hide `connect device error` and defeat the fix.

## [2.9.19]

### Fixed
- **Devices no longer read as permanently disconnected on newer installs.** The
  LAN-session check compared a private attribute against an enum that exists
  only in one version of the underlying device library. On the newer version -
  the one Home Assistant core's own AiDot integration requires - neither the
  attribute nor the enum is there, so the check answered "not connected"
  forever and every device looked permanently offline. Nothing raised an error,
  which is what made it easy to miss; the cost was a pointless reconnect
  attempt on every poll. The check now asks the library, which knows which
  signal the installed version actually maintains. Lights were never affected -
  their availability deliberately does not depend on this check.

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.13.0`.** Required
  by the fix above. That release also brings two things worth knowing about:

  A camera that advertises an address Home Assistant cannot reach now works.
  Previously the only address ever offered to the camera was the one it
  advertised, so a camera on a different subnet negotiated a session and then
  showed nothing at all. It now also uses the address the camera's own
  connectivity checks arrive from. Cameras reachable at the address they
  advertise behave exactly as before.

  The dependency on the underlying device library widens from an exact version
  to a range. That is what lets this integration be installed alongside Home
  Assistant core's own AiDot integration, which requires a version the old
  exact pin ruled out.

## [2.9.18]

### Changed
- **Library floor raised to `python-aidot-cameras[webrtc]>=0.12.17`.** That
  release fixes SDES cameras negotiating a session and then delivering zero
  bytes: the camera's `webrtcResp` answer was parsed before it had arrived, so
  no ICE credentials were read, nothing was ever nominated, and the camera - a
  controlled ICE agent - stayed in "Checking" and never sent SRTP. Every
  A001513 and A001064 was affected; DTLS cameras were not, which is why only
  part of a fleet appeared broken. 0.12.17 also moves the battery-hostile
  options out of caller-visible settings and into the library (Home Assistant
  surfaces those options, and one of them re-broke every battery camera), and
  makes the TURN relay a working fallback for a camera the host cannot reach
  directly.

  A battery camera that will not wake still returns no media; nothing in this
  release wakes a sleeping camera.

## [2.9.17]

### Fixed
- **Battery cameras (the L2 / A001513) reconnect far more reliably.** Requires
  library 0.12.16. The library used to register a brand-new camera-side session
  on every reconnect attempt and never release the old ones, so a battery camera
  that had to retry could pile up sessions faster than it freed them and stop
  serving video entirely until it timed out or was power-cycled. It now reuses a
  single session across retries and backs off when the camera reports it has no
  free session, instead of hammering it. Measured on an L2: two back-to-back
  live views now both play, where the second in a row used to come up blank.

  (A rapid third view in quick succession can still occasionally come up blank on
  a battery camera - a separate wake-timing issue being worked on next; normal
  use, with time between views, is unaffected.)

## [2.9.16]

### Fixed
- **Battery cameras (the L2 / A001513) show live video again.** Requires library
  0.12.15. The library made an AWS-KVS pre-connect for battery cameras only,
  which provisioned the camera's session toward KVS - its media went there
  instead of to the integration, so the session connected, the serve started,
  and the picture stayed blank forever. That call is now off by default.
  Measured on an L2: h264 1280x960 with audio, where before there was no media
  at all.

  0.12.15 also stops a battery camera from skipping the TURN relay: these
  cameras sleep and are woken through the cloud and have no LAN address, so the
  relay is their only route back - which means **LAN-direct** connection mode no
  longer breaks them. Two SDES serve bugs are fixed alongside it (a payload-type
  narrowing that quietly stopped working when the camera offered a third video
  codec, and an RTSP push that failed outright when the camera's audio type
  could not be determined).

## [2.9.15]

### Fixed
- **Docs and option text caught up with current behavior.** The Configure dialog
  still described pre-2.9.12 defaults: warm-hold "default 300s" (it is now 0 =
  always warm), startup prewarm "off by default" (it is on), a fixed
  concurrent-stream cap of 3 (it now sizes itself to the camera count).
  The README now also notes the blank-mains-picture fix in 2.9.14 and that the
  integration serves mains streams directly (`AIDOT_SERVE_RELAY=0`). Wiki pages
  updated to match.

## [2.9.14]

### Fixed
- **Require library 0.12.14 - DTLS live video works again.** The library's
  serve-wait loop ended its viewer check with an unconditional `break`, so any
  positive idle window tore down a healthy ffmpeg serve after a single half-second
  tick and respawned it forever. Every DTLS camera went dark (go2rtc saw 404s or
  unreadable data) while `mains_idle_s = 0` configurations were untouched - which
  is why the never-release default of 2.9.12 masked it and a custom idle window
  re-armed it. 0.12.14 also stops missing audio from holding video hostage in the
  serve mux, and stops an abandoned mux thread from starving its replacement.

  With the teardown fixed, idle release now genuinely works: an unwatched camera
  goes dormant after its idle window and a cold wake reaches first HLS parts in
  about 16 seconds (validated live).

### Changed
- **The serve-relay is disabled by default.** Its accept thread was observed dead
  on HA 2026.7 with no recovery while a camera held its warm session, leaving the
  public serve port unconnectable. ffmpeg now serves its port directly; set
  `AIDOT_SERVE_RELAY=1` to re-enable the relay for testing.

## [2.9.13]

### Fixed
- **DTLS cameras produced nothing at all.** 2.9.8 began passing `go2rtc_url` to the
  library so it could ask go2rtc who was watching. That also switched on the
  library's own go2rtc registration, which PUTs the same stream this integration
  already registers - a duplicate registration that re-points the source while a
  stream is mid-flight. On this fleet the three DTLS cameras then served nothing,
  through both go2rtc and Home Assistant's own stream, while the build without it
  served all four.

  It is no longer passed. Its only purpose was the viewer check, which no longer
  matters: mains cameras never release (`mains_idle_s` defaults to 0) and battery
  cameras fall back to the socket check. A note in the source records why, so it is
  not re-added without first making the library skip registration when the consumer
  already registers.

## [2.9.12]

### Fixed
- **Live views are instant again, without cameras streaming around the clock.**
  The blackout that led to the 2.9.11 rollback was not a media fault. Before the
  idle-release work, the "is anyone watching?" check could never answer no - it
  watched a shared port that always had a peer - so every camera streamed 24/7 and
  video was always instantly there. Fixing that made cameras correctly go dormant,
  and a cold wake on these measures 16-22 seconds, which reads as a broken view.

  Mains cameras now stay warm (`mains_idle_s` defaults to 0 = never release) and
  are pre-warmed at startup again. Battery cameras still go dormant, which is where
  dormancy actually saves something. Both remain configurable.

- Requires library 0.12.13, which brings everything found while chasing this: the
  go2rtc request storm that blacked out all cameras, the self-referential go2rtc
  registration, idle-release for DTLS cameras, the SRTP key-restart re-introducing
  two earlier media bugs, three lifecycle leaks, dead go2rtc streams left behind by
  a dormant camera, and a normal consumer disconnect being logged as a failure.

## [2.9.10]

### Fixed
- **All cameras went black.** Library 0.12.10 put a go2rtc viewer check inside a
  watchdog that ticks twice a second, opening a fresh HTTP session each time - a
  request storm aimed at the service that also has to serve the video, which
  stopped go2rtc answering at all. Cameras still reported as streaming while
  nothing could be pulled from them. Requires library 0.12.12, which caches that
  answer. Nothing to change in your settings.

## [2.9.9]

### Fixed
- **A camera could show a connected stream with no picture.** 2.9.8 began passing
  `go2rtc_url` to the library so the viewer-aware idle check could work - which
  also switched on the library's own go2rtc registration, and in push mode the URL
  it registered was that stream's own address. go2rtc became its own producer: the
  stream listed a producer, nothing fed it, and consumers got a connection with no
  media. Seen on the Winees Spotlight, whose stream had two producers, one of them
  itself. Requires library 0.12.11, which refuses to register a source that would
  loop. Nothing to change in your settings.

## [2.9.8]

### Fixed
- **Cameras streamed when nobody had opened one.** Three separate causes, found by
  auditing after exactly that report:
  - Startup pre-warm opened a session on *every* mains camera at every restart and
    reload, with no viewer present. It is now **off by default** (a new "Warm
    camera sessions at startup" option turns it back on) and it skips a camera the
    cloud says is offline.
  - The integration never passed `go2rtc_url` to the library, so the viewer-aware
    idle check added in library 0.12.9 was unreachable here and fell back to a
    socket check that is wrong in push mode. Both stream-start paths now pass it.
  - Motion pre-warm could start a stream on an offline camera, driven by a cloud
    poll rather than anything a user did.
- **An offline camera kept streaming.** Going offline only made the entities
  unavailable, which is presentation; the keepalive stayed latched on, keeping a
  renew POST every 20s and a wake probe every 10 minutes at a camera nobody can
  view. Seen live on a battery camera already down to 5%. A camera that goes
  offline while streaming is now stopped.
- Requires library 0.12.10, which fixes idle-release for DTLS cameras (0.12.9 only
  covered the SDES path), the SRTP key-restart re-introducing two earlier media
  bugs, and three lifecycle leaks.

## [2.9.7]

### Fixed
- **All cameras kept streaming after you viewed one.** The library decided whether
  a camera was idle by checking for a TCP client on its serve port - but go2rtc
  attaches there as the producer and never lets go, so every camera looked
  permanently watched and none went dormant. Each one held a concurrency slot, kept
  decrypting, and drained the battery models. Measured here: five cameras, nobody
  watching, still producing 7 minutes into a 5 minute idle window. Requires library
  0.12.9, which asks go2rtc how many viewers a stream actually has.

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
  start. The guidance now uses `provider: ha` -- the native Home Assistant path
  this integration already wires to go2rtc WebRTC -- and adds a static `16:9`
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
  was gated only by the unguessable event id -- replayable indefinitely. The
  media source now mints a URL signed with an HMAC over `device + event +
  expiry` (per-process secret, never persisted), verified in the view with a
  constant-time compare and a 6 h expiry.

### Changed
- **`iot_class`** corrected from `cloud_polling` to `cloud_push` (the
  integration receives MQTT signaling and cloud motion-event push), and
  **`loggers: ["aidot"]`** added so HA can surface the library logger in the UI.
- **Coordinator background tasks** (LAN-control attach, stop-streaming /
  stop-motion on device removal, per-coordinator init) moved from
  `hass.async_create_task` to `config_entry.async_create_background_task` -- now
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
