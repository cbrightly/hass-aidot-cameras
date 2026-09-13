# AiDot for Home Assistant

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

## Features

- **Lights** -- on/off, brightness, colour (RGBW) and colour temperature.
- **Cameras**
  - Live **WebRTC** streaming (via go2rtc) and snapshots, LAN-direct when the
    camera is on the same network.
  - **Motion / person events** (`event` entity) for automations.
  - **Motion push notifications** -- choose cameras, motion or person-only, and
    which notify services to send to; tapping the notification opens the clip.
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
3. **Settings -> Devices & Services -> Add Integration -> AiDot**, and sign in with
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
   See [Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards).
2. **A camera that has been idle takes a moment on its first view** while the
   connection handshake runs. After that go2rtc WebRTC takes over and later
   views are quick.

So a slow *first* frame is expected; a slow *every* frame almost always means
the wrong card.

How long the camera takes to start sending video on a **cold** open, measured
from the integration's own log on a live install:

| camera | cold open |
|---|---|
| mains, standard (DTLS) models | **~2.6 - 2.9 s** |
| mains, SDES models (PTZ / spotlight) | **~2.5 - 3.6 s** |
| battery (L2) models | **~6 - 9 s**, dominated by the camera waking |

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
the start-up warm-up mains cameras get. They *are* warmed on a motion event,
but only because the camera has already woken itself to record.

Mains cameras are held warm by default so their views stay quick. If one keeps
reconnecting while idle, it is a model that stops sending when unwatched and
cannot be held warm -- give it a positive **warm-hold** window instead. See
[Configuration options](https://github.com/cbrightly/hass-aidot-cameras/wiki/Configuration-options).

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
The three that come up most:

- **A battery camera's first view after a long sleep shows nothing, then works
  on the next try.** Expected, not a fault: a deeply asleep camera sometimes
  acknowledges the request and never starts the stream. The integration
  abandons that attempt and retries, and the second one streams normally. A
  battery camera failing *repeatedly* is worth a bug report; a single dud
  attempt after a quiet period is the camera.

- **Live view buffers like HLS instead of being near-instant.** Use a WebRTC
  card (see [Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards)) and check the go2rtc add-on
  is running. Without go2rtc every view falls back to Home Assistant's HLS
  pipeline.

- **A camera connects but the picture stays blank.** Usually the camera's media
  is not reaching Home Assistant - most often because it is isolated on the
  network (a separate VLAN, or AP/client isolation). If it shares the Home
  Assistant LAN, set **Connection mode** to **LAN-direct**. See
  [Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting#camera-connects-but-the-picture-stays-blank-no-video).

**Always update before troubleshooting.** A large share of past reports were
bugs already fixed in a later release; the [CHANGELOG](CHANGELOG.md) records
which.

### Environment overrides

Rarely needed, and off the supported path:

| Variable | Effect |
| --- | --- |
| `AIDOT_VIDEO_DECODER` | Force a decoder by name, or `hwaccel:<method>` (e.g. `hwaccel:videotoolbox`). |
| `AIDOT_DISABLE_HWACCEL=1` | Keep to software decoding. |
| `AIDOT_SERVE_RELAY=1` | Re-enable the library's serve relay. Testing only. |

Video decoding is otherwise chosen automatically: each candidate must actually
decode a sample on your machine before it is used, because the decoders ffmpeg
lists describe what it was built with rather than what the hardware can do.
Hardware decoding is not always faster, so candidates are ranked by measured
speed and the result is remembered.

## Supported devices

Confirmed on AiDot / Leedarson Wi-Fi bulbs, the **M3 Pro (A000088)** and **PTZ
(A001064)** cameras, and AiDot hubs; other models should work too. Full entity
list:
**[Supported devices](https://github.com/cbrightly/hass-aidot-cameras/wiki/Supported-devices)**.

## Local control and which account you sign in as

Local (LAN) control works, but devices accept it **only from the account that
owns them**. Sign in as a member of a shared home and everything still works
through the cloud, while local control silently never engages -- the cloud hands
a shared member a complete device list, credentials included, so nothing looks
wrong until the device itself refuses the login.

**So if "Enable local control" is on and nothing happens, check the account
first.** A secondary login -- the sort you might create so the integration does
not contend with the phone app over a rotating token -- controls everything
through the cloud and never logs in locally.

Camera live view is unaffected either way: it is WebRTC signalled over cloud
MQTT and does not use this path. Per-model refusal codes are in
[Known limitations](https://github.com/cbrightly/hass-aidot-cameras/wiki/Known-limitations).

## License

MIT -- see [LICENSE](LICENSE). This integration is not affiliated with or endorsed
by AiDot or Leedarson; it is community-maintained and provided as-is.
