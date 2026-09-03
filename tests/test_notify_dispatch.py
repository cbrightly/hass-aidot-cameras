"""Motion push notifications: config resolution, wording, payload, cooldown."""
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from homeassistant.util import dt as dt_util

from custom_components.aidot.const import (
    CONF_NOTIFICATIONS,
    DEFAULT_NOTIFY_COOLDOWN_S,
    DEFAULT_NOTIFY_MESSAGE,
    DEFAULT_NOTIFY_TITLE,
)
from custom_components.aidot.notify_dispatch import (
    CooldownGate,
    build_payload,
    event_kind,
    render,
    resolve_camera_config,
)

DEV = "eeee4444ffff5555"


def _options(**cfg):
    return {"serve_port_base": 18600, CONF_NOTIFICATIONS: cfg}


# ---- resolve_camera_config -------------------------------------------------

def test_no_notifications_key_means_off():
    assert resolve_camera_config({"serve_port_base": 1}, DEV) is None


def test_camera_missing_from_config_means_off():
    assert resolve_camera_config(_options(targets=["a"], cameras={}), DEV) is None


def test_camera_events_off_means_off():
    opts = _options(targets=["a"], cameras={DEV: {"events": "off"}})
    assert resolve_camera_config(opts, DEV) is None


def test_camera_targets_override_global():
    opts = _options(targets=["global"], cameras={DEV: {"events": "all", "targets": ["mine"]}})
    cfg = resolve_camera_config(opts, DEV)
    assert cfg.events == "all"
    assert cfg.targets == ["mine"]


def test_empty_camera_targets_fall_back_to_global():
    opts = _options(targets=["global"], cameras={DEV: {"events": "person", "targets": []}})
    cfg = resolve_camera_config(opts, DEV)
    assert cfg.targets == ["global"]
    assert cfg.events == "person"


def test_missing_camera_targets_fall_back_to_global():
    opts = _options(targets=["global"], cameras={DEV: {"events": "all"}})
    assert resolve_camera_config(opts, DEV).targets == ["global"]


def test_both_targets_empty_yields_no_targets_not_none():
    opts = _options(cameras={DEV: {"events": "all"}})
    cfg = resolve_camera_config(opts, DEV)
    assert cfg is not None
    assert cfg.targets == []


def test_defaults_for_cooldown_title_message():
    cfg = resolve_camera_config(_options(cameras={DEV: {"events": "all"}}), DEV)
    assert cfg.cooldown_s == DEFAULT_NOTIFY_COOLDOWN_S
    assert cfg.title == DEFAULT_NOTIFY_TITLE
    assert cfg.message == DEFAULT_NOTIFY_MESSAGE


def test_configured_cooldown_title_message():
    opts = _options(cooldown_s=5, title="T {camera}", message="M", cameras={DEV: {"events": "all"}})
    cfg = resolve_camera_config(opts, DEV)
    assert (cfg.cooldown_s, cfg.title, cfg.message) == (5, "T {camera}", "M")


# ---- event_kind --------------------------------------------------------------

@pytest.mark.parametrize("code,kind", [("4", "person"), ("1", "motion"), (None, "motion"), ("", "motion"), ("9", "motion"), (4, "person")])
def test_event_kind(code, kind):
    assert event_kind({"eventCode": code}) == kind


# ---- render ------------------------------------------------------------------

def test_render_substitutes_known_placeholders():
    assert render("{camera} {event_title}", {"camera": "Porch", "event_title": "Person"}) == "Porch Person"


def test_render_leaves_unknown_placeholder_as_its_name():
    assert render("{camera} {nope}", {"camera": "Porch"}) == "Porch {nope}"


@pytest.mark.parametrize("bad", ["{", "}", "{0}", "{camera.x}", "{camera[0]}"])
def test_render_never_raises_on_malformed_template(bad):
    assert isinstance(render(bad, {"camera": "Porch"}), str)


# ---- build_payload -------------------------------------------------------------

def _payload(**over):
    kw = dict(
        dev_id=DEV,
        camera_name="Porch",
        kind="person",
        event={"eventCode": "4", "picUrl": "https://cdn/x.jpg", "eventUuid": "u-1"},
        title_template="{camera}: {event_title}",
        message_template="{event} at {time} ({device_id})",
        camera_entity_id="camera.porch",
    )
    kw.update(over)
    return build_payload(**kw)


def test_payload_title_and_message_render():
    p = _payload()
    assert p["title"] == "Porch: Person"
    assert p["message"].startswith("person at ")
    assert p["message"].endswith(f"({DEV})")


def test_payload_rich_fields():
    p = _payload()
    d = p["data"]
    assert d["tag"] == f"aidot-{DEV}"
    assert d["group"] == "aidot-motion"
    assert d["image"] == "https://cdn/x.jpg"
    assert d["url"].startswith("/api/aidot/video?")
    assert "event=u-1" in d["url"]
    assert d["clickAction"] == d["url"]
    assert d["entity_id"] == "camera.porch"


def test_payload_omits_missing_rich_fields():
    p = _payload(event={"eventCode": "1"}, camera_entity_id=None, kind="motion")
    d = p["data"]
    for key in ("image", "url", "clickAction", "entity_id"):
        assert key not in d
    assert d["tag"] == f"aidot-{DEV}"
    assert p["title"] == "Porch: Motion"


def test_payload_time_uses_event_time_when_present():
    event_time_ms = 1_700_000_000_000
    expected = dt_util.as_local(dt_util.utc_from_timestamp(event_time_ms / 1000.0)).strftime("%H:%M")
    p = _payload(event={"eventCode": "4", "eventTime": event_time_ms})
    assert f"person at {expected} " in p["message"]


@pytest.mark.parametrize("bad_event", [{"eventCode": "4"}, {"eventCode": "4", "eventTime": "abc"}])
def test_payload_time_falls_back_to_now_when_event_time_missing_or_bad(bad_event):
    p = _payload(event=bad_event)
    match = re.search(r"person at (\d\d:\d\d) ", p["message"])
    assert match is not None


# ---- CooldownGate ----------------------------------------------------------------

def test_first_event_always_allowed():
    assert CooldownGate().allows(DEV, "motion", 60, now=100.0)


def test_within_cooldown_suppressed():
    g = CooldownGate()
    g.record(DEV, "motion", now=100.0)
    assert not g.allows(DEV, "motion", 60, now=130.0)


def test_after_cooldown_allowed():
    g = CooldownGate()
    g.record(DEV, "motion", now=100.0)
    assert g.allows(DEV, "motion", 60, now=160.0)


def test_person_bypasses_a_motion_started_cooldown():
    g = CooldownGate()
    g.record(DEV, "motion", now=100.0)
    assert g.allows(DEV, "person", 60, now=101.0)


def test_person_does_not_bypass_a_person_started_cooldown():
    g = CooldownGate()
    g.record(DEV, "person", now=100.0)
    assert not g.allows(DEV, "person", 60, now=101.0)
    assert not g.allows(DEV, "motion", 60, now=101.0)


def test_cooldown_zero_never_suppresses():
    g = CooldownGate()
    g.record(DEV, "person", now=100.0)
    assert g.allows(DEV, "person", 0, now=100.0)


def test_cooldown_is_per_camera():
    g = CooldownGate()
    g.record(DEV, "motion", now=100.0)
    assert g.allows("other", "motion", 60, now=101.0)


# ---- AidotMotionNotifier --------------------------------------------------------

from custom_components.aidot import notify_dispatch  # noqa: E402
from custom_components.aidot.notify_dispatch import AidotMotionNotifier  # noqa: E402


class _FakeCamCoord:
    """Stands in for AidotCameraUpdateCoordinator: a motion bus + device info."""

    def __init__(self, dev_id, name="Porch"):
        self.device_client = SimpleNamespace(
            info=SimpleNamespace(dev_id=dev_id, name=name), device_id=dev_id
        )
        self.listeners = []

    def add_motion_listener(self, cb):
        self.listeners.append(cb)

        def _remove():
            if cb in self.listeners:
                self.listeners.remove(cb)

        return _remove

    def fire(self, event):
        for cb in list(self.listeners):
            cb(event)


def _entry(hass, cams, **cfg):
    """A config entry whose runtime_data looks like the manager coordinator."""
    manager = SimpleNamespace(camera_coordinators=cams, refresh_listeners=[])

    def _add_listener(cb):
        manager.refresh_listeners.append(cb)
        return lambda: manager.refresh_listeners.remove(cb)

    manager.async_add_listener = _add_listener
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = _options(**cfg)
    entry.runtime_data = manager
    entry.async_create_task = lambda _hass, coro, name=None: hass.async_create_task(coro)
    return entry


def _capture_notify(hass, name):
    calls = []
    hass.services.async_register("notify", name, lambda call: calls.append(dict(call.data)))
    return calls


async def test_attach_subscribes_every_camera_once(hass):
    cam = _FakeCamCoord(DEV)
    n = AidotMotionNotifier(hass, _entry(hass, {DEV: cam}))
    n.start()
    n.attach()
    assert len(cam.listeners) == 1


async def test_new_camera_after_start_gets_a_listener_on_refresh(hass):
    cams = {}
    entry = _entry(hass, cams)
    n = AidotMotionNotifier(hass, entry)
    n.start()
    late = _FakeCamCoord("late1")
    cams["late1"] = late
    for cb in entry.runtime_data.refresh_listeners:
        cb()
    assert len(late.listeners) == 1


async def test_detach_removes_listeners(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam})
    n = AidotMotionNotifier(hass, entry)
    n.start()
    n.detach()
    assert cam.listeners == []
    assert entry.runtime_data.refresh_listeners == []


async def test_event_sends_to_camera_targets(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["global"], cameras={DEV: {"events": "all", "targets": ["phone"]}})
    phone = _capture_notify(hass, "phone")
    glob = _capture_notify(hass, "global")
    AidotMotionNotifier(hass, entry).start()
    cam.fire({"eventCode": "1", "picUrl": "https://cdn/p.jpg", "eventUuid": "u1"})
    await hass.async_block_till_done()
    assert len(phone) == 1 and glob == []
    assert phone[0]["title"] == "Porch: Motion"
    assert phone[0]["data"]["image"] == "https://cdn/p.jpg"


async def test_person_only_camera_ignores_plain_motion(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["phone"], cameras={DEV: {"events": "person"}})
    phone = _capture_notify(hass, "phone")
    AidotMotionNotifier(hass, entry).start()
    cam.fire({"eventCode": "1"})
    cam.fire({"eventCode": "4"})
    await hass.async_block_till_done()
    assert len(phone) == 1
    assert phone[0]["title"] == "Porch: Person"


async def test_camera_set_to_off_sends_nothing(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["phone"], cameras={DEV: {"events": "off"}})
    phone = _capture_notify(hass, "phone")
    AidotMotionNotifier(hass, entry).start()
    cam.fire({"eventCode": "4"})
    await hass.async_block_till_done()
    assert phone == []


async def test_options_are_read_live(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["phone"], cameras={DEV: {"events": "off"}})
    phone = _capture_notify(hass, "phone")
    AidotMotionNotifier(hass, entry).start()
    entry.options = _options(targets=["phone"], cameras={DEV: {"events": "all"}})
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    assert len(phone) == 1


async def test_cooldown_suppresses_repeat_motion_but_not_person(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["phone"], cooldown_s=60, cameras={DEV: {"events": "all"}})
    phone = _capture_notify(hass, "phone")
    AidotMotionNotifier(hass, entry).start()
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    cam.fire({"eventCode": "4"})
    await hass.async_block_till_done()
    assert [c["title"] for c in phone] == ["Porch: Motion", "Porch: Person"]


async def test_no_targets_anywhere_warns_once_and_sends_nothing(hass, caplog):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, cameras={DEV: {"events": "all"}})
    AidotMotionNotifier(hass, entry).start()
    cam.fire({"eventCode": "1"})
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "no notify target" in r.getMessage()]
    assert len(warnings) == 1
    assert "Porch" in warnings[0].getMessage()


async def test_no_target_warning_rearms_after_a_successful_send(hass, caplog):
    cam = _FakeCamCoord(DEV)
    entry = _entry(
        hass, {DEV: cam}, targets=["phone"], cooldown_s=0, cameras={DEV: {"events": "all"}}
    )
    phone = _capture_notify(hass, "phone")
    AidotMotionNotifier(hass, entry).start()

    # Targets configured: event sends normally.
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    assert len(phone) == 1

    def _warnings():
        return [
            r for r in caplog.records
            if r.levelname == "WARNING" and "no notify target" in r.getMessage()
        ]

    # Targets removed: first warning.
    entry.options = _options(cooldown_s=0, cameras={DEV: {"events": "all"}})
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    assert len(_warnings()) == 1

    # Targets restored and a send succeeds: the latch should clear.
    entry.options = _options(targets=["phone"], cooldown_s=0, cameras={DEV: {"events": "all"}})
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    assert len(phone) == 2
    assert len(_warnings()) == 1

    # Targets removed again: a SECOND warning must fire.
    entry.options = _options(cooldown_s=0, cameras={DEV: {"events": "all"}})
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    assert len(_warnings()) == 2


async def test_unknown_target_does_not_block_the_others(hass, caplog):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["missing", "phone"], cameras={DEV: {"events": "all"}})
    phone = _capture_notify(hass, "phone")
    AidotMotionNotifier(hass, entry).start()
    cam.fire({"eventCode": "1"})
    await hass.async_block_till_done()
    assert len(phone) == 1
    assert any("missing" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


async def test_camera_removed_on_refresh_loses_its_listener(hass):
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam})
    n = AidotMotionNotifier(hass, entry)
    n.start()
    assert cam.listeners != []
    del entry.runtime_data.camera_coordinators[DEV]
    for cb in list(entry.runtime_data.refresh_listeners):
        cb()
    assert cam.listeners == []


async def test_scheduling_failure_is_logged_and_leaves_no_pending_coroutine(hass, caplog):
    # entry.async_create_task can fail (entry unloading, event loop closing, ...);
    # the callback must log it, not raise into the bus, and must not leak the
    # unscheduled coroutine (which would surface later as a RuntimeWarning).
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["phone"], cameras={DEV: {"events": "all"}})

    def _raise(_hass, _coro, name=None):
        raise RuntimeError("cannot schedule")

    entry.async_create_task = _raise
    n = AidotMotionNotifier(hass, entry)
    n.start()
    cam.fire({"eventCode": "1"})  # must not raise out of fire()
    await hass.async_block_till_done()
    errors = [
        r for r in caplog.records
        if r.levelname == "ERROR" and "Could not schedule" in r.getMessage()
    ]
    assert len(errors) == 1


async def test_a_failure_inside_send_is_logged_not_raised(hass, caplog, monkeypatch):
    # The bus isolates callbacks, but the notifier must not raise into it either:
    # a failure anywhere in the send path must be caught and logged, never
    # propagated back through _on_motion into the motion poll loop.
    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(notify_dispatch, "build_payload", _boom)
    cam = _FakeCamCoord(DEV)
    entry = _entry(hass, {DEV: cam}, targets=["phone"], cameras={DEV: {"events": "all"}})
    _capture_notify(hass, "phone")
    n = AidotMotionNotifier(hass, entry)
    n.start()
    cam.fire({"eventCode": "1"})  # must not raise out of fire()
    await hass.async_block_till_done()   # no exception propagates
    errors = [
        r for r in caplog.records
        if r.levelname == "ERROR"
        and "Motion notification for" in r.getMessage()
        and "failed" in r.getMessage()
    ]
    assert len(errors) == 1
