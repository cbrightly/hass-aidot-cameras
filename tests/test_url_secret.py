"""Tests for the persisted clip-link signing secret.

The secret used to be regenerated every process start (secrets.token_bytes),
which invalidated every already-issued signed URL - including the week-long
links inside push notifications - at each Home Assistant restart. These tests
cover async_load_url_secret: first-run creation, survival across a simulated
restart, tolerance of a corrupt or wrong-length stored value, the once-per-
process short-circuit, and serialization against a concurrent first call.
"""

import asyncio
import secrets

import pytest

from custom_components.aidot import proxy


@pytest.fixture(autouse=True)
def _restore_url_secret():
    """Give every test in this module a clean, un-latched starting state.

    Other test modules (e.g. one exercising the real async_setup_entry) can
    leave the once-per-process sentinel set from an earlier, unrelated run in
    the same pytest session - so this resets _SECRET_LOADED to False up
    front rather than merely snapshotting whatever it happens to be, and
    restores both globals afterward so this module doesn't leak forward
    either.
    """
    original_secret = proxy._URL_SECRET
    original_loaded = proxy._SECRET_LOADED
    proxy._SECRET_LOADED = False
    try:
        yield
    finally:
        proxy._URL_SECRET = original_secret
        proxy._SECRET_LOADED = original_loaded


async def test_first_load_creates_and_persists_a_secret(hass, hass_storage):
    await proxy.async_load_url_secret(hass)

    stored = hass_storage["aidot_url_secret"]["data"]["secret"]
    assert len(stored) == 64
    assert bytes.fromhex(stored)  # round-trips as hex
    assert proxy._URL_SECRET == bytes.fromhex(stored)
    assert len(proxy._URL_SECRET) == 32


async def test_signed_url_verifies_across_a_simulated_restart(hass, hass_storage):
    await proxy.async_load_url_secret(hass)

    url = proxy.sign_playback_url("dev", "evt")
    query = url.split("?", 1)[1]
    import urllib.parse

    params = urllib.parse.parse_qs(query)
    exp = params["exp"][0]
    sig = params["sig"][0]

    # Simulate the next HA process: the module-level fallback re-randomizes
    # and the once-per-process sentinel resets before setup calls
    # async_load_url_secret again.
    proxy._URL_SECRET = secrets.token_bytes(32)
    proxy._SECRET_LOADED = False
    await proxy.async_load_url_secret(hass)

    assert proxy._verify_sig("dev", "evt", exp, sig) is True


async def test_second_call_does_not_reload_or_clobber(hass, hass_storage):
    # The store is only ever consulted once per process. A second call - as
    # happens when a second config entry sets up, or an entry reloads -
    # must not touch a garbage-seeded store and must not change the secret
    # already loaded into memory.
    await proxy.async_load_url_secret(hass)
    loaded = proxy._URL_SECRET

    hass_storage["aidot_url_secret"] = {
        "version": 1,
        "key": "aidot_url_secret",
        "data": {"secret": "not-hex-either"},
    }

    await proxy.async_load_url_secret(hass)

    assert proxy._URL_SECRET == loaded


async def test_concurrent_first_calls_agree_on_one_secret(hass, hass_storage, monkeypatch):
    # Two config entries can set up concurrently. Without the lock, both
    # could see an empty store, mint different secrets, and race their
    # writes. With it, only one caller should ever reach secrets.token_bytes.
    #
    # A single sleep(0) inside only store.async_load isn't enough to prove
    # this: the mocked Store completes the rest of one call's body (mint,
    # save, set the globals) synchronously once resumed, so the first call to
    # resume just runs straight through to a finished write before the
    # second call is scheduled again - the second call's own (delayed) load
    # then simply reads back what the first one already wrote, which passes
    # for the wrong reason regardless of whether the lock exists.
    #
    # Delaying BOTH async_load and async_save closes that gap: it forces
    # each call to yield again right after it decides to mint (before it has
    # written anything), giving the other call a real window to also observe
    # the still-empty store and mint independently - the actual race the
    # lock exists to prevent.
    from homeassistant.helpers import storage

    orig_load = storage.Store.async_load
    orig_save = storage.Store.async_save

    async def _slow_load(self):
        await asyncio.sleep(0)   # real suspension: lets the second call interleave
        return await orig_load(self)

    async def _slow_save(self, data):
        await asyncio.sleep(0)   # real suspension: lets the second call interleave
        return await orig_save(self, data)

    monkeypatch.setattr(storage.Store, "async_load", _slow_load)
    monkeypatch.setattr(storage.Store, "async_save", _slow_save)

    mint_calls = []
    orig_token_bytes = proxy.secrets.token_bytes

    def _counting_token_bytes(n):
        mint_calls.append(n)
        return orig_token_bytes(n)

    monkeypatch.setattr(proxy.secrets, "token_bytes", _counting_token_bytes)

    await asyncio.gather(
        proxy.async_load_url_secret(hass),
        proxy.async_load_url_secret(hass),
    )

    stored = hass_storage["aidot_url_secret"]["data"]["secret"]
    assert bytes.fromhex(stored) == proxy._URL_SECRET
    assert len(proxy._URL_SECRET) == 32
    assert len(mint_calls) == 1


async def test_garbage_stored_secret_is_regenerated(hass, hass_storage):
    hass_storage["aidot_url_secret"] = {
        "version": 1,
        "key": "aidot_url_secret",
        "data": {"secret": "zz-not-hex"},
    }

    await proxy.async_load_url_secret(hass)

    assert isinstance(proxy._URL_SECRET, bytes)
    assert len(proxy._URL_SECRET) == 32
    stored = hass_storage["aidot_url_secret"]["data"]["secret"]
    assert bytes.fromhex(stored) == proxy._URL_SECRET


async def test_wrong_length_stored_secret_is_regenerated(hass, hass_storage):
    hass_storage["aidot_url_secret"] = {
        "version": 1,
        "key": "aidot_url_secret",
        "data": {"secret": "aabb"},
    }

    await proxy.async_load_url_secret(hass)

    assert isinstance(proxy._URL_SECRET, bytes)
    assert len(proxy._URL_SECRET) == 32
    stored = hass_storage["aidot_url_secret"]["data"]["secret"]
    assert bytes.fromhex(stored) == proxy._URL_SECRET
