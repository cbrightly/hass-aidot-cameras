"""What a camera MODEL declares in its cloud profile, and gating controls on it.

The vendor app shows a setting only when the model's profile declares the
property behind it (its ``LedOnOffConfig``, ``VoiceEnableConfig``,
``WDREnableConfig`` and so on are each "the profile has this property"). The
cloud's reported properties are no substitute: they carry defaults for keys a
model never implemented, and this firmware acknowledges writes to those and
does nothing. So a control is gated on the declaration, the way the app does.

Conservative by design: an empty declaration means the profile was absent or
unreadable, which is unknown rather than "declares nothing", and every control
is kept.
"""

from __future__ import annotations


def declares(client, name: str) -> bool:
    """Does this MODEL declare the property in its cloud profile?

    True when the profile is unreadable, because an empty set means unknown
    rather than "declares nothing" -- a camera whose profile failed to parse
    should not quietly lose a control. Applied to every description with a
    ``profile_property`` by ``entity.described_entities``, which also removes
    the registry row an earlier release made for an undeclared one.
    """
    declared = client.info.declared_properties
    return not declared or name in declared
