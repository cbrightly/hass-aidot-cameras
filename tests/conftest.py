"""Common fixtures for Aidot integration tests."""

import sys
from pathlib import Path
from unittest.mock import patch

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

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests in this package."""
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
