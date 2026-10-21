# AiDot Cameras for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/cbrightly/hass-aidot-cameras)](https://github.com/cbrightly/hass-aidot-cameras/releases)
[![License: MIT](https://img.shields.io/github/license/cbrightly/hass-aidot-cameras)](LICENSE)

A Home Assistant custom integration for **AiDot / Leedarson** Wi-Fi lights **and
cameras** -- live WebRTC video, two-way audio, PTZ, motion events, and the usual
light controls. It is a camera-capable fork of the upstream lights-only
[AiDot-Development-Team/hass-AiDot](https://github.com/AiDot-Development-Team/hass-AiDot).

The integration is the Home Assistant front-end only; all device communication
lives in the [`python-aidot-cameras`](https://github.com/cbrightly/python-aidot-cameras)
library, which it installs automatically.

<!--
Hero screenshot slot -- add a PNG at docs/hero.png in this repo, then replace this
whole comment with the line below (it publishes with the repo and renders here):
<p align="center"><img src="docs/hero.png" alt="AiDot cameras on a Home Assistant dashboard" width="760"></p>
-->

## How it fits together

```
  camera ==SRTP over the LAN==> python-aidot-cameras ==RTSP push==> go2rtc ==> WebRTC card, rtsp:// for Frigate / NVR
                                (inside Home Assistant)
   ^  cloud MQTT brokers the        |
   |  handshake; the media          +--MPEG-TS on loopback--> Home Assistant's HLS dialog and camera.record
   |  never leaves the LAN          |                        (sound and picture on one clock)
  AiDot cloud  <--poll--------------+--event list----------> motion / person events, notifications, Media browser clips
```

The library opens one session per camera and everything else reads from it:
go2rtc for live views and the RTSP URL, the loopback stream for HLS and
recordings, the cloud event list for motion. A second consumer never costs a
second camera session.

## Features

- **Lights** -- on/off, brightness, colour (RGBW) and colour temperature.
- **Cameras**
  - Live **WebRTC** streaming (via go2rtc) and snapshots, LAN-direct when the
    camera is on the same network.
  - **Motion / person events** (`event` entity) for automations.
  - **Motion push notifications** -- choose cameras, motion or person-only, and
    which notify services to send to. Each notification shows the camera's
    picture of what triggered it, and tapping it opens the clip (on an iPhone,
    in Safari; with the experimental "Open the clip inside the Home Assistant
    app" option, on a page in the app).
    The targets, wording and cooldown are set on the **Motion notifications**
    options page:

    <p align="center"><img src="docs/images/options-notifications.png" alt="The Motion notifications options page" width="520"></p>

    This is an **independent notification path** from the AiDot app's own
    notifications: Home Assistant produces these by polling the camera's cloud
    event list, the app receives its own by a separate push channel, and
    neither turns the other on or off. That is unlike almost every other
    control here, which mirrors a setting stored on the device and so changes
    on both surfaces at once.
  - **Recordings in the Media browser** -- cloud clips (with a plan) and a
    listing of what each camera holds on its **own SD card**, grouped by day.
  - **Two-way audio** -- play a media clip or URL through the camera speaker.
  - **PTZ** on supported models, limited to the directions the camera itself
    advertises.
  - **Detection types** -- human, vehicle, package and pet, each its own
    switch. The cameras have always typed their detections; this surfaces it.
  - **Sound detection** -- glass breaking, smoke alarm, baby crying, dog
    barking. Each detector a camera reports becomes a switch. Mains cameras
    only ([why](https://github.com/cbrightly/hass-aidot-cameras/wiki/Known-limitations)).
  - **Automatic siren** -- whether the camera sounds its own siren, and whether
    on motion or only on a person. Separate from the siren control, which
    sounds it now.
  - **Light when someone appears** -- the camera's own floodlight automation,
    plus how long the light stays on, how bright it comes up, and whether it
    comes on constant or flashing. Each appears only where it actually works,
    which is not the same set of cameras for all of them.
  - **Controls** -- motion detection, status LED, microphone, floodlight and
    its automation, siren, auto-tracking, night vision, motion sensitivity,
    speaker volume, timestamp overlay, HDR, and voice prompts.
  - **Diagnostics** (disabled by default) -- battery, SD-card status, Wi-Fi
    signal and network name, SD total/used. The SD figures deliberately carry
    no unit: the camera reports bare numbers and does not say what they are.
    From **2.36.0**, any device the integration does not claim (a hub, a
    plug) is also listed here, under `unclaimed`, with its model, type and
    why.

Controls are only exposed where the value can be read back from the device,
because this firmware acknowledges writes it then ignores.

## Installation (HACS)

> **Note - this replaces the core AiDot integration.** Home Assistant ships a
> built-in `aidot` integration that is lights-only. This project claims the same
> `aidot` domain to add full **camera + light** support, so it overrides the core
> one. That override is why it installs as a HACS **custom repository** (below)
> rather than from the HACS default store, where core already owns `aidot`.

1. In HACS -> (menu) -> **Custom repositories**, add
   `https://github.com/cbrightly/hass-aidot-cameras` with category **Integration**.
2. Search for **AiDot**, **Download** it, then restart Home Assistant.
3. **Settings -> Devices & Services -> Add Integration -> AiDot Cameras**, and sign in with
   your AiDot account.

> Camera streaming needs **ffmpeg** and (for low-latency browser playback)
> **go2rtc** -- both ship with Home Assistant OS / Container, and go2rtc is
> bundled with Home Assistant 2026. Without go2rtc the integration falls back to
> higher-latency HLS.

Full steps and prerequisites:
**[Installation](https://github.com/cbrightly/hass-aidot-cameras/wiki/Installation)**.

## Quick start: a fast live view

Two things decide whether cameras feel fast:

1. **Use a WebRTC dashboard card, not the default Picture / Picture Glance
   card.** The Picture card plays through Home Assistant's HLS dialog and its
   ~20 s scrubber buffer, so the first frame is seconds away on *every* view.
   That dialog has the camera's audio through the "Audio in HLS and
   recordings" option (on by default) - see [Audio](#audio) below - but a
   WebRTC card is still what you want for speed.
   See [Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards).
2. **A camera that has been idle takes a moment on its first view** while the
   connection handshake runs. After that go2rtc WebRTC takes over and later
   views are quick.

So a slow *first* frame is expected; a slow *every* frame almost always means
the wrong card.

How long the camera takes to start sending video on a **cold** open, measured
from the integration's own log on a live install (the middle 80% of about 300
cold opens, 2026-09-15 to 16):

| camera | cold open |
|---|---|
| mains, standard (DTLS) models | **~2.4 - 3.3 s** |
| mains, SDES models (PTZ / spotlight) | **~2.3 - 2.8 s** |
| battery (L2) models | **~3 - 6 s**, dominated by the camera waking |

A **warm** view starts immediately, because the session is already open. What
you see on the dashboard adds Home Assistant's own stream pipeline on top of the
figures above, and that depends on your card (see point 1).

Battery cameras are deliberately never held warm -- a held session drains them --
so they pay the handshake on every view, and nearly all of it is the camera
waking. That wake is variable, so an occasional first view takes noticeably
longer; the view waits it out rather than failing, and gives up after about a
minute if the camera really is unreachable.

Nothing wakes a battery camera just to poll it: dashboard thumbnails come from
the cloud, the periodic sensor refresh skips them, and they are excluded from
the start-up warm-up mains cameras get and from the warm-up on a motion event.
A battery camera streams only while someone is viewing it, and stops within
about half a minute of the last viewer leaving (plus Home Assistant's own 30 s
for the HLS dialog).

Mains cameras are held warm by default so their views stay quick. If one keeps
reconnecting while idle, it is a model that stops sending when unwatched and
cannot be held warm -- give it a positive **warm-hold** window instead. See
[Configuration options](https://github.com/cbrightly/hass-aidot-cameras/wiki/Configuration-options).

### Direct publish into go2rtc

**Settings -> Devices & Services -> AiDot Cameras -> Configure -> Audio and
recordings -> Direct publish into go2rtc** (on by default since 2.35.0; turn it
off to go back to an ffmpeg process per camera). Each camera's
video and audio go straight into go2rtc from the integration's own process -
no ffmpeg process per camera, and WebRTC gets the camera's audio untranscoded -
and the standard (DTLS) models publish the same way the SDES models already do
instead of being served on a local port. In side-by-side tests on seven
cameras the stream reached go2rtc sooner on every one, most of all on the
standard models (about 7 s -> under 2 s), with no packets lost.

Cameras that stream H.265 keep the previous path for those sessions, so this
affects H.264 sessions only. Where the cameras publish depends on which go2rtc
is there:

- An installation whose go2rtc on port 1984 answers (the WebRTC add-on or
  integration) publishes into it.
- `AIDOT_GO2RTC_API` names any other server (see Environment overrides).
- Nothing is published into Home Assistant's bundled go2rtc. Standard (DTLS)
  cameras keep their local serve, which that go2rtc pulls; SDES cameras have
  no such path, get no live view there, and open no session (from 2.37.3; a
  warning in the log says so once). 2.37.0 and 2.37.1 did publish into
  it, and while an AiDot camera published there, Home Assistant failed every
  camera's WebRTC offer (`MissingField: url` in the log); 2.37.2 stopped
  that, and that publish path has since been removed.

> **On Home Assistant OS or Container with no go2rtc add-on installed** -
> the most common setup, with only the bundled go2rtc above - every SDES
> camera (the battery and PTZ models) has no live view at all; standard
> cameras still stream, pulled as above. From the next release
> **Settings -> Repairs** says so once this happens. The fix is one of:
> install a go2rtc add-on that answers on `127.0.0.1:1984` - the **WebRTC
> Camera** integration or the **go2rtc** add-on - and restart; or, on **Home
> Assistant Core**, set `AIDOT_GO2RTC_API` to a go2rtc you run yourself and
> restart.

> **When Home Assistant's `go2rtc:` `url:` points at the go2rtc the cameras
> publish into** (and that go2rtc is 1.9.13 or newer: the go2rtc add-on,
> Frigate's, or a standalone one), Home Assistant cannot read a stream a camera
> publishes - its go2rtc client fails on the publisher, which has no source URL,
> and it reads the whole stream list on every WebRTC offer, so every camera's
> live view would fail. From 2.37.5 the integration detects this and does not
> publish there: standard cameras are pulled instead (without the in-sync HLS
> audio and the AAC track), SDES cameras (the battery and PTZ models) are not
> streamed, and **Settings -> Repairs** says so. Before 2.37.5, every camera's
> live view failed while an AiDot camera published there (a `go2rtc_client`
> traceback with `Field "url"` / `Producer`). The WebRTC Camera integration's
> own go2rtc (1.9.9) is older than Home Assistant accepts, so pointing `url:` at
> it makes Home Assistant fall back to HLS for every camera instead.
>
> **To get everything back on Home Assistant OS or Container,** remove the
> `url:` line from the `go2rtc:` section of `configuration.yaml` and restart:
> Home Assistant then uses its built-in go2rtc and pulls each AiDot camera from
> the other server, which is how the reference installation runs. **On Home
> Assistant Core**, where `url:` is required, run a second go2rtc for the
> cameras to publish into and point `AIDOT_GO2RTC_API` at it.

## Audio

**Live WebRTC views have sound. Home Assistant's built-in HLS dialog - the one
the Picture and Picture Glance cards open - and `camera.record` recordings have
it too, through the "Audio in HLS and recordings" option, which is on by
default.**

<p align="center"><img src="docs/images/options-audio.png" alt="The Audio and recordings options page" width="520"></p>

The audio and recording settings are on their own options page, **Audio and
recordings** (Settings > Devices & services > AiDot > Configure).

That dialog only accepts AAC, and the cameras send G.711. go2rtc used to
transcode a copy of the camera's audio for it. That copy arrived with
stretched timestamps: a 15 second recording claimed to be 163 seconds, and
because players sync to audio, the dialog played in slow motion on every camera
and every model. Dropping the copy fixed the speed in 2.30.4 and left the
dialog silent.

With the "Audio in HLS and recordings" option on (Settings -> Devices &
Services -> AiDot Cameras -> Configure; on by default), the camera library also publishes its own AAC
track next to the camera's audio, encoded in-process with timestamps that
follow the camera's own audio clock, and the integration asks go2rtc for that
track on the HLS path only. It applies to cameras that publish directly: every
SDES camera, and standard cameras with "Direct publish" on. WebRTC views still
get the camera's own G.711 audio, untranscoded. It costs about 2-3% of one CPU
core per streaming camera.

When a view or recording starts a camera that was idle, the camera first
sends a buffered backlog of video; from python-aidot-cameras 1.0.0rc33 the
AAC track starts in step with that backlog, so the sound lines up with the
picture from the first seconds (measured within about 0.1 s; before, it led by
0.8-2.5 s depending on the model). Turn the option off to save the encode or
if a camera's HLS audio misbehaves; the dialog is then silent, at the right
speed, as it is on a camera whose audio is mu-law rather than A-law.

A recording or HLS view that starts while the camera is already streaming
goes through go2rtc, which lines its tracks up per viewer, so its sound can
trail the picture by up to about 0.75 s, by a different amount each time. The
**"In-sync audio for recordings"** option (on by default since 2.35.0; needs
"Direct publish into go2rtc" and "Audio in HLS and recordings" on) has the HLS
dialog and recordings read the camera's stream straight from this integration
instead, with sound and picture on one clock: measured on M3 Pro cameras,
recordings that joined a running stream had exactly the timing of ones that
started the camera, and claps landed within 0.02-0.08 s of the picture; on the
PTZ camera, 16 pan steps across a recording that started it and one that
joined it landed within -0.03 to +0.08 s. SDES cameras (the battery and PTZ
models) take part too: turning the option on also pins their video to H.264,
as "Pin camera video to H.264" does, because a session that answers in H.265
cannot feed this stream. The stream is served only on the Home Assistant host,
with a secret made fresh for each start, which Home Assistant masks in its
logs. Requires python-aidot-cameras 1.0.0rc40.

Two-way audio (talking *to* the camera) is unaffected and works regardless of
the card.

## Your cameras are reachable through go2rtc

This is how the integration gets you a fast live view, and it is worth knowing
about rather than discovering.

**The cameras themselves do not speak RTSP.** There is no
`rtsp://user:pass@camera/stream` for them: they hand out media over WebRTC
after a cloud handshake, which is why this integration exists. What you can
point other software at is go2rtc, which re-serves what the integration
decrypts. To do that, each camera is registered as a go2rtc stream named
`aidot_` plus the first twelve characters of its device id:

```
rtsp://<the host running go2rtc>:8554/aidot_<id12>
```

Read the exact list from go2rtc's own `/api/streams`.

That is useful. Point Frigate, an NVR, or VLC at it and you get the camera
without a second cloud connection and without a second slot on the camera,
which only serves one live viewer at a time - everything pulling from go2rtc
shares the single session the integration already holds.

### It has no password until you give it one

By default go2rtc serves that URL to anyone who can reach it. Measured on a
live install: from a different machine on the same LAN, with no credentials,
`/api/streams` answered and every camera pulled.

go2rtc can require credentials, and then the URL takes the form you would
expect. In `go2rtc.yaml`:

```yaml
rtsp:
  listen: ":8554"
  username: "someone"
  password: "something-long"
api:
  listen: ":1984"
  username: "someone"
  password: "something-long"
```

```
rtsp://someone:something-long@<host>:8554/aidot_<id12>
```

Verified against go2rtc 1.9.14, the version Home Assistant ships: with that
set, a request from another machine with no credentials or the wrong ones is
refused with `401`, and the right ones play.

**One trap worth knowing:** go2rtc exempts loopback. Tested from the machine
go2rtc runs on, an unauthenticated request still succeeds, so a check there
will look wide open whether or not you have configured a password. Test from a
different machine.

### The rest of the exposure

- **How far it reaches is go2rtc's configuration, not this integration's.**
  go2rtc binds its RTSP port on all interfaces unless told otherwise. If you do
  not want credentials, bind it to loopback or firewall the port instead.
- **A stream carries media only while the camera has a live session.** With the
  default mains warm-hold (`0`, always warm) a mains camera holds one
  continuously, so its stream is pullable around the clock. With a positive
  warm-hold, it carries media while someone is watching and for the warm-hold
  window after; a battery camera carries media while someone is watching and
  stops within about half a minute of the last viewer leaving (plus Home
  Assistant's own 30 s for the HLS dialog).
- **The integration's own local serve is loopback only** (`127.0.0.1`), and the
  library warns if decrypted media is ever served on a non-loopback address.
  go2rtc is the part that reaches the network.

None of this is new in a recent release - registering the camera with go2rtc is
how WebRTC has always worked here. It had simply never been written down.

## Documentation

The **[Wiki](https://github.com/cbrightly/hass-aidot-cameras/wiki)** is the full reference:

- **Getting started** --
  [Installation](https://github.com/cbrightly/hass-aidot-cameras/wiki/Installation) |
  [Configuration options](https://github.com/cbrightly/hass-aidot-cameras/wiki/Configuration-options) |
  [Supported devices](https://github.com/cbrightly/hass-aidot-cameras/wiki/Supported-devices)
- **Cameras** --
  [Overview](https://github.com/cbrightly/hass-aidot-cameras/wiki/Cameras) |
  [Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards) |
  [PTZ control](https://github.com/cbrightly/hass-aidot-cameras/wiki/PTZ-control) |
  [Two-way audio](https://github.com/cbrightly/hass-aidot-cameras/wiki/Two-way-audio) |
  [Cloud recordings](https://github.com/cbrightly/hass-aidot-cameras/wiki/Cloud-recordings) |
  [On-device recordings](https://github.com/cbrightly/hass-aidot-cameras/wiki/On-device-recordings)
- **Using it** --
  [Automation examples](https://github.com/cbrightly/hass-aidot-cameras/wiki/Automation-examples) |
  [Services reference](https://github.com/cbrightly/hass-aidot-cameras/wiki/Services)
- **Help** --
  [Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting) |
  [Known limitations](https://github.com/cbrightly/hass-aidot-cameras/wiki/Known-limitations) |
  [FAQ](https://github.com/cbrightly/hass-aidot-cameras/wiki/FAQ)

## Troubleshooting

The full list lives in the **[Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting)** wiki page.
The ones that come up most:

- **A battery camera's first view after a long sleep shows nothing, then works
  on the next try.** Expected, not a fault: a deeply asleep camera sometimes
  acknowledges the request and never starts the stream. The integration
  abandons that attempt and retries - after about 20 s if the camera has gone
  completely quiet - and the second one streams normally. A
  battery camera failing *repeatedly* is worth a bug report; a single dud
  attempt after a quiet period is the camera.

- **Every camera's live view fails, and the log shows `Field "url"` /
  `Producer` from `go2rtc_client`; or, from 2.37.5, a repair says Home
  Assistant's go2rtc reads the server the cameras publish into.** Your
  `go2rtc:` configuration points `url:` at the go2rtc the cameras publish into;
  see [Direct publish into go2rtc](#direct-publish-into-go2rtc) for the cause
  and how to get every camera back.

- **Live view buffers like HLS instead of being near-instant.** Use a WebRTC
  card (see [Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards)). Check that a go2rtc is reachable -- the WebRTC
  add-on/integration, or Home Assistant's own bundled go2rtc for standard
  cameras (see [Direct publish into go2rtc](#direct-publish-into-go2rtc)
  above). With neither, every view falls back to Home Assistant's HLS
  pipeline.

- **Live view plays in slow motion, roughly a tenth of real speed.** Fixed in
  **2.30.4** - update. The Picture card's HLS dialog was being served an AAC
  copy of the camera audio whose timestamps were stretched about elevenfold,
  and players sync to audio, so the video crawled to match. It affected every
  camera and every model. That dialog now plays at the right speed; for sound
  in it, see the HLS audio option under [Audio](#audio).

- **A camera connects but the picture stays blank.** Usually the camera's media
  is not reaching Home Assistant - most often because it is isolated on the
  network (a separate VLAN, or AP/client isolation). If it shares the Home
  Assistant LAN, set **Connection mode** to **LAN only**. See
  [Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting#camera-connects-but-the-picture-stays-blank-no-video).

- **Every live view has dropped to HLS at once, and the log shows
  `go2rtc: add stream 'aidot_...' failed http=400` for every camera.** go2rtc's
  own configuration file (`go2rtc.yaml`, the WebRTC add-on's or integration's)
  holds a stream listed twice, which makes that go2rtc (1.9.9) reject every
  registration until the duplicate is removed by hand. From 2.34.3 this
  integration never writes two registrations at once, which is what tore the
  file, and from 2.34.4 a **repair issue** in Settings names the condition. To
  fix an existing file: stop go2rtc, delete the second copy of the duplicated
  `aidot_...:` block in `go2rtc.yaml`, start go2rtc. The warning's text from
  library 1.0.0rc41 on quotes go2rtc's reason (`mapping key ... already
  defined`).

- **The PTZ camera's HLS view or recording is blank with "In-sync audio for
  recordings" on, while the live view works.** The camera answered in H.265
  despite the H.264 pin (it does so in roughly one open in seven), and only an
  H.264 session can feed the in-sync stream. From library 1.0.0rc42 such a
  session is abandoned and re-opened, up to twice; the log line is `sent video
  payload type 97 against an offer pinned to 96 ... re-opening`. If it still
  happens, the camera insisted three times in a row and the session was served
  as it came; the next one is normally fine.

- **A light cannot be controlled locally and nothing explains why.** If the
  light's address (found by discovery, or the cloud's last-known one) is
  outside every network Home Assistant is on (a different subnet or VLAN),
  local login cannot reach it. From **2.36.0**, once that has failed enough
  times in a row, **Settings -> Repairs** names the light and that address.
  The issue clears once a local login gets through or the light is removed. A
  light that is simply switched off, last seen on another subnet, can show it
  too.

**Always update before troubleshooting.** A large share of past reports were
bugs already fixed in a later release; the [CHANGELOG](CHANGELOG.md) records
which.

### Reading the health line

From python-aidot-cameras 1.0.0rc51 (integration 2.37.9) every live session
logs one INFO line a minute under `aidot_cameras.camera.health`, so a pasted
log can say which of three things happened without a second look:

```
camera <id>: health 20s: packets=1686 (84.1/s) lost=0 (0.0%) stall=0.0s path=direct viewers=present
```

- **The link is lossy**: `lost` climbs (a DTLS camera; the SDES models recover
  loss with retransmission and report `bytes` instead).
- **The picture froze**: `stall` climbs while `lost` stays at 0. The camera
  stopped sending; a DTLS session with no video for 30 s is re-opened on its
  own, and an SDES camera gets a nudge first.
- **Nobody was watching**: `viewers=absent` for longer than the idle window,
  after which the session is released. `unknown` means the go2rtc the camera
  publishes into could not be asked.

From library 1.0.0rc58 a DTLS camera's line also ends with frame figures:
`frames` is the pictures a viewer was served (with the rate), `resent` the
frames the camera sent twice and every consumer dropped, `keyframes` the
keyframes in the period, and `max_keyframe_gap` the longest run of pictures
between keyframes since the session started. SDES cameras carry no frame
figures.

`path=relay` means the media is riding the vendor's relay rather than your
LAN, which is normal for a camera on another subnet and worth a look for one
that is not. The counts are the library's own view of the stream, before
go2rtc, so they tell you whether media was arriving even when a dashboard
shows nothing. The interval is `AIDOT_HEALTH_LINE_S`; `0` turns the line off.

### Environment overrides

Rarely needed, and off the supported path:

| Variable | Effect |
| --- | --- |
| `AIDOT_VIDEO_DECODER` | Force a decoder by name, or `hwaccel:<method>` (e.g. `hwaccel:videotoolbox`). |
| `AIDOT_DISABLE_HWACCEL=1` | Keep to software decoding. |
| `AIDOT_SERVE_RELAY=1` | Re-enable the library's serve relay. Testing only. |
| `AIDOT_GO2RTC_API` | The API URL of a go2rtc to publish into (e.g. `http://192.168.1.5:1984`), used instead of the one on port 1984. Home Assistant's bundled go2rtc is never published into. |
| `AIDOT_GO2RTC_RTSP_PORT` | The RTSP port of that `AIDOT_GO2RTC_API` server. Default 8554; a malformed value logs a warning and falls back to 8554. |

Video decoding is otherwise chosen automatically: each candidate must actually
decode a sample on your machine before it is used, because the decoders ffmpeg
lists describe what it was built with rather than what the hardware can do.
Hardware decoding is not always faster, so candidates are ranked by measured
speed and the result is remembered.

## Supported devices

Confirmed on AiDot / Leedarson Wi-Fi bulbs, the **M3 Pro (A000088)**, **L2
battery (A001513)** and **PTZ (A001064)** cameras; other models should work
too. A BLE hub on the account (a B1 Plus, say) gets no device or entities,
and the lights behind it are not reachable through this integration. Full
entity list:
**[Supported devices](https://github.com/cbrightly/hass-aidot-cameras/wiki/Supported-devices)**.
Which camera models are recognised, how each connects, and which have been
tested live is one table in the library's
[CAMERAS.md](https://github.com/cbrightly/python-aidot-cameras/blob/main/docs/CAMERAS.md#supported-cameras).

## Local control and which account you sign in as

Local (LAN) control works, but devices accept it **only from the account that
owns them**. Sign in as a member of a shared home and everything still works
through the cloud, while local control silently never engages -- the cloud hands
a shared member a complete device list, credentials included, so nothing looks
wrong until the device itself refuses the login.

**So if "Local camera control" is on and nothing happens, check the account
first.** A secondary login -- the sort you might create so the integration does
not contend with the phone app over a rotating token -- controls everything
through the cloud and never logs in locally.

Camera live view is unaffected either way: it is WebRTC signalled over cloud
MQTT and does not use this path. Per-model refusal codes are in
[Known limitations](https://github.com/cbrightly/hass-aidot-cameras/wiki/Known-limitations).

## License

MIT -- see [LICENSE](LICENSE). This integration is not affiliated with or endorsed
by AiDot or Leedarson; it is community-maintained and provided as-is.
