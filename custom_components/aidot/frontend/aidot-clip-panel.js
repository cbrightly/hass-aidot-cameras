// AiDot Cameras: plays one event clip inside the Home Assistant app.
// Opened from a motion notification at /aidot-clip/<device id>/<event uuid>,
// or for a recording on the camera's SD card at /aidot-clip/<device id>/sd-...
class AidotClipPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  set route(route) {
    this._route = route;
    this._render();
  }

  set narrow(_narrow) {}

  set panel(_panel) {}

  disconnectedCallback() {
    this._stopPendingFetch();
  }

  // Leaving while a card recording is still loading would leave the camera's
  // live view held for the rest of the fetch: stop that recording's fetch.
  _stopPendingFetch() {
    const url = this._pendingStopUrl;
    this._pendingStopUrl = null;
    if (url) fetch(url, { method: "POST", keepalive: true }).catch(() => {});
  }

  _render() {
    if (!this._hass || !this._route) return;
    const parts = (this._route.path || "")
      .split("/")
      .filter((p) => p)
      .map((p) => decodeURIComponent(p));
    const key = parts.join("/");
    if (key === this._shown) return;
    this._stopPendingFetch();
    this._shown = key;
    this.innerHTML = `
      <style>
        :host, aidot-clip-panel { display: block; height: 100%; background: var(--primary-background-color); }
        .bar { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
               color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        .bar button { font: inherit; padding: 6px 12px; border-radius: 8px;
                      border: 1px solid var(--divider-color); background: var(--card-background-color);
                      color: var(--primary-text-color); }
        video { display: block; width: 100%; max-height: calc(100vh - 80px); background: #000; }
      </style>
      <div class="bar"><button id="back">Back</button><span id="status">Loading the clip...</span>
        <button id="stop" hidden>Stop fetching</button></div>
      <video id="video" controls playsinline></video>`;
    this.querySelector("#back").addEventListener("click", () => {
      if (history.length > 1) history.back();
      else location.assign("/");
    });
    const status = this.querySelector("#status");
    if (parts.length !== 2) {
      status.textContent = "This link does not name a clip.";
      return;
    }
    const [device, event] = parts;
    const fromCard = event.startsWith("sd-");
    this._hass
      .callWS({
        type: "media_source/resolve_media",
        media_content_id: `media-source://aidot/${device}/${event}`,
      })
      .then((res) => {
        const video = this.querySelector("#video");
        const stop = this.querySelector("#stop");
        if (fromCard) {
          // A first play fetches the recording off the card, which takes about
          // as long as the clip and holds the camera's live view; a replay is
          // served from the cache at once. The panel cannot tell which, so it
          // says what a wait means rather than claiming a fetch is running.
          status.textContent =
            "Loading the recording. If it has not been played before, it is fetched from the camera's SD card first - about as long as the clip - and the live view is paused meanwhile.";
          const stopUrl = this._hass.hassUrl(res.url);
          this._pendingStopUrl = stopUrl;
          stop.hidden = false;
          stop.addEventListener("click", () => {
            if (stop.disabled) return;
            stop.disabled = true;
            // The play URL's own signature authorises the stop, for this
            // recording only.
            fetch(stopUrl, { method: "POST" })
              .then((r) => {
                if (r.ok) {
                  this._pendingStopUrl = null;
                  status.textContent = "Stopped - playing what was fetched...";
                } else if (r.status === 409) {
                  stop.hidden = true;
                  status.textContent = "Nothing is being fetched for this recording.";
                } else {
                  stop.disabled = false;
                  status.textContent = `Could not stop the fetch (${r.status}).`;
                }
              })
              .catch(() => { stop.disabled = false; });
          });
        } else {
          // The clip is converted for the phone on first play, which can take
          // 10-20 s on a small box: say so until the player has data.
          status.textContent = "Preparing the clip...";
        }
        video.addEventListener("loadeddata", () => {
          this._pendingStopUrl = null;
          status.textContent = "Recording";
          stop.hidden = true;
        }, { once: true });
        video.addEventListener("error", () => {
          this._pendingStopUrl = null;
          stop.hidden = true;
          status.textContent = fromCard
            ? "The recording could not be fetched from the card. The camera may be offline or busy - try again."
            : "The clip could not be played. It may not be uploaded yet - try again in a minute.";
        }, { once: true });
        video.src = this._hass.hassUrl(res.url);
        const playing = video.play();
        if (playing && playing.catch) playing.catch(() => {});
      })
      .catch((err) => {
        status.textContent = `Could not load the clip: ${err && (err.message || err.code) ? err.message || err.code : err}`;
      });
  }
}

if (!customElements.get("aidot-clip-panel")) {
  customElements.define("aidot-clip-panel", AidotClipPanel);
}
