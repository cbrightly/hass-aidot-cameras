/**
 * AiDot PTZ Card
 *
 * A custom Lovelace card that gives AiDot PTZ cameras an app-like, press-and-hold
 * joystick: holding a direction sends a single `aidot.ptz` *move* command and
 * keeps the camera moving; releasing sends `aidot.ptz` *stop*. This is the only
 * control model that matches the official app — Home Assistant's built-in
 * tap/hold actions are discrete (a tap moves, a separate tap stops), so they
 * always overshoot. See the README ("How responsive is PTZ?") for the why.
 *
 * The live feed is rendered by Home Assistant's own `picture-entity` card with
 * `camera_view: live`, so it uses the native go2rtc WebRTC path — and keeping the
 * feed live also keeps the stream session open, which PTZ commands require.
 *
 * Pure custom element, no external imports, so it loads as a plain ES module the
 * integration registers automatically (no manual HACS/resource step needed).
 */

const CARD_VERSION = "1.0.0";

// Directions the aidot.ptz service understands, mapped to overlay icons.
const ICONS = {
  up: "mdi:chevron-up",
  down: "mdi:chevron-down",
  left: "mdi:chevron-left",
  right: "mdi:chevron-right",
  zoom_in: "mdi:magnify-plus-outline",
  zoom_out: "mdi:magnify-minus-outline",
};

class AidotPtzCard extends HTMLElement {
  static getStubConfig() {
    return {
      entity: "",
      speed: 4,
      show_feed: true,
      pan: true,
      tilt: true,
      zoom: true,
      aspect_ratio: "16:9",
    };
  }

  static getConfigElement() {
    return document.createElement("aidot-ptz-card-editor");
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("aidot-ptz-card: 'entity' (a camera.* entity) is required");
    }
    if (!String(config.entity).startsWith("camera.")) {
      throw new Error("aidot-ptz-card: 'entity' must be a camera.* entity");
    }
    this._config = {
      speed: 4,
      show_feed: true,
      pan: true,
      tilt: true,
      zoom: true,
      aspect_ratio: "16:9",
      ...config,
    };
    this._active = null;
    this._build();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._feed) this._feed.hass = hass;
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    return this._config && this._config.show_feed ? 5 : 2;
  }

  connectedCallback() {
    // Safety: if the page is hidden or loses focus mid-hold, stop the camera so
    // it can't pan forever because a pointerup never arrived.
    this._onInterrupt = () => this._release();
    window.addEventListener("blur", this._onInterrupt);
    document.addEventListener("visibilitychange", this._onVisibility = () => {
      if (document.hidden) this._release();
    });
  }

  disconnectedCallback() {
    this._release();
    if (this._onInterrupt) window.removeEventListener("blur", this._onInterrupt);
    if (this._onVisibility) document.removeEventListener("visibilitychange", this._onVisibility);
  }

  _build() {
    if (!this._root) this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .wrap { position: relative; width: 100%; overflow: hidden; border-radius: var(--ha-card-border-radius, 12px); }
      .wrap.no-feed {
        background: var(--ha-card-background, var(--card-background-color, #1c1c1c));
        border: 1px solid var(--divider-color, rgba(0,0,0,.12));
        aspect-ratio: var(--ptz-aspect, 16 / 9);
      }
      .feed { display: block; width: 100%; }
      .feed ha-card { box-shadow: none; border: none; border-radius: inherit; }
      .overlay {
        position: absolute; inset: 0; pointer-events: none;
        touch-action: none; -webkit-user-select: none; user-select: none;
      }
      .btn {
        position: absolute; pointer-events: auto; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        width: 18%; max-width: 60px; aspect-ratio: 1 / 1;
        color: #fff; background: rgba(0,0,0,.38);
        border: 1px solid rgba(255,255,255,.55); border-radius: 50%;
        box-shadow: 0 1px 4px rgba(0,0,0,.45);
        touch-action: none; -webkit-touch-callout: none; user-select: none;
        transition: background .08s ease, transform .08s ease;
      }
      .btn ha-icon { --mdc-icon-size: 60%; width: 60%; height: 60%; }
      .btn.active { background: var(--primary-color, #03a9f4); transform: scale(.92); }
      .up    { top: 4%;  left: 50%; transform: translateX(-50%); }
      .down  { bottom: 4%; left: 50%; transform: translateX(-50%); }
      .left  { top: 50%; left: 4%;  transform: translateY(-50%); }
      .right { top: 50%; right: 4%; transform: translateY(-50%); }
      .up.active    { transform: translateX(-50%) scale(.92); }
      .down.active  { transform: translateX(-50%) scale(.92); }
      .left.active  { transform: translateY(-50%) scale(.92); }
      .right.active { transform: translateY(-50%) scale(.92); }
      .zoom_in  { top: 6%; right: 4%; width: 13%; max-width: 44px; border-radius: 8px; }
      .zoom_out { top: 6%; left: 4%;  width: 13%; max-width: 44px; border-radius: 8px; }
      .badge {
        position: absolute; left: 8px; bottom: 6px; pointer-events: none;
        font: 600 11px/1.4 var(--paper-font-body1_-_font-family, sans-serif);
        color: #fff; background: rgba(0,0,0,.45); padding: 1px 6px; border-radius: 6px;
        opacity: .85;
      }
    `;
    this._root.appendChild(style);

    const wrap = document.createElement("div");
    wrap.className = "wrap" + (this._config.show_feed ? "" : " no-feed");
    wrap.style.setProperty(
      "--ptz-aspect",
      String(this._config.aspect_ratio || "16:9").replace(":", " / ")
    );

    if (this._config.show_feed) {
      const feed = document.createElement("div");
      feed.className = "feed";
      wrap.appendChild(feed);
      this._mountFeed(feed);
    }

    const overlay = document.createElement("div");
    overlay.className = "overlay";

    const dirs = [];
    if (this._config.tilt) dirs.push("up", "down");
    if (this._config.pan !== false) dirs.push("left", "right");
    if (this._config.zoom) dirs.push("zoom_in", "zoom_out");

    for (const dir of dirs) {
      overlay.appendChild(this._makeButton(dir));
    }

    if (this._config.show_feed) {
      const badge = document.createElement("div");
      badge.className = "badge";
      badge.textContent = "Hold to move";
      overlay.appendChild(badge);
    }

    wrap.appendChild(overlay);
    this._root.appendChild(wrap);
  }

  async _mountFeed(host) {
    try {
      const helpers = await window.loadCardHelpers();
      this._feed = helpers.createCardElement({
        type: "picture-entity",
        entity: this._config.entity,
        camera_view: "live",
        show_name: false,
        show_state: false,
        tap_action: { action: "none" },
        hold_action: { action: "none" },
        double_tap_action: { action: "none" },
      });
      if (this._hass) this._feed.hass = this._hass;
      host.appendChild(this._feed);
    } catch (err) {
      host.textContent = "Live feed unavailable";
    }
  }

  _makeButton(dir) {
    const btn = document.createElement("div");
    btn.className = `btn ${dir}`;
    btn.title = dir.replace("_", " ");
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", ICONS[dir]);
    btn.appendChild(icon);

    const press = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.pointerId != null && btn.setPointerCapture) {
        try { btn.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      }
      this._active = dir;
      btn.classList.add("active");
      this._call(dir);
    };
    const release = () => {
      if (this._active !== dir) return;
      btn.classList.remove("active");
      this._release();
    };

    btn.addEventListener("pointerdown", press);
    btn.addEventListener("pointerup", release);
    btn.addEventListener("pointercancel", release);
    btn.addEventListener("lostpointercapture", release);
    // Block the long-press context menu on touch devices.
    btn.addEventListener("contextmenu", (ev) => ev.preventDefault());
    return btn;
  }

  _release() {
    if (!this._active) return;
    this._active = null;
    if (this._root) {
      this._root.querySelectorAll(".btn.active").forEach((b) => b.classList.remove("active"));
    }
    this._call("stop");
  }

  _call(direction) {
    if (!this._hass) return;
    const data = { entity_id: this._config.entity, direction };
    if (direction !== "stop") data.speed = Number(this._config.speed) || 4;
    this._hass.callService("aidot", "ptz", data).catch(() => {
      // Most common cause: the camera isn't streaming. Keeping the feed live
      // (show_feed: true) avoids this; nothing actionable to do here.
    });
  }
}

class AidotPtzCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => {
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: ev.detail.value },
            bubbles: true,
            composed: true,
          })
        );
      });
      this._form.computeLabel = (s) =>
        ({
          entity: "Camera",
          speed: "Speed (1 slow – 8 fast)",
          show_feed: "Show live feed",
          pan: "Show left/right (pan)",
          tilt: "Show up/down (tilt)",
          zoom: "Show zoom buttons",
          aspect_ratio: "Aspect ratio (no feed)",
        }[s.name] || s.name);
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = [
      { name: "entity", required: true, selector: { entity: { domain: "camera" } } },
      { name: "speed", selector: { number: { min: 1, max: 8, step: 1, mode: "slider" } } },
      { name: "show_feed", selector: { boolean: {} } },
      { name: "pan", selector: { boolean: {} } },
      { name: "tilt", selector: { boolean: {} } },
      { name: "zoom", selector: { boolean: {} } },
      { name: "aspect_ratio", selector: { text: {} } },
    ];
  }
}

customElements.define("aidot-ptz-card", AidotPtzCard);
customElements.define("aidot-ptz-card-editor", AidotPtzCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "aidot-ptz-card",
  name: "AiDot PTZ Card",
  description:
    "Live camera with press-and-hold PTZ controls (hold to move, release to stop).",
  preview: false,
  documentationURL:
    "https://github.com/cbrightly/hass-aidot-cameras#app-like-ptz-press-and-hold-card",
});

// eslint-disable-next-line no-console
console.info(`%c AIDOT-PTZ-CARD %c v${CARD_VERSION} `,
  "color:#fff;background:#03a9f4;border-radius:3px 0 0 3px;padding:2px 4px",
  "color:#03a9f4;background:#1c1c1c;border-radius:0 3px 3px 0;padding:2px 4px");
