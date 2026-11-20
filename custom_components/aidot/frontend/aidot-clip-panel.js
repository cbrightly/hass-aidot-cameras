// AiDot Cameras: plays one event clip inside the Home Assistant app.
// Opened from a motion notification at /aidot-clip/<device id>/<event uuid>,
// or for a recording on the camera's SD card at /aidot-clip/<device id>/sd-...
// An administrator can also delete a card recording here, through the
// aidot.delete_sd_recording service.
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

  // The camera entity of the AiDot device the path names: the delete service
  // targets an entity, and the path carries the AiDot device id, which is the
  // device's identifier in the registry.
  _cameraEntity(device) {
    const hass = this._hass;
    const entry = Object.values(hass.devices || {}).find((d) =>
      (d.identifiers || []).some((i) => i[0] === "aidot" && i[1] === device)
    );
    if (!entry) return null;
    const entity = Object.values(hass.entities || {}).find(
      (e) =>
        e.device_id === entry.id &&
        e.platform === "aidot" &&
        e.entity_id.startsWith("camera.")
    );
    return entity ? entity.entity_id : null;
  }

  // The service's own error, in the user's language when the frontend can
  // translate it, else the message the server sent with it.
  async _errorText(err) {
    const hass = this._hass;
    if (err && err.translation_key && err.translation_domain && hass.loadBackendTranslation) {
      try {
        const localize = await hass.loadBackendTranslation("exceptions", err.translation_domain);
        const text = localize(
          `component.${err.translation_domain}.exceptions.${err.translation_key}.message`,
          err.translation_placeholders || {}
        );
        if (text) return text;
      } catch (_e) {
        // Fall through to the server's message.
      }
    }
    return (err && (err.message || err.code)) || String(err);
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
        .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 12px 16px;
               color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        .bar button { font: inherit; padding: 6px 12px; border-radius: 8px;
                      border: 1px solid var(--divider-color); background: var(--card-background-color);
                      color: var(--primary-text-color); }
        .bar button.danger { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
        .bar button.danger.solid { background: var(--error-color, #db4437); color: #fff; }
        .bar button:disabled { opacity: 0.5; }
        .bar.confirm { padding-top: 0; }
        aidot-clip-panel [hidden] { display: none !important; }
        .bar .error { color: var(--error-color, #db4437); }
        video { display: block; width: 100%; max-height: calc(100vh - 80px); background: #000; }
      </style>
      <div class="bar"><button id="back">Back</button><span id="status">Loading the clip...</span>
        <button id="stop" hidden>Stop fetching</button>
        <button id="delete" class="danger" hidden>Delete from card</button></div>
      <div class="bar confirm" id="confirm" hidden>
        <span>Delete this recording from the camera's SD card? This cannot be undone.</span>
        <button id="confirm-delete" class="danger solid">Delete permanently</button>
        <button id="cancel-delete">Cancel</button></div>
      <div class="bar confirm" id="delete-error" role="alert" hidden><span class="error"></span></div>
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
    // Set once the recording is deleted: nothing may then load or relabel it.
    const view = { deleted: false };
    if (fromCard) this._offerDelete(device, event, view);
    this._hass
      .callWS({
        type: "media_source/resolve_media",
        media_content_id: `media-source://aidot/${device}/${event}`,
      })
      .then((res) => {
        if (view.deleted) return;
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
          if (view.deleted) return;
          this._pendingStopUrl = null;
          status.textContent = "Recording";
          stop.hidden = true;
        }, { once: true });
        video.addEventListener("error", () => {
          if (view.deleted) return;
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
        if (view.deleted) return;
        status.textContent = `Could not load the clip: ${err && (err.message || err.code) ? err.message || err.code : err}`;
      });
  }

  // The delete control of a card recording: a destructive button, then a
  // confirmation, then the aidot.delete_sd_recording service with the key the
  // path already carries. Cloud clips never get here. Shown to administrators
  // only; the service applies its own check to whoever calls it.
  _offerDelete(device, key, view) {
    const hass = this._hass;
    if (!hass.user || !hass.user.is_admin) return;
    const shown = this._shown;
    const current = () => this._shown === shown && this.isConnected;
    const button = this.querySelector("#delete");
    const confirm = this.querySelector("#confirm");
    const confirmButton = this.querySelector("#confirm-delete");
    const cancel = this.querySelector("#cancel-delete");
    const errorRow = this.querySelector("#delete-error");
    const showError = (text) => {
      errorRow.querySelector("span").textContent = text;
      errorRow.hidden = !text;
    };
    button.hidden = false;
    button.addEventListener("click", () => {
      showError("");
      confirm.hidden = false;
      button.disabled = true;
    });
    cancel.addEventListener("click", () => {
      confirm.hidden = true;
      button.disabled = false;
    });
    confirmButton.addEventListener("click", () => {
      if (confirmButton.disabled) return;
      const entityId = this._cameraEntity(device);
      if (!entityId) {
        confirm.hidden = true;
        button.disabled = false;
        showError("Could not delete: this camera has no enabled camera entity in Home Assistant.");
        return;
      }
      confirmButton.disabled = true;
      cancel.disabled = true;
      // Through the websocket rather than hass.callService, which would also
      // raise its own toast for an error this panel shows itself.
      hass
        .callWS({
          type: "call_service",
          domain: "aidot",
          service: "delete_sd_recording",
          service_data: { key },
          target: { entity_id: entityId },
        })
        .then(() => {
          if (!current()) return;
          view.deleted = true;
          this._pendingStopUrl = null;
          const video = this.querySelector("#video");
          video.pause();
          video.removeAttribute("src");
          video.load();
          video.hidden = true;
          confirm.hidden = true;
          button.hidden = true;
          this.querySelector("#stop").hidden = true;
          this.querySelector("#status").textContent =
            "Deleted from the camera's SD card.";
        })
        .catch(async (err) => {
          const text = await this._errorText(err);
          if (!current()) return;
          confirmButton.disabled = false;
          cancel.disabled = false;
          confirm.hidden = true;
          button.disabled = false;
          showError(`Could not delete the recording: ${text}`);
        });
    });
  }
}

if (!customElements.get("aidot-clip-panel")) {
  customElements.define("aidot-clip-panel", AidotClipPanel);
}
