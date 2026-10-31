"""Camera serve ports: stable, and never shared by two cameras.

The port was ``base + crc32(name) % 400``, so two cameras could get the same
one (about 5% with seven cameras) and the second to bind lost its stream.
serve_ports keeps each camera's crc32 slot when it is free, probes to the next
free slot when it is not, and persists the result so it survives restarts and
never moves when cameras are added.
"""

import itertools

from custom_components.aidot import serve_ports
from custom_components.aidot.camera import _serve_port
from custom_components.aidot.const import DEFAULT_SERVE_PORT_BASE


def _names():
    return (f"cam-{i}" for i in itertools.count())


def _colliding_pair():
    seen = {}
    for name in _names():
        slot = serve_ports.crc_slot(name)
        if slot in seen:
            return seen[slot], name
        seen[slot] = name
    raise AssertionError("unreachable")


def _name_with_slot(slot, exclude, prefix="cam-"):
    for i in itertools.count():
        name = f"{prefix}{i}"
        if name not in exclude and serve_ports.crc_slot(name) == slot:
            return name
    raise AssertionError("unreachable")


def _free_names(count, avoid_slots):
    out, used = [], set(avoid_slots)
    for name in _names():
        slot = serve_ports.crc_slot(name)
        if slot not in used:
            out.append(name)
            used.add(slot)
            if len(out) == count:
                return out
    raise AssertionError("unreachable")


def test_non_colliding_cameras_keep_their_crc32_port():
    names = _free_names(7, ())
    serve_ports.assign(names)
    for name in names:
        assert _serve_port(name) == DEFAULT_SERVE_PORT_BASE + serve_ports.crc_slot(name)


def test_a_collision_is_resolved_deterministically():
    a, b = _colliding_pair()
    lo, hi = sorted((a, b))
    for order in ((a, b), (b, a)):
        serve_ports.reset()
        serve_ports.assign(order)
        assert _serve_port(lo) == DEFAULT_SERVE_PORT_BASE + serve_ports.crc_slot(lo)
        assert _serve_port(hi) != _serve_port(lo)
        assert _serve_port(hi) == DEFAULT_SERVE_PORT_BASE + (
            (serve_ports.crc_slot(hi) + 1) % serve_ports.SLOTS
        )


def test_the_base_option_still_shifts_every_port(monkeypatch):
    a, b = _colliding_pair()
    serve_ports.assign([a, b])
    monkeypatch.setenv("AIDOT_SERVE_PORT_BASE", "30000")
    ports = {_serve_port(a), _serve_port(b)}
    assert len(ports) == 2
    assert all(30000 <= p < 30000 + serve_ports.SLOTS for p in ports)


def test_adding_a_camera_does_not_move_existing_ones():
    a, b = _colliding_pair()
    serve_ports.assign([a, b])
    before = {n: _serve_port(n) for n in (a, b)}
    # A newcomer whose crc32 slot is the one the loser of the pair probed to,
    # and whose name sorts before both: neither may move for it.
    probed = before[max(a, b)] - DEFAULT_SERVE_PORT_BASE
    newcomer = _name_with_slot(probed, exclude={a, b}, prefix="a-")
    assert newcomer < min(a, b)
    serve_ports.assign([newcomer])
    assert {n: _serve_port(n) for n in (a, b)} == before
    assert _serve_port(newcomer) not in before.values()


async def test_ports_survive_a_restart(hass, hass_storage):
    await serve_ports.async_load(hass)
    a, b = _colliding_pair()
    others = _free_names(3, {serve_ports.crc_slot(a), serve_ports.crc_slot(a) + 1})
    serve_ports.assign([a, b, *others])
    await hass.async_block_till_done()
    before = {n: _serve_port(n) for n in (a, b, *others)}
    assert len(set(before.values())) == len(before)
    assert hass_storage[serve_ports.STORAGE_KEY]["data"]["slots"]

    serve_ports.reset()  # simulated restart: memory gone, storage kept
    await serve_ports.async_load(hass)
    # Even if the restart discovers a newcomer first, the stored ones stay put.
    newcomer = _name_with_slot(serve_ports.crc_slot(a), exclude=set(before))
    serve_ports.assign([newcomer])
    assert {n: _serve_port(n) for n in before} == before


async def test_a_corrupt_store_falls_back_to_crc32_ports(hass, hass_storage):
    hass_storage[serve_ports.STORAGE_KEY] = {
        "version": serve_ports.STORAGE_VERSION,
        "key": serve_ports.STORAGE_KEY,
        "data": {"slots": {"cam-x": "nope", "cam-y": 9999}},
    }
    await serve_ports.async_load(hass)
    assert _serve_port("cam-x") == DEFAULT_SERVE_PORT_BASE + serve_ports.crc_slot(
        "cam-x"
    )


async def test_setup_assigns_a_batch_in_sorted_order(monkeypatch):
    """camera.async_setup_entry assigns ports before building any entity."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.aidot import camera

    hooks, seen = [], []
    monkeypatch.setattr(
        camera, "async_add_when_discovered", lambda _entry, add: hooks.append(add)
    )

    def _entity(coord):
        # By the time an entity exists, both of the pair hold distinct slots.
        seen.append(dict(serve_ports._SLOTS))
        return coord

    monkeypatch.setattr(camera, "AidotCamera", _entity)
    monkeypatch.setattr(camera.entity_platform, "async_get_current_platform", MagicMock)
    a, b = _colliding_pair()
    lo, hi = sorted((a, b))
    coords = {}
    entry = SimpleNamespace(runtime_data=SimpleNamespace(camera_coordinators=coords))
    await camera.async_setup_entry(MagicMock(), entry, lambda _new: None)
    (add_new,) = hooks
    coords[hi] = object()  # discovered in reverse-sorted order
    coords[lo] = object()
    add_new()
    assert seen and set(seen[0]) == {lo, hi}
    assert _serve_port(lo) == DEFAULT_SERVE_PORT_BASE + serve_ports.crc_slot(lo)
    assert _serve_port(hi) != _serve_port(lo)
