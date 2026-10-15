"""Common fixtures for Aidot integration tests."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The pytest-homeassistant-custom-component plugin imports `custom_components`
# (as a namespace package) before this conftest runs, so sys.path.insert alone
# won't update the already-resolved __path__. Directly patch it to include our
# project's custom_components directory so HA's loader and patch() both find it.
_project_root = Path(__file__).parent.parent
_cc_path = str(_project_root / "custom_components")
sys.path.insert(0, str(_project_root))

import custom_components as _cc_pkg  # noqa: E402

if _cc_path not in list(_cc_pkg.__path__):
    _cc_pkg.__path__ = list(_cc_pkg.__path__) + [_cc_path]

# CI sets AIDOT_FAIL_ON_SKIP so a test that skips for a missing tool fails the
# run instead of passing silently (the clip test did, 2026-10-07); see
# skip_guard.py.
pytest_plugins = ["pytest_homeassistant_custom_component", "tests.skip_guard"]


@pytest.fixture(autouse=True)
def _library_env_is_per_test(monkeypatch):
    """Each test gets the library's switches as it finds them, then restored.

    The integration drives the library through AIDOT_* environment variables,
    and a test that runs the real setup leaves them set for every test after
    it. Pull mode is the baseline here (direct publish off): the library's own
    default is on since 1.0.0rc44, and the tests of the pull path were written
    against pull. A test of push mode sets what it needs itself.
    """
    for key in [k for k in os.environ if k.startswith("AIDOT_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AIDOT_DIRECT_PUBLISH", "0")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests in this package."""
    yield


@pytest.fixture(autouse=True)
def go2rtc_unreachable_by_default():
    """No test talks to a real go2rtc.

    Newer pytest-homeassistant-custom-component fails any test that tries to
    open a socket, even when the code under test catches the error. The
    library's list_streams() already returns {} when go2rtc cannot be reached,
    which is what an unstubbed call used to get from a refused connection, so
    the default here returns exactly that without touching the network. Tests
    that need go2rtc state mock the client themselves, which overrides this.
    """

    async def _unreachable(self, *args, **kwargs):
        return {}

    with patch("aidot_cameras.camera.go2rtc.Go2rtcClient.list_streams", _unreachable):
        yield


@pytest.fixture(autouse=True)
def go2rtc_target_is_the_addon_by_default():
    """camera.py talks to the go2rtc go2rtc_target resolves; here, the add-on.

    The add-on on 127.0.0.1:1984 / 8554 is the reference box's answer and the
    one every older test was written against. Resolving for real would probe
    the network, which the test plugin forbids. A test of another answer (the
    bundled server, an explicit URL, or none) patches
    custom_components.aidot.camera.resolve_go2rtc_target itself, which
    overrides this; the client a target builds is
    custom_components.aidot.go2rtc_target.Go2rtcClient, which is where a test
    stubs go2rtc's answers.
    """
    from aidot_cameras.camera.go2rtc import DEFAULT_BASE_URL, DEFAULT_RTSP_PORT

    from custom_components.aidot import go2rtc_target as gt

    target = gt.Go2rtcTarget(
        DEFAULT_BASE_URL, MagicMock(), "127.0.0.1", DEFAULT_RTSP_PORT, gt.KIND_ADDON
    )
    with patch(
        "custom_components.aidot.camera.resolve_go2rtc_target",
        AsyncMock(return_value=target),
    ):
        yield


@pytest.fixture
def mock_setup_entry():
    """Prevent actual setup of the integration during config flow tests."""
    with patch(
        "custom_components.aidot.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def mock_login_info():
    """Return a minimal successful login payload (mirrors AidotClient.async_post_login)."""
    return {
        "id": "test-user-id-123",
        "username": "test@example.com",
        "password": "correct-password",
        "country_code": "US",
        "accessToken": "fake-token",
        "mqttPassword": "fake-mqtt-pw",
    }
