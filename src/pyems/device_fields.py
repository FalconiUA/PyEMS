"""THE single place for canonical device field names — the profile vocabulary.

A tag is `<device id>.<field>`: the `<device id>` half comes from site.yaml,
the `<field>` half MUST come from this module. Field names follow SunSpec
Modbus point names where SunSpec covers the quantity (W, VAR, Hz, AphA,
PhVphA, ...) plus EMS-specific additions (WSet; vendor status words).

Vendor register-map display names ("Phase A active power") are NEVER channel
names. A profile maps vendor registers ONTO this vocabulary: the vendor doc
contributes only address/type/scale; the *meaning and unit* of every field is
fixed here, identically for every device model — that is what lets a site
swap one meter/inverter model for another with a one-line `profile:` change
and zero binding edits.

`DeviceProfile.load()` enforces the vocabulary: an unknown field, a malformed
channel name or a non-canonical unit fails at profile load, not on live
hardware. Values are SI base units so controllers never see kW/kVar — a
register published in scaled units is converted with the profile's `scale`
(a MULTIPLIER on the raw register; note vendor "gain" is usually a divisor).

To add a new quantity: add it HERE once, with its canonical SI unit and a
comment saying what it means. Every profile and tool then accepts it;
tests/test_device_fields.py introspects this dict, so no test edits needed.
"""

# field -> canonical unit ('' = dimensionless vendor status/enum word)
DEVICE_FIELDS: dict[str, str] = {
    # active power P (generating convention: + = injection into the AC bus)
    "W": "W",
    "WphA": "W",
    "WphB": "W",
    "WphC": "W",
    # active power setpoint (absolute W) — written ONLY by the PowerAllocator
    "WSet": "W",
    # reactive power Q
    "VAR": "VAr",
    "VARphA": "VAr",
    "VARphB": "VAr",
    "VARphC": "VAr",
    # apparent power S
    "VA": "VA",
    "VAphA": "VA",
    "VAphB": "VA",
    "VAphC": "VA",
    # frequency f
    "Hz": "Hz",
    # phase current I
    "AphA": "A",
    "AphB": "A",
    "AphC": "A",
    # phase-to-neutral voltage U
    "PhVphA": "V",
    "PhVphB": "V",
    "PhVphC": "V",
    # phase-to-phase (line) voltage U
    "PPVphAB": "V",
    "PPVphBC": "V",
    "PPVphCA": "V",
    # vendor status/enum words (raw register value, device-specific meaning)
    "Status": "",
    "OperatingMode": "",
    "Alarm": "",
}


def validate_channel(channel: str, unit: str) -> None:
    """Raise ValueError unless `channel` is `<class>.<Field>` with a
    vocabulary field and the canonical unit. Called by DeviceProfile.load()
    for every register, so a profile authored from a vendor register map
    fails at load — not after feeding mis-scaled values to the control loop.
    """
    cls, dot, field_name = channel.partition(".")
    if not dot or not cls or not field_name or any(c.isspace() for c in channel):
        raise ValueError(
            f"channel {channel!r} must be '<class>.<Field>' with no whitespace"
        )
    canonical_unit = DEVICE_FIELDS.get(field_name)
    if canonical_unit is None:
        raise ValueError(
            f"channel {channel!r}: unknown field {field_name!r} — channel names "
            f"come from the canonical vocabulary, not the vendor register map. "
            f"Known fields: {sorted(DEVICE_FIELDS)}. A genuinely new quantity "
            f"is added once in src/pyems/device_fields.py."
        )
    if unit != canonical_unit:
        raise ValueError(
            f"channel {channel!r}: unit must be {canonical_unit!r} (canonical "
            f"SI), got {unit!r} — convert scaled registers with the profile's "
            f"`scale` (multiplier), never by changing the unit"
        )
