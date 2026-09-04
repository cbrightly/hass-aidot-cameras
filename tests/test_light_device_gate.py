"""Which cloud devices become light entities.

A bulb that is online in the AiDot app but absent from Home Assistant used to be
undiagnosable: the gate required ``type == "light"`` exactly, indexed aesKey
blindly, and logged nothing when it dropped a device.
"""
from custom_components.aidot.coordinator import (
    _advertises_light_control,
    _has_usable_aes_key,
    _is_light_device,
)


def _dev(**kw):
    d = {"id": "d1", "modelId": "LK.light.A001497", "type": "light",
         "aesKey": ["0123456789abcdef"]}
    d.update(kw)
    return d


def test_plain_light_passes():
    assert _is_light_device(_dev()) is True


def test_empty_aes_key_list_does_not_raise():
    # Previously device[CONF_AES_KEY][0] raised IndexError out of the filtering
    # comprehension, dropping every remaining light - not just this device.
    assert _has_usable_aes_key(_dev(aesKey=[])) is False
    assert _is_light_device(_dev(aesKey=[])) is False


def test_null_and_missing_aes_key_rejected():
    assert _is_light_device(_dev(aesKey=[None])) is False
    d = _dev()
    del d["aesKey"]
    assert _is_light_device(d) is False


def test_light_without_type_light_is_accepted_via_capability():
    # Controllers and strips do not always report type == "light"; the advertised
    # control.light.* service module is the reliable signal.
    d = _dev(type="strip", product={"serviceModules": [
        {"identity": "control.light.rgbw"}]})
    assert _advertises_light_control(d) is True
    assert _is_light_device(d) is True


def test_non_light_without_capability_is_rejected():
    d = _dev(type="plug", product={"serviceModules": [
        {"identity": "control.switch.onoff"}]})
    assert _is_light_device(d) is False


def test_camera_is_never_a_light():
    d = _dev(modelId="LK.IPC.A000088", type="light")
    assert _is_light_device(d) is False
