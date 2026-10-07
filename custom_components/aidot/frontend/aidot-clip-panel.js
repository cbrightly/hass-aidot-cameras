// AiDot Cameras: plays one event clip inside the Home Assistant app.
// Opened from a motion notification at /aidot-clip/<device id>/<event uuid>.
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

  _render() {
    if (!this._hass || !this._route) return;
    const parts = (this._route.path || "")
      .split("/")
      .filter((p) => p)
      .map((p) => decodeURIComponent(p));
    const key = parts.join("/");
    if (key === this._shown) return;
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
      <div class="bar"><button id="back">Back</button><span id="status">Loading the clip...</span></div>
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
    this._hass
      .callWS({
        type: "media_source/resolve_media",
        media_content_id: `media-source://aidot/${device}/${event}`,
      })
      .then((res) => {
        const video = this.querySelector("#video");
        video.src = this._hass.hassUrl(res.url);
        status.textContent = "Recording";
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
