# AiDot for Home Assistant

[![CI](https://github.com/cbrightly/hass-aidot-cameras/actions/workflows/ci.yml/badge.svg)](https://github.com/cbrightly/hass-aidot-cameras/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/cbrightly/hass-aidot-cameras)](https://github.com/cbrightly/hass-aidot-cameras/releases)
[![License: MIT](https://img.shields.io/github/license/cbrightly/hass-aidot-cameras)](LICENSE)

A Home Assistant custom integration for **AiDot / Leedarson** Wi-Fi lights **and
cameras** - live WebRTC video, two-way audio, PTZ, motion events, and the usual
light controls. It is a camera-capable fork of the upstream lights-only
[AiDot-Development-Team/hass-AiDot](https://github.com/AiDot-Development-Team/hass-AiDot).

The integration is the Home Assistant front-end only; all device communication
lives in the [`python-aidot-cameras`](https://github.com/cbrightly/python-aidot-cameras)
library, which it installs automatically.

<!--
Hero screenshot slot - add a PNG at docs/hero.png in this repo, then replace this
whole comment with the line below (it publishes with the repo and renders here):
<p align="center"><img src="docs/hero.png" alt="AiDot cameras on a Home Assistant dashboard" width="760"></p>
-->

## Features

- **Lights** - on/off, brightness, color (RGBW) and color-temperature.
- **Cameras**
  - **Live WebRTC** streaming (via go2rtc) and snapshots, LAN-direct when the
    camera is on the same network.
  - **Motion / person events** (`event` entity) for automations.
  - **Two-way audio** - play a media clip or URL through the camera speaker.
  - **PTZ** (pan / tilt / zoom) on supported models.
  - **Controls** - motion detection, status LED, microphone, floodlight, siren,
    IR light, auto-tracking, night vision, motion sensitivity, and speaker volume.

## Installation (HACS)

1. In HACS -> ... -> **Custom repositories**, add
   `https://github.com/cbrightly/hass-aidot-cameras` with category **Integration**.
2. Search for **AiDot**, **Download** it, then restart Home Assistant.
3. **Settings -> Devices & Services -> Add Integration -> AiDot**, and sign in with
   your AiDot account.

> [!NOTE]
> Camera streaming needs **ffmpeg** and (for sub-second browser playback)
> **go2rtc** - both ship with Home Assistant OS / Container, and go2rtc is
> bundled with Home Assistant 2026. Without go2rtc the integration falls back to
> higher-latency HLS.

Full steps and prerequisites:
**[Installation](https://github.com/cbrightly/hass-aidot-cameras/wiki/Installation)**.

## Quick start: a fast live view

> [!TIP]
> **Two things decide whether cameras feel fast - set both and you're done:**
>
> 1. **Use a WebRTC dashboard card, not the default Picture / Picture Glance
>    card.** The Picture card plays through Home Assistant's **HLS** dialog (the
>    ~20 s scrubber buffer), so the first frame is seconds away on *every* view.
> 2. **The first view of an idle camera is slow on purpose.** Opening a cold
>    camera runs a one-time connection handshake - roughly **15-21 s** for mains
>    cameras and up to **~70 s** for battery models - then go2rtc **WebRTC** takes
>    over and that camera is **sub-second** for every later view.
>
> So a slow *first* frame is expected; a slow *every* frame almost always means
> the wrong card, not the integration. The **mains warm-hold** option (see
> [Configuration options](https://github.com/cbrightly/hass-aidot-cameras/wiki/Configuration-options))
> keeps recently-viewed cameras instant.

Point the HACS
**[Advanced Camera Card](https://github.com/dermotduffy/advanced-camera-card)** at
the camera entity to render the **go2rtc WebRTC** stream inline (sub-second once
the camera is warm):

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

- **Getting started** -
  [Installation](https://github.com/cbrightly/hass-aidot-cameras/wiki/Installation) -
  [Configuration options](https://github.com/cbrightly/hass-aidot-cameras/wiki/Configuration-options) -
  [Supported devices](https://github.com/cbrightly/hass-aidot-cameras/wiki/Supported-devices)
- **Cameras** -
  [Overview](https://github.com/cbrightly/hass-aidot-cameras/wiki/Cameras) -
  [Dashboard cards](https://github.com/cbrightly/hass-aidot-cameras/wiki/Dashboard-cards) -
  [PTZ control](https://github.com/cbrightly/hass-aidot-cameras/wiki/PTZ-control) -
  [Two-way audio](https://github.com/cbrightly/hass-aidot-cameras/wiki/Two-way-audio) -
  [Resolution](https://github.com/cbrightly/hass-aidot-cameras/wiki/Resolution) -
  [Cloud recordings](https://github.com/cbrightly/hass-aidot-cameras/wiki/Cloud-recordings)
- **Using it** -
  [Automation examples](https://github.com/cbrightly/hass-aidot-cameras/wiki/Automation-examples) -
  [Services reference](https://github.com/cbrightly/hass-aidot-cameras/wiki/Services)
- **Help** -
  [Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting) -
  [Known limitations](https://github.com/cbrightly/hass-aidot-cameras/wiki/Known-limitations) -
  [FAQ](https://github.com/cbrightly/hass-aidot-cameras/wiki/FAQ)

## Troubleshooting

A few of the most common; the full list is in the
**[Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting)** wiki:

- **Stream is slow / buffers like HLS** - use a WebRTC card (above) and confirm
  **go2rtc is running** (Settings -> Add-ons -> go2rtc). Without go2rtc, all views
  fall back to HLS.
- **"Camera must be streaming" on PTZ / resolution / `aidot.talk`** - open the
  live view first; those commands ride the active stream session.
- **Authentication error / integration keeps reloading** - re-enter your AiDot
  credentials via **AiDot -> Reconfigure**.

## Supported devices

| Category | Confirmed                                              |
| -------- | ------------------------------------------------------ |
| Cameras  | M3 Pro (A000088), L2 battery (A001513), PTZ (A001064)  |
| Lights   | AiDot / Leedarson Wi-Fi bulbs                          |
| Hubs     | AiDot hubs                                             |

Other models should work too. Full entity list:
**[Supported devices](https://github.com/cbrightly/hass-aidot-cameras/wiki/Supported-devices)**.

## License

MIT - see [LICENSE](LICENSE). This integration is not affiliated with or endorsed
by AiDot or Leedarson; it is community-maintained and provided as-is.
