"""CI must fail when a test that needs a tool skips because the tool is missing.

A test that skips is a test that ran nothing. The integration's clip test
skipped in CI for want of ffmpeg, and a clip losing its first seconds passed
(2026-10-07). With AIDOT_FAIL_ON_SKIP set - CI sets it - any skip whose reason
is not one the suite expects (two tests that cover the two shapes of an
upstream record, say, one of which always skips) fails the run and names the
tests. Unset, the guard does nothing, so a developer without ffmpeg still sees
a green run with skips listed.
"""

import os

import pytest

pytest_plugins = ["pytester"]

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def guarded(pytester, monkeypatch):
    # A subprocess with no auto-loaded plugins: the outer session's plugins
    # (Home Assistant's test plugin, say) must not run inside the probe.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", HERE)  # where skip_guard.py is
    pytester.makeconftest("pytest_plugins = ['skip_guard']\n")
    pytester.makepyfile(
        test_things="""
        import pytest
        def test_fine():
            pass
        def test_needs_ffmpeg():
            pytest.skip("no ffmpeg binary")
        @pytest.mark.skipif(True, reason="never on this platform")
        def test_other_shape():
            pass
        def test_needs_av():
            pytest.importorskip("no_such_module_for_this_test")
        """
    )
    return pytester


def test_unexpected_skips_fail_the_run_and_are_named(guarded, monkeypatch):
    monkeypatch.setenv("AIDOT_FAIL_ON_SKIP", "1")
    result = guarded.runpytest_subprocess("-q")
    assert result.ret == 1
    out = result.stdout.str()
    assert "test_needs_ffmpeg" in out and "no ffmpeg binary" in out
    assert "test_needs_av" in out and "no_such_module_for_this_test" in out
    assert (
        "test_other_shape" in out.split("AIDOT_FAIL_ON_SKIP")[-1]
    )  # no reason is expected here


def test_an_unset_guard_and_a_run_without_skips_pass(guarded, monkeypatch):
    monkeypatch.delenv("AIDOT_FAIL_ON_SKIP", raising=False)
    assert guarded.runpytest_subprocess("-q").ret == 0
    monkeypatch.setenv("AIDOT_FAIL_ON_SKIP", "1")
    guarded.makepyfile(
        test_things="""
        import pytest
        def test_one_shape():
            pass
        def test_fine():
            pass
        """
    )
    assert guarded.runpytest_subprocess("-q").ret == 0
