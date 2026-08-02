"""The upstream-noise filter must lose chatter and nothing else.

Every sample below is a REAL message from python-aidot 0.3.56's own call sites
(``aidot/device_client.py``, ``aidot/discover.py``), not an invented string - a
filter tested against paraphrases proves nothing about the log it will actually
see.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.upstream_log import UpstreamNoiseFilter, install

LOGGER_NAME = "aidot.device_client"

# --- upstream's routine chatter (device_client.py lines 161-371) ------------ #
NOISE = [
    "3d6037bce874 send_ping {'service': 'test', 'method': 'pingreq', 'seq': '123456'}",
    "a1b2c3d4e5f6:reveive_data : {'service': 'test', 'method': 'pingresp'}",
    "a1b2c3d4e5f6:connect device: 192.0.2.121",
    "a1b2c3d4e5f6:connect success: 192.0.2.121",
    "a1b2c3d4e5f6:login result: {'ack': {'code': 200}}",
    "a1b2c3d4e5f6 send_action ['OnOff', 'Dimming']",
]

# --- the one WARNING that matters (device_client.py line 176) --------------- #
KEEP_WARNINGS = [
    "a1b2c3d4e5f6:connect device error: [Errno 111] Connection refused",
    "a1b2c3d4e5f6:connect device error: timed out",
]

# --- upstream ERRORs; all must survive unconditionally ---------------------- #
KEEP_ERRORS = [
    "a1b2c3d4e5f6:login error, code: 4354",
    "a1b2c3d4e5f6 login read status error [Errno 104] Connection reset by peer",
    "a1b2c3d4e5f6:read status error incomplete read",
    "a1b2c3d4e5f6:recv error: boom",
    "a1b2c3d4e5f6:send action error boom",
    "a1b2c3d4e5f6 ping error boom",
]


def _record(msg: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(LOGGER_NAME, level, __file__, 1, msg, None, None)


ALL_LOGGERS = ("aidot.device_client", "aidot.discover", "aidot.client")


def _strip_filters() -> None:
    for name in ALL_LOGGERS:
        logger = logging.getLogger(name)
        for f in [f for f in logger.filters if isinstance(f, UpstreamNoiseFilter)]:
            logger.removeFilter(f)


@pytest.fixture(autouse=True)
def clean_logging_state():
    """Leave no filter or level behind, and inherit none.

    install() is idempotent by design, so a filter left over from another test
    would make an install-count assertion silently vacuous. The suite runs in
    random order, so this cannot be handled by writing the tests in sequence.
    """
    _strip_filters()
    levels = {n: logging.getLogger(n).level for n in ALL_LOGGERS}
    yield
    _strip_filters()
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def filt():
    """A filter with the logger at its normal (non-debug) level."""
    logging.getLogger(LOGGER_NAME).setLevel(logging.WARNING)
    return UpstreamNoiseFilter()


@pytest.mark.parametrize("msg", NOISE)
def test_routine_chatter_is_dropped(filt, msg):
    assert filt.filter(_record(msg)) is False


@pytest.mark.parametrize("msg", KEEP_WARNINGS)
def test_the_only_useful_warning_survives(filt, msg):
    """'connect device error' is the sole signal that a light failed to connect.

    This is the whole reason the filter exists rather than
    `aidot.device_client: error`, which would silence exactly this.
    """
    assert filt.filter(_record(msg)) is True


@pytest.mark.parametrize("msg", KEEP_ERRORS)
def test_errors_always_survive(filt, msg):
    assert filt.filter(_record(msg, logging.ERROR)) is True


def test_credentials_never_reach_the_log(filt):
    """Upstream dumps the raw device record - password and aesKey included."""
    raw = (
        "a1b2c3d4e5f6:{'id': 'abc', 'mac': '00:00:5e:00:53:01', "
        "'password': '61tCqYWCTLw3', 'aesKey': ['yMrke5j61XgCEtcP'], "
        "'modelId': 'LK.IPC.A001064'}"
    )
    assert filt.filter(_record(raw)) is False

    # Dropped even at ERROR, and even with debug deliberately enabled: there is
    # no level at which writing a device password to disk is the right call.
    assert filt.filter(_record(raw, logging.ERROR)) is False
    logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG)
    assert filt.filter(_record(raw)) is False


def test_debug_brings_the_chatter_back(filt):
    """The escape hatch: `aidot.device_client: debug` restores the protocol log."""
    logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG)
    for msg in NOISE:
        assert filt.filter(_record(msg)) is True


def test_unknown_messages_fail_open(filt):
    """A message we do not recognise must be emitted, never swallowed.

    This is the safety property: if upstream rewords a line or adds a new
    failure mode, the worst case is noise returning - not a silent failure.
    """
    for msg in (
        "a1b2c3d4e5f6: some entirely new upstream message",
        "device unreachable after 3 retries",
        "",
    ):
        assert filt.filter(_record(msg)) is True


def test_a_record_that_cannot_be_formatted_is_emitted(filt):
    """A broken record must not take the logging subsystem down with it."""
    bad = logging.LogRecord(LOGGER_NAME, logging.WARNING, __file__, 1,
                            "%s %s", ("only-one-arg",), None)
    assert filt.filter(bad) is True


def test_install_is_idempotent():
    """Setup runs per config entry and on reload; filters must not accumulate."""
    logger = logging.getLogger(LOGGER_NAME)
    before = len(logger.filters)   # clean_logging_state guarantees ours is absent
    install()
    install()
    install()
    added = [f for f in logger.filters if isinstance(f, UpstreamNoiseFilter)]
    assert len(added) == 1, "install() must not stack filters on repeat calls"
    assert len(logger.filters) == before + 1, "no unrelated filter was disturbed"


def test_install_covers_every_chatty_upstream_logger():
    install()
    for name in ALL_LOGGERS:
        logger = logging.getLogger(name)
        assert any(isinstance(f, UpstreamNoiseFilter) for f in logger.filters), name


def test_discover_chatter_is_dropped(filt):
    """aidot.discover logs every broadcast and every datagram at WARNING too."""
    for msg in (
        "send_broadcast {'service': 'discover', 'payload': {'userId': 'u1'}}",
        "datagram_received {'payload': {'devId': 'abc'}}",
        "setup_discover",
    ):
        rec = logging.LogRecord("aidot.discover", logging.WARNING, __file__, 1,
                                msg, None, None)
        logging.getLogger("aidot.discover").setLevel(logging.WARNING)
        assert filt.filter(rec) is False
