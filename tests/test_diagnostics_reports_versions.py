"""Diagnostics must say which library versions are actually installed.

The public library repo is a GitHub fork of the upstream lights-only
`python-aidot`, so GitHub permanently displays "N commits behind" on it. That
banner is meaningless here -- this package does not merge upstream code, it
depends on upstream as an ordinary pip requirement and extends it -- but it is
the first thing a user sees, and there was nothing anywhere telling them which
upstream release their install is actually running.

Nor could a bug report answer it. Diagnostics is the file a reporter attaches
to an issue and it listed devices only, so "which versions?" cost a round trip
every time.

The requirement is a range rather than a pin (upstream shipped two incompatible
shapes of the private API this attaches to and both are live), which makes the
*resolved* version the only one worth reporting: two users on the same
integration version can legitimately be running different upstream releases.
"""

from custom_components.aidot.diagnostics import _package_versions


def test_it_reports_the_camera_library_and_its_upstream():
    v = _package_versions()
    assert "python-aidot-cameras" in v
    assert "python-aidot" in v


def test_the_versions_are_the_resolved_ones_not_the_requirement():
    # A range requirement means the manifest cannot answer this - only the
    # installed distribution can.
    v = _package_versions()
    assert v["python-aidot-cameras"] is not None
    assert not any(c in str(v["python-aidot-cameras"]) for c in "<>=,"), v


def test_a_missing_package_reports_none_rather_than_raising():
    # Diagnostics must never raise: a partially installed environment is
    # exactly when someone reaches for it.
    v = _package_versions(("definitely-not-installed-xyzzy",))
    assert v == {"definitely-not-installed-xyzzy": None}


class _Coordinator:
    device_coordinators: dict = {}
    camera_coordinators: dict = {}


class _Entry:
    runtime_data = _Coordinator()


async def test_the_diagnostics_download_carries_the_versions():
    # The whole point is that the file a user attaches to an issue answers
    # "which versions?" without a round trip.
    from custom_components.aidot.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(None, _Entry())
    assert "versions" in diag, diag.keys()
    assert diag["versions"]["python-aidot-cameras"] is not None
