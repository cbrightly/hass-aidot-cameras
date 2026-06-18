# AiDot for Home Assistant

A Home Assistant custom integration for **AiDot / Leedarson** Wi-Fi lights **and
cameras**. This is a camera-capable fork of the upstream
[AiDot-Development-Team/hass-AiDot](https://github.com/AiDot-Development-Team/hass-AiDot)
(lights-only).

It is the Home Assistant front-end only - all device communication lives in the
[`python-aidot-cameras`](https://github.com/cbrightly/python-aidot-cameras) library, which this
integration installs automatically.

## Features

- **Lights** - on/off, brightness, color (RGBW) and color-temperature.
- **Cameras**
  - Live **WebRTC** streaming (via go2rtc) and snapshots. Media flows LAN-direct
    when the camera is on the same network.
  - **Motion / person events** (`event` entity) for automations.
  - **Two-way audio** - the `aidot.talk` service plays a media-source or http(s)
    URL through the camera speaker.
  - Controls: motion detection, status LED, microphone, floodlight, siren,
    IR light, auto-tracking (switches); night vision (select); motion
    sensitivity and speaker volume (numbers); PTZ (buttons, on supported models).

## Installation (HACS)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**, add
   `https://github.com/cbrightly/hass-aidot-cameras` with category **Integration**.
2. Install **AiDot**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → AiDot**, and sign in with
   your AiDot account.

> The integration depends on `python-aidot-cameras[webrtc]`, which Home
> Assistant installs automatically from this integration's manifest. For camera
> streaming, Home Assistant needs **ffmpeg** and (for sub-second browser
> playback) **go2rtc** - both ship with Home Assistant OS / Container, and
> go2rtc is bundled with Home Assistant 2026. Without go2rtc the integration
> falls back to HLS (higher latency).

## Configuration options

After the integration is set up, open **Settings → Devices & Services → AiDot →
Configure** to adjust its behaviour. All options have sensible defaults and most
users never need to change them.

| Option | Default | What it does |
|---|---|---|
| **Connection mode** | Relay (recommended) | How cameras connect. **Relay** keeps the cloud TURN relay available and lets the connection pick the best path — exactly what the official app does, so it works on every network (same-LAN, a separate camera VLAN, or remote/strict-NAT). **LAN-direct** skips the relay for a faster (~1 s vs ~3–4 s) start, but a camera that is *not* on the same network as Home Assistant cannot connect — choose it only if all cameras share the HA LAN. (Existing installs that had "Fast connect" on are migrated to LAN-direct automatically.) |
| **Mains camera warm-hold (seconds)** | 120 | How long a mains-powered camera's live session is kept warm after the last viewer leaves, so re-opening it is instant instead of paying the full cold-start handshake. `0` keeps it warm forever. Each warm camera holds a concurrent-stream slot and keeps decrypting, so keep the total within the stream cap (3). Battery cameras ignore this and warm on motion instead, to preserve battery. |
| **SDES camera audio** | On | Includes audio in SDES (battery) camera streams, matching the official app. On by default: the library feeds the audio encoder a continuous silence base so audio from battery cameras streams smoothly. Turn off only if you don't want camera audio. |
| **Camera audio gain (dB)** | -8 | Volume trim for SDES camera audio. The camera microphone runs hot, so the default tames it. Raise toward `0` (or positive) if audio is too quiet; lower if it clips. |
| **SDES fast connect (faster signaling)** | **On** | Matches the official app (it never waits for `livePlayResp`): skips that wait and goes straight to the video handshake, so SDES/battery cameras start ~4.5 s sooner. The PTZ (A001064) is auto-excluded (its handshake needs the slower path). If a specific camera ever drops a live view to a snapshot, turn this off for it. |
| **Local camera control** | Off | Routes camera *settings* writes (LED, motion detection, night vision, sensitivity, volume, PTZ tracking) over the LAN instead of the cloud, for eligible mains-powered cameras, with automatic cloud fallback. Does not affect video. Battery cameras are skipped automatically. Takes effect after a restart. |
| **Camera stream port base** | 18600 | Base local port used to serve camera streams to Home Assistant / go2rtc (each camera uses `base`..`base+399`). Change only if it conflicts with another service. |

## Two-way audio (`aidot.talk`)

```yaml
service: aidot.talk
target:
  entity_id: camera.front_door
data:
  media: "media-source://media_source/local/doorbell.mp3"
  max_seconds: 30
```

## PTZ (pan / tilt / zoom)

PTZ cameras (model **A001064**) expose two ways to move the camera:

- **Button entities** - only the buttons the camera actually supports are created.
  A pan-only camera gets `_left` / `_right` / `_stop`; a full pan-tilt camera also
  gets `_up` / `_down`; zoom buttons appear only if the camera advertises zoom
  capability. Good for automations and quick taps.
- **`aidot.ptz` service** - one call for any direction; handy in scripts and as a
  dashboard card's tap target.

```yaml
service: aidot.ptz
target:
  entity_id: camera.outdoor_ptz
data:
  direction: left      # up/down/left/right/left_up/.../zoom_in/zoom_out/stop
  speed: 4             # 1 (slow) - 8 (fast)
```

> PTZ commands ride the **active stream session**, so the camera must be
> streaming - open the live view first. `stop` halts continuous motion.

### Overlay the controls on the live view (no custom cards)

Home Assistant has no built-in PTZ overlay (neither does ONVIF/Reolink - it's a
core limitation), but you can put the arrows **on top of the live feed** with the
built-in **Picture Elements** card - no HACS required. Tapping an arrow moves the
camera instead of opening a dialog.

`camera_view: live` is important: it keeps the WebRTC session open so PTZ
commands have an active channel. While the stream is connecting the card shows
the camera's latest event thumbnail as a poster (no blank tile).

```yaml
type: picture-elements
camera_image: camera.outdoor_ptz
camera_view: live
elements:
  # D-pad
  - type: icon
    icon: mdi:arrow-up-bold
    style: {top: 12%, left: 50%, transform: translate(-50%,-50%), color: white}
    tap_action: {action: perform-action, perform_action: button.press,
                 target: {entity_id: button.outdoor_ptz_ptz_up}}
  - type: icon
    icon: mdi:arrow-down-bold
    style: {top: 88%, left: 50%, transform: translate(-50%,-50%), color: white}
    tap_action: {action: perform-action, perform_action: button.press,
                 target: {entity_id: button.outdoor_ptz_ptz_down}}
  - type: icon
    icon: mdi:arrow-left-bold
    style: {top: 50%, left: 8%, transform: translate(-50%,-50%), color: white}
    tap_action: {action: perform-action, perform_action: button.press,
                 target: {entity_id: button.outdoor_ptz_ptz_left}}
  - type: icon
    icon: mdi:arrow-right-bold
    style: {top: 50%, left: 92%, transform: translate(-50%,-50%), color: white}
    tap_action: {action: perform-action, perform_action: button.press,
                 target: {entity_id: button.outdoor_ptz_ptz_right}}
  # Zoom
  - type: icon
    icon: mdi:magnify-plus
    style: {bottom: 8%, left: 38%, transform: translate(-50%,50%), color: white}
    tap_action: {action: perform-action, perform_action: button.press,
                 target: {entity_id: button.outdoor_ptz_ptz_zoom_in}}
  - type: icon
    icon: mdi:magnify-minus
    style: {bottom: 8%, left: 62%, transform: translate(-50%,50%), color: white}
    tap_action: {action: perform-action, perform_action: button.press,
                 target: {entity_id: button.outdoor_ptz_ptz_zoom_out}}
```

For a polished joystick/D-pad in the fullscreen view, the HACS
**Advanced Camera Card** can bind its PTZ controls to these same buttons -
optional, for the best experience.

## Resolution (HD / SD)

Cameras get a `Resolution` select (`select.<camera>_resolution`) with **HD** and
**SD** options. Selecting one sends the camera's stream-quality command (the same
one the official app's HD/SD toggle uses) over the active stream session, so the
camera must be streaming for it to apply.

> **Caveat:** some models stream a single fixed resolution and ignore the toggle.
> It has been verified as a no-op on the **M3 Pro (A000088)**, which always
> streams 720p regardless of the setting. The control is provided for parity with
> the app and for models that expose multiple resolutions; on a fixed-resolution
> camera it changes nothing visible.

## Cloud Recordings (Media Browser)

Cameras with an AiDot **cloud storage plan** expose their recorded events in the
Home Assistant **Media** panel under **AiDot → \<camera name\>**.

Each event shows a thumbnail (fetched from CloudFront CDN) and a title with the
event type and timestamp. Tapping an event plays the clip directly inside HA.

### How it works

The AiDot cloud returns each recording as a short HLS stream (M3U8 + `.ts`
segments) on CloudFront, encoded as **H.265/HEVC video + AAC audio** - which
browsers cannot decode via MSE, so a `<video>` element (or `hls.js`) just shows
an endless loading spinner.

To make clips playable, the integration registers a local HA HTTP view at
`/api/aidot/video` that hands the HLS stream to **ffmpeg**, transcodes the video
to **H.264** (AAC audio kept), and streams a `video/mp4` the browser plays
natively. ffmpeg's output is streamed to the browser *as it is produced* (so
playback starts in ~1s regardless of clip length) and teed to a per-event cache
under `aidot_clips/`, so replays are served instantly with seeking. If a
VAAPI/QSV hardware encoder is available it is used, falling back to libx264.

The view takes only `device` + `event`; it resolves the (short-lived, signed)
CloudFront URL **server-side** from the AiDot cloud and never trusts a
client-supplied URL. It carries no HA auth token (the media-browser `<video>`
request can't send one) - access is gated by the unguessable event UUID.

### Requirements

- Camera must have an active AiDot cloud storage subscription.
- Home Assistant must have the **Media Source** integration enabled (it is by
  default). HA 2026.6+ does not auto-discover `media_source.py` in custom
  components; this integration registers it explicitly on setup.

## Supported devices

| Type | Confirmed models | Entities / features |
|---|---|---|
| **Wi-Fi light** | Any AiDot / Leedarson RGB, RGBW, CCT, or dimmable-white bulb | `light` — on/off, brightness, color, color temperature |
| **IPC camera** | M3 Pro (LK.IPC.A000088), PTZ model (LK.IPC.A001064) | See full entity list below |
| **Gateway / hub** | AiDot Smart Home Hub (G152US, Mini Hub) | Appears as a device; no standalone entities |

Any AiDot or Leedarson device not listed here should still work — the table reflects what has been explicitly confirmed.

### Camera entities

| Entity | Description | Default |
|---|---|---|
| `camera` | Live WebRTC stream and snapshots | Enabled |
| `event.motion` | Fires on motion or person detection (`motion` / `person` event types) | Enabled |
| `binary_sensor.occupancy` | True while the most recent motion event is recent | Enabled |
| `sensor.battery` | Battery percentage (battery-powered models) | Disabled |
| `sensor.sd_card_status` | SD card health | Disabled |
| `switch.motion_detection` | Enable / disable motion detection | Enabled |
| `switch.status_led` | Camera status LED | Enabled |
| `switch.microphone` | **Microphone (audio privacy)** — config entity. OFF disables **all** camera audio (recordings *and* two-way talk), not just HA's use. | Enabled |
| `light.<camera>_floodlight` | White floodlight/spotlight (models with one). A **light** entity (config) — moved off the switch domain so blanket `switch.*` automations can't trip it. | Enabled |
| `siren.<camera>_siren` | Siren. A **siren** entity (config) — off the switch domain so it isn't fired by accident. | Enabled |
| `switch.ir_light` | IR emitter on/off | Enabled |
| `switch.ptz_tracking` | Auto-tracking toggle (PTZ models) | Enabled |
| `number.motion_sensitivity` | Motion sensitivity 1–10 | Enabled |
| `number.speaker_volume` | Speaker volume 0–100 | Enabled |
| `select.night_vision` | Auto / on / off | Enabled |
| `select.resolution` | HD / SD (see known limitations) | Enabled |
| PTZ buttons | Move up/down/left/right, stop, zoom in/out | Disabled |

## Data updates

Light state is pushed over TCP and arrives almost instantly — there is no polling interval for lights. Camera attributes (battery level, SD card status, occupancy, motion detection state, night vision mode, IR state) are refreshed from the AiDot cloud every **5 minutes**. The device list — used to detect newly added or removed devices without restarting — is refreshed every **6 hours**. Motion events are delivered continuously via cloud MQTT and typically appear in Home Assistant within a few seconds of the camera detecting movement.

## Use cases

**Security notifications** — combine `event.motion` with a `notify` action and `camera.snapshot` to receive a push notification with a still image whenever someone approaches a door or driveway, even while you are away from home.

**Doorbell-and-announce** — when a camera at your front door detects a person, have Home Assistant announce "Someone is at the door" through a speaker and briefly flash the living-room lights so occupants notice even with headphones on.

**PTZ patrol** — use an automation to step through a sequence of pan-and-tilt positions on a schedule or when a zone becomes occupied, giving a broader view than a static mount.

**Occupancy-triggered lighting** — use `binary_sensor.occupancy` as a trigger to turn on outdoor or garden lights when the camera detects presence, and turn them off a fixed time after occupancy clears.

**Recording review in HA** — cameras with an active AiDot cloud plan surface their recorded events directly in the Home Assistant **Media** panel, so you can browse and replay clips without opening a separate app.

## Automation examples

### Motion alert with a snapshot

Send a mobile notification with a camera still when motion is detected.

```yaml
alias: Camera motion alert
triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_occupancy
    to: "on"
actions:
  - action: camera.snapshot
    target:
      entity_id: camera.front_door
    data:
      filename: /config/www/snapshots/front_door_latest.jpg
  - action: notify.mobile_app_my_phone
    data:
      title: Motion detected
      message: Someone is at the front door.
      data:
        image: /local/snapshots/front_door_latest.jpg
```

### Floodlight on for 60 seconds when a person is detected

The `event.motion` entity fires with an `event_type` of `person` for person detections and `motion` for general motion. Filter on `person` to avoid triggering on animals or branches.

```yaml
alias: Floodlight on person
triggers:
  - trigger: event
    event_type: state_changed
    event_data:
      entity_id: event.backyard_motion
actions:
  - condition: template
    value_template: >
      {{ trigger.event.data.new_state.attributes.event_type == 'person' }}
  - action: light.turn_on
    target:
      entity_id: light.backyard_floodlight
  - delay:
      seconds: 60
  - action: light.turn_off
    target:
      entity_id: light.backyard_floodlight
```

### PTZ preset patrol

Step through four positions in sequence. Each `button.press` nudges the camera; adjust the delay to control how long it dwells at each position. The camera must be streaming for PTZ commands to reach it — include a `camera_view: live` card on a dashboard to maintain the session, or start the stream with a `camera.turn_on` action before the sequence.

```yaml
alias: PTZ patrol
triggers:
  - trigger: time_pattern
    minutes: /30
actions:
  - action: button.press
    target:
      entity_id: button.outdoor_ptz_ptz_left
  - delay:
      seconds: 5
  - action: button.press
    target:
      entity_id: button.outdoor_ptz_ptz_stop
  - delay:
      seconds: 10
  - action: button.press
    target:
      entity_id: button.outdoor_ptz_ptz_right
  - delay:
      seconds: 5
  - action: button.press
    target:
      entity_id: button.outdoor_ptz_ptz_stop
```

## Known limitations

- **Resolution select is a no-op on some models.** The M3 Pro (A000088) and other fixed-resolution cameras stream a single resolution (720p on the M3 Pro) regardless of the HD/SD setting. The control exists for parity with the AiDot app and works as expected on multi-resolution models.
- **PTZ commands, resolution changes, and two-way audio require an active stream.** The camera must be streaming for these commands to reach it — open the live camera card first, then retry.
- **Cloud recordings require a subscription.** The Media Browser integration for recorded clips only works when the camera has an active AiDot cloud storage plan.
- **Off-LAN streaming uses STUN/TURN.** When Home Assistant and the camera are on different networks, media is relayed through the AiDot cloud TURN servers (the same path the official app uses remotely). This is why the default **Connection mode** is Relay — it keeps that fallback available. LAN-direct is faster on the local network but cannot reach a camera on another network/VLAN.
- **Battery cameras skip local control.** The optional LAN-direct discovery uses a UDP subnet sweep that battery cameras do not respond to, so they fall back to cloud-only control even when local control is enabled in the integration options.
- **`aidot.talk` audio format support.** The service decodes audio server-side using ffmpeg, so it accepts any format ffmpeg can handle (MP3, WAV, OGG, AAC, FLAC, and others). The camera speaker receives the decoded PCM stream.
- **Motion event delivery can lag.** Cloud MQTT delivery of motion events is typically within a few seconds but can lag up to approximately 30 seconds depending on cloud conditions.
- **DTLS camera connections are retried automatically.** Connections to DTLS-mode cameras are probabilistic per attempt; the integration retries silently, so a brief delay before a stream loads is normal.
- **First view of an idle camera has a connection delay.** Opening a cold camera runs the proprietary handshake before video appears — roughly 15–25 s for mains cameras and 25–70 s for battery (L2) models. Once connected, the stream is delivered over **go2rtc WebRTC** (sub-second); subsequent views, and views pre-warmed by a recent motion event, are near-instant. The mains warm-hold option (see *Configuration options*) keeps recently-viewed mains cameras instant.
- **One live viewer per camera (AiDot-app contention).** Each camera serves a single live session at a time. If the AiDot mobile app has the camera's live view open, Home Assistant cannot connect (and vice versa) — close the live view in the app before streaming in Home Assistant. Recorded clips and snapshots are unaffected.

## Troubleshooting

**Camera stream won't load in the browser**
go2rtc must be running. Go to **Settings → Add-ons → go2rtc** and confirm it is started. On Home Assistant OS and Container it ships pre-installed; if you run Core or Supervised you may need to install it separately.

**Stream is slow / buffers like HLS instead of WebRTC**
Live view is served over **go2rtc WebRTC** (sub-second) whenever go2rtc is reachable; the integration registers each camera's local serve with go2rtc and hands Home Assistant the go2rtc RTSP URL. If go2rtc is unavailable it falls back to Home Assistant's HLS pipeline (higher latency) — so confirm go2rtc is running (above). Note the first view of an idle camera always has the handshake delay (see *Known limitations*) before WebRTC takes over. To force the HLS fallback (e.g. for debugging), set `AIDOT_GO2RTC=0` in the Home Assistant process environment; `AIDOT_GO2RTC_API` overrides the go2rtc REST base URL (default `http://127.0.0.1:1984`).

**"Camera must be streaming" error on PTZ, resolution change, or `aidot.talk`**
Open the live camera card in the Home Assistant UI to establish the stream session, then retry the command. PTZ, resolution, and audio all ride an active WebRTC session.

**Clips in the Media Browser show blank or won't play**
Recorded clips require an active AiDot cloud storage subscription. Check the AiDot mobile app to confirm the camera's cloud plan is active.

**Motion events are not firing**
First check that `switch.motion_detection` is turned on for the camera. If it is on, cloud MQTT delivery can occasionally lag — wait up to 30 seconds. If events consistently never arrive, reload the integration from **Settings → Devices & Services → AiDot**.

**Authentication error / integration keeps reloading**
Your AiDot credentials have likely expired or changed. Go to **Settings → Devices & Services → AiDot → Reconfigure** and re-enter your credentials.

**LAN control is not attaching to cameras**
Enable **Local camera control** in the integration options (Settings → Devices & Services → AiDot → Configure). This only works for mains-powered cameras; battery cameras are excluded by design. Changes take effect after restarting Home Assistant. If cameras still don't attach, confirm they are on the same subnet as the Home Assistant host.

## License

MIT - see [LICENSE](LICENSE).
