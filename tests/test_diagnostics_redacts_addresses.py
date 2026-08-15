"""A diagnostics download must not carry the user's IP addresses.

Diagnostics is the file a maintainer asks a reporter to attach to a PUBLIC
issue. `stream_health` embeds the nominated ICE pair, and the library builds
each candidate as "host:port" (python-aidot-cameras camera/webrtc.py), so an
unredacted dump publishes the reporter's LAN address and the WAN address of
whatever relay or peer they reached.

What must survive redaction is the diagnostic signal itself: `local_type` /
`remote_type` / `transport` are what say relay-vs-direct, which is the whole
reason the block exists. Redacting those would trade a privacy leak for a
useless report.
"""

from custom_components.aidot.diagnostics import _redact_stream_health


def _health():
    return {
        "nominated": {
            "local_type": "host",
            "local": "192.168.0.109:54321",
            "remote_type": "relay",
            "remote": "203.0.113.7:3478",
            "transport": "udp",
        },
        "inbound_rtp": {"packets_received": 481, "jitter": 0.004},
    }


def test_the_addresses_are_gone():
    out = _redact_stream_health(_health())
    flat = repr(out)
    assert "192.168.0.109" not in flat
    assert "203.0.113.7" not in flat
    assert "54321" not in flat
    assert "3478" not in flat


def test_the_signal_that_matters_survives():
    """relay-vs-direct is why anyone reads this block."""
    out = _redact_stream_health(_health())
    nom = out["nominated"]
    assert nom["local_type"] == "host"
    assert nom["remote_type"] == "relay"
    assert nom["transport"] == "udp"
    assert out["inbound_rtp"]["packets_received"] == 481


def test_none_and_junk_pass_through_without_raising():
    """Diagnostics must never raise - a broken shape is not worth a 500."""
    assert _redact_stream_health(None) is None
    assert _redact_stream_health({"nominated": None})["nominated"] is None
    assert _redact_stream_health("unexpected") == "unexpected"


def test_redaction_does_not_mutate_the_live_session_stats():
    original = _health()
    _redact_stream_health(original)
    assert original["nominated"]["local"] == "192.168.0.109:54321", (
        "redaction must copy, not edit the object the session still owns"
    )
