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
  - **Motion push notifications** -- pick cameras, motion or person-only, and
    the notify services to send to from the integration's options; tapping
    the notification opens the clip.
  - **Recordings in the Media browser** -- cloud clips (with a plan) and a
    listing of what each camera holds on its **own SD card**, grouped by day.
  - **Two-way audio** -- play a media clip or URL through the camera speaker.
  - **PTZ** on supported models, limited to the directions the camera itself
    advertises -- the A001064 spotlight is pan-only, so no tilt or zoom buttons
    are created for it.
  - **Sound detection** -- the cameras listen for glass breaking, a smoke
    alarm, a baby crying and a dog barking. Each detector a camera reports
    becomes its own switch. Mains cameras only: reading these costs an MQTT
    round trip and a battery camera would have to be woken for it, so those get
    no sound switches at all rather than switches that never resolve. A mains
    camera that has not answered yet reads as *unknown* rather than off, since
    a row of off switches would claim a state it never reported.
  - Controls: motion detection, status LED, microphone, floodlight, floodlight
    automation, siren, auto-tracking, night vision, motion sensitivity, speaker
    volume, timestamp overlay, HDR, and voice prompts.
  - Diagnostics (disabled by default): battery, SD-card status, Wi-Fi signal
    and network name, and SD-card total/used. The SD figures are shown without a unit on purpose --
    the camera reports bare numbers whose units it does not state, and
    labelling them bytes would render a confident number that could be wrong
    by a factor of a million.

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
  - camera_entity: camera.front_door
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

- **A battery camera's first view after a long sleep shows nothing, then works
  on the next try** -- expected behaviour, not a fault. A deeply asleep camera
  sometimes fails to deliver media on the wake attempt itself (the camera acks
  the request and then never starts the stream); the integration retries, and
  the second attempt streams normally, typically within seconds. Measured on
  the reference fleet: a battery unit that returned nothing for 75 s woke and
  streamed on the immediately following attempt. If a battery camera fails
  repeatedly rather than once, that is worth a bug report; a single dud
  attempt after a quiet period is the camera, not the integration.

- **A camera you removed from the AiDot app still shows up in Home Assistant**
  -- also expected. A device the vendor cloud no longer serves goes
  `unavailable` here (that part is automatic and was verified against real
  offline hardware), but its entities stay registered until you delete the
  device yourself in **Settings -> Devices & Services**. Home Assistant never
  deletes devices behind your back; two cameras retired at the vendor were the
  test case.

- **Stream is slow / buffers like HLS** -- use a WebRTC card (above) and confirm
  **go2rtc is running** (Settings -> Add-ons -> go2rtc). Without go2rtc, all views
  fall back to HLS.
- **One camera's live view stalls or updates only every few seconds, while the
  others are fine** -- update to **2.17.1** (library `1.0.0b23`).

  A camera on a weak wireless link loses some of its video packets on the way to
  Home Assistant, and nothing used to ask for them back. The picture then arrived
  with pieces missing. A browser playing over **WebRTC** hides that damage; the
  **MSE** playback path treats a damaged keyframe as fatal and stops the stream,
  which is why one camera could look fine on one card and fail on another.
  Cameras that send large keyframes are hit hardest -- on the affected model here
  about two thirds of keyframes were arriving damaged.

  The integration now asks the camera to resend what went missing. Measured on
  that camera: **98.4% of lost packets recovered**, against none at all before.
  A camera on a healthy link loses nothing, so nothing is requested and nothing
  changes for it.

  In the browser console the signature was a decode failure on a **keyframe**:

      PIPELINE_ERROR_DECODE: Failed to send video packet for decoding:
      {timestamp=946000 duration=55000 size=189996 is_key_frame=1 ...}

  If it persists after updating, the link itself is the limit -- a resend that
  arrives too late is still discarded. Check the camera's signal, and as a
  fallback set `mode: webrtc` for that camera on the
  [WebRTC Camera](https://github.com/AlexxIT/WebRTC) card rather than a list such
  as `webrtc,mse`, since a fallback list lets the player drop back to MSE.

  You can turn the retransmission requests off with `AIDOT_SDES_NACK=0` if you
  ever need to.

- **"Camera must be streaming" on PTZ or `aidot.talk`** -- open the live view
  first; those commands ride the active stream session.
- **Lights, plugs and switches on a shared home may not be controllable.**
  These non-camera devices are controlled **on the local network only**, and
  that login is the upstream integration's, not this fork's. On the account
  that owns the devices it works normally. An account the home has merely been
  *shared with* can be refused that local login by the device itself, and the
  integration then shows those entities as *unknown* and returns a clear error
  rather than pretending a command was delivered.

  If this affects you, the devices still work from the AiDot app, and the
  cameras in this integration are unaffected -- they do not use that path.
  This is upstream behaviour and is deliberately left alone here rather than
  worked around, but it is worth knowing before sharing a home with someone who
  expects to control the lights from Home Assistant.

- **A leftover `switch.<camera>_auto_tracking` on a camera that cannot move** --
  the auto-tracking switch was gated to pan/tilt-capable cameras in **2.17.3**.
  On a fixed camera the setting was acknowledged and ignored by the camera
  itself (measured on all three A000088 units here), so it was a control that
  did nothing. Delete the stale entity if it lingers.
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
