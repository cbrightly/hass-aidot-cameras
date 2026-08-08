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

- **Lights** -- on/off, brightness, color (RGBW) and color-temperature.
- **Cameras**
  - Live **WebRTC** streaming (via go2rtc) and snapshots, LAN-direct when the
    camera is on the same network.
  - **Motion / person events** (`event` entity) for automations.
  - **Two-way audio** -- play a media clip or URL through the camera speaker.
  - **PTZ** (pan / tilt / zoom) on supported models.
  - Controls: motion detection, status LED, microphone, floodlight, siren, IR
    light, auto-tracking, night vision, motion sensitivity, and speaker volume.

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

> **Two things decide whether cameras feel fast -- set both and you're done:**
>
> 1. **Use a WebRTC dashboard card, not the default Picture / Picture Glance
>    card.** The Picture card plays through Home Assistant's **HLS** dialog (the
>    ~20 s scrubber buffer), so the first frame is seconds away on *every* view.
> 2. **A camera that has been idle takes a moment on its first view.** Opening it
>    runs a one-time connection handshake; after that go2rtc **WebRTC** takes over
>    and later views are quick.
>
> So a slow *first* frame is expected; a slow *every* frame almost always means
> the wrong card, not the integration. The **mains warm-hold** option (see
> [Configuration options](https://github.com/cbrightly/hass-aidot-cameras/wiki/Configuration-options))
> keeps mains cameras warm so their views stay quick -- set it to `0` (never
> release) if you would rather hold the session than pay a reconnect.

Measured end to end through Home Assistant's own live-view signalling, on a
seven-camera reference fleet (time to first frame):

| camera type | first view |
|---|---|
| mains, standard models | **1.7 - 5.5 s** |
| mains, SDES models (PTZ / spotlight) | **0.5 s warm, up to ~15 s cold** |
| battery models, from idle | **~9 s** |

Battery cameras are deliberately not kept warm -- holding a session drains them --
so they pay the handshake on each view.

The wide range on the SDES spotlight model is not a measurement error and is
worth understanding, because it is the one figure here that varies by more than
an order of magnitude. That camera ends its own streaming session about every
minute or two and starts a new one, with a gap of roughly twenty seconds in
between. A view that arrives while a session is running starts almost at once; a
view that arrives during the gap waits for the camera to come back. Nothing on
the Home Assistant side chooses which one you get. Earlier versions failed
outright rather than waiting, so a slow view here is the fixed behaviour rather
than a fault.

Figures are one install on one network, measured through Home Assistant's own
live view; treat them as the shape to expect, not a guarantee.

Point the HACS
**[Advanced Camera Card](https://github.com/dermotduffy/advanced-camera-card)** at
the camera entity to render the **go2rtc WebRTC** stream inline (quick to open
once the camera is warm):

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.bedroom_m3_pro
live:
  provider: ha          # serves go2rtc WebRTC and auto-starts the stream
  preload: true         # show the tile already live, not click-to-play
  lazy_unload: []       # never auto-unload, so re-views stay warm
dimensions:
  aspect_ratio_mode: static
  aspect_ratio: "16:9"
```

Full card options, multi-camera walls, and the host-resource notes are in
**[Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards)**.

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
  [Cloud recordings](https://github.com/cbrightly/hass-aidot-cameras/wiki/Cloud-recordings)
- **Using it** --
  [Automation examples](https://github.com/cbrightly/hass-aidot-cameras/wiki/Automation-examples) |
  [Services reference](https://github.com/cbrightly/hass-aidot-cameras/wiki/Services)
- **Help** --
  [Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting) |
  [Known limitations](https://github.com/cbrightly/hass-aidot-cameras/wiki/Known-limitations) |
  [FAQ](https://github.com/cbrightly/hass-aidot-cameras/wiki/FAQ)

## Troubleshooting

A few of the most common; the full list is in the
**[Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting)** wiki:

- **Stream is slow / buffers like HLS** -- use a WebRTC card (above) and confirm
  **go2rtc is running** (Settings -> Add-ons -> go2rtc). Without go2rtc, all views
  fall back to HLS.
- **"Camera must be streaming" on PTZ or `aidot.talk`** -- open the live view
  first; those commands ride the active stream session.
- **An unavailable `select.<camera>_resolution`** -- the resolution control was
  removed in **2.11.9** (the cameras acknowledge the command and report the new
  value back, but encode the same picture either way). **2.12.0** clears the
  leftover entity; upgrade and reload the integration once. See
  [Resolution](https://github.com/cbrightly/hass-aidot-cameras/wiki/Resolution).
- **Integration keeps reloading / motion + occupancy look stuck** - self-reloading
  on every account-token refresh (which re-primed the motion poll and dropped
  events) was fixed in **2.8.5**; update to the latest. A genuine authentication
  error also reloads; re-enter your credentials via **AiDot -> Reconfigure**. To
  reload on demand, use the **Reload** button on the AiDot device (or the entry's
  `...` menu).
- **Camera connects but the picture stays blank (battery / PTZ cameras)** - if you
  are on a version before **2.10.0**, update first. Battery L2 models and the
  A001064 PTZ could fail every live view outright, and the cause was a bug in this
  integration, not your network: opening a view re-registered the camera with
  go2rtc, and that call silently dropped the very stream the camera was publishing.
  Both that and the related "press play twice" behaviour on a cold camera are
  fixed in 2.10.0.

  If the picture is black while everything else looks healthy, update to
  **2.11.0**. This was easy to mistake for a dead camera because nothing else was
  wrong: the camera connected, the stream ran at around 2 Mbps, and megabytes
  arrived during a viewing that showed nothing. The decoder was being set up from
  a description of the video remembered from an earlier session, and one camera
  model changes that description between sessions, so it could not decode a
  single frame. Such a camera is now recognised and the stale description
  discarded.

  If it still happens on 2.10.0 or later, the remaining cause is the camera's media
  not reaching Home Assistant - common when the camera is isolated on the network
  (a separate VLAN, or AP/client isolation). If the camera shares the Home
  Assistant LAN, set **Connection mode** to **LAN-direct** to bypass the cloud
  relay; otherwise it is a camera/network limitation. The repeated
  `ffmpeg SDES stderr` log warnings this used to produce are quieted as of
  **2.8.9**. A blank picture on **mains (M3 Pro)** cameras was a separate bug fixed
  in **2.9.14**.
- **Advanced:** the integration serves mains camera streams directly and sets
  `AIDOT_SERVE_RELAY=0` (overriding the library's standalone default of on). Set
  `AIDOT_SERVE_RELAY=1` in Home Assistant's environment only for testing.
- **Video decoding** is worked out per machine when the integration starts, in the
  background, so it never delays startup. Where a machine has video decoding
  hardware that genuinely works it is used, and where it does not, software
  decoding is used as before. The choice cannot be read off the list of decoders
  ffmpeg reports, because that list describes what the program was built with
  rather than what the machine can do - a Raspberry Pi 4 lists decoders for
  graphics hardware it does not have - so each candidate is required to decode a
  sample first. This takes a few seconds the first time and is then remembered.
  Hardware decoding is not always faster - on some machines it is slower than
  software - so candidates are ranked by measured speed on the machine itself
  and the faster one is used. To override, set `AIDOT_VIDEO_DECODER` to a
  decoder name, or to `hwaccel:` followed by an acceleration method (for
  example `hwaccel:videotoolbox`), or set `AIDOT_DISABLE_HWACCEL=1` to keep to
  software decoding.

## Supported devices

Confirmed on AiDot / Leedarson Wi-Fi bulbs, the **M3 Pro (A000088)** and **PTZ
(A001064)** cameras, and AiDot hubs; other models should work too. Full entity
list:
**[Supported devices](https://github.com/cbrightly/hass-aidot-cameras/wiki/Supported-devices)**.

## License

MIT -- see [LICENSE](LICENSE). This integration is not affiliated with or endorsed
by AiDot or Leedarson; it is community-maintained and provided as-is.
