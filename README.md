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
  - **Recordings in the Media browser** -- cloud clips (with a plan) and a
    listing of what each camera holds on its **own SD card**, grouped by day.
  - **Two-way audio** -- play a media clip or URL through the camera speaker.
  - **PTZ** on supported models, limited to the directions the camera itself
    advertises -- the A001064 spotlight is pan-only, so no tilt or zoom buttons
    are created for it.
  - Controls: motion detection, status LED, microphone, floodlight, floodlight
    automation, siren, auto-tracking, night vision, motion sensitivity, speaker
    volume, timestamp overlay, HDR, and voice prompts.

    Every one of those is confirmed by reading the value back from the device
    on each model that offers it, because this firmware acknowledges writes it
    then ignores. An IR-light switch was removed in 2.15.0 for exactly that
    reason: the camera accepted the write and kept its own value.

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

A few of the most common; the full list is in the
**[Troubleshooting](https://github.com/cbrightly/hass-aidot-cameras/wiki/Troubleshooting)** wiki:

- **Stream is slow / buffers like HLS** -- use a WebRTC card (above) and confirm
  **go2rtc is running** (Settings -> Add-ons -> go2rtc). Without go2rtc, all views
  fall back to HLS.
- **One camera stutters or keeps reloading, updating about once every few
  seconds, while the others are fine** -- switch that camera to a **WebRTC**
  card. It is a decoder problem specific to the MSE playback path.

  Some cameras change their H.264 parameter sets (the SPS) between sessions,
  and at least one changes it when it switches into night mode, because the
  declared frame rate is part of the SPS. Media Source Extensions takes its
  decoder configuration once, from the `avcC` box in the stream's init segment.
  If the camera's SPS then differs from the one the player was configured with,
  every inter-frame fails to decode and the picture only updates on keyframes --
  roughly one frame every few seconds on a camera with a 4 second keyframe
  interval. WebRTC is unaffected because it reads parameter sets in-band, and
  the stream does carry them on every keyframe.

  Confirmed by comparing the two directly on an affected camera:

      avcC   SPS: 674d001fe900a00b742000007d20000daf8080
      stream SPS: 674d001fe900a00b742000007d200004e34080

  The bytes that differ sit in the VUI timing section -- the frame rate. On an
  unaffected camera on the same host the two match exactly.

  To confirm it on your own setup, open the camera in a desktop browser and look
  at the developer console. The signature is a decode failure on a frame that is
  not a keyframe:

      PIPELINE_ERROR_DECODE: Failed to send video packet for decoding:
      {timestamp=7296000 duration=110000 size=7485 is_key_frame=0 ...}

  With the [WebRTC Camera](https://github.com/AlexxIT/WebRTC) card, set
  `mode: webrtc` for that camera rather than leaving it on a list such as
  `webrtc,mse` -- a fallback list lets the player drop back to MSE and stall
  again. Other cameras can stay on the default.

- **"Camera must be streaming" on PTZ or `aidot.talk`** -- open the live view
  first; those commands ride the active stream session.
- **A leftover `switch.<camera>_ir_light`** -- the IR-light switch was removed
  in **2.15.0**. The camera acknowledged every write to it and kept its own
  value, confirmed by read-back on two M3 Pro units, so it was a control that
  did nothing and said nothing. Night vision still switches the IR cut filter;
  use `select.<camera>_night_vision`. Delete the stale entity if it lingers.
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
  `...` menu). If streams keep dying on a regular cadence, check whether anything
  outside Home Assistant is reloading the entry -- an automation, a script, or a
  leftover job calling `POST /api/config/config_entries/entry/<id>/reload`. Every
  reload tears down every camera session, so a periodic reload looks exactly like
  a streaming bug: the picture goes black until something reopens the camera.
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

## Local control and which account you sign in as

Local (LAN) control works - but the devices accept it **only from the account
that owns them**. If you sign this integration in as a member of a shared home,
every device will still work through the cloud, and local control will never
engage.

That failure is quiet and easy to misread. The cloud gives a shared member a
complete device list, including each device's local `password` and `aesKey`, so
everything looks correct; the device itself then refuses the login with a code
that varies by model:

| Model | What the device answers |
| --- | --- |
| `LK.light.A001493` | 400 `not equal abort user id or password` |
| `LK.light.A001497`, `LK.plug.A001535` | 4354 `fail` |
| `LK.IPC.A000088` cameras | 4352 `fail` |

The first one is the honest message - it is the **user id** that does not match,
not the password.

Verified 2026-08-08 across ten devices and four model families: all ten accept
the owning account and refuse a shared-home member.

**So if "Enable local control" is on and nothing happens, check the account
first.** A secondary login - the sort you might create so the integration does
not contend with the phone app over a rotating token - controls everything
through the cloud and never logs in locally.

Camera live view is unaffected either way: it is WebRTC signaled over cloud MQTT
and does not use this path.

## License

MIT -- see [LICENSE](LICENSE). This integration is not affiliated with or endorsed
by AiDot or Leedarson; it is community-maintained and provided as-is.
