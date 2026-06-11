"""THE single place for canonical device field names — the profile vocabulary.

A tag is `<device id>.<field>`: the `<device id>` half comes from site.yaml,
the `<field>` half MUST come from this module.

Provenance — two explicitly separated families:

  - SUNSPEC_FIELDS: names and units taken verbatim from the SunSpec Modbus
    information models. The authoritative model files are checked into this
    repo at documents/SunSpec/model_103.json (three-phase inverter) and
    model_203.json (three-phase wye meter); tests/test_device_fields.py
    cross-checks every name and unit against them, so this dict provably
    cannot drift from the standard.
  - EMS_FIELDS: quantities the EMS needs that SunSpec does not model the way
    we use them (an absolute active-power setpoint in W; vendor status
    words). Tests assert these never shadow a SunSpec point name.

Vendor register-map display names ("Phase A active power") are NEVER channel
names. A profile maps vendor registers ONTO this vocabulary: the vendor doc
contributes only address/type/scale; the *meaning and unit* of every field is
fixed here, identically for every device model — that is what lets a site
swap one meter/inverter model for another with a one-line `profile:` change
and zero binding edits.

`DeviceProfile.load()` enforces the vocabulary: an unknown field, a malformed
channel name or a non-canonical unit fails at profile load, not on live
hardware. Values are SI base units so controllers never see kW/kvar — a
register published in scaled units is converted with the profile's `scale`
(a MULTIPLIER on the raw register; note vendor "gain" is usually a divisor).

To add a new quantity: if SunSpec models it, use the SunSpec point name and
unit (check the model JSONs) and add it to SUNSPEC_FIELDS; otherwise add it
to EMS_FIELDS with its canonical SI unit and a comment saying what it means.
Tests introspect both dicts — no test edits needed.
"""

# field -> canonical unit, verbatim from the SunSpec models (see docstring)
SUNSPEC_FIELDS: dict[str, str] = {
    # active power P (generating convention: + = injection into the AC bus)
    "W": "W",
    "WphA": "W",
    "WphB": "W",
    "WphC": "W",
    # reactive power Q (IEC symbol: var)
    "VAR": "var",
    "VARphA": "var",
    "VARphB": "var",
    "VARphC": "var",
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
    # storage (model 802, Battery Base Model) — starter set; extend from the
    # model JSON as storage scenarios need more points
    "SoC": "%WHRtg",        # state of charge, % of nameplate energy capacity
    "SoH": "%",             # state of health
    "WChaRteMax": "W",      # nameplate max charge rate
    "WDisChaRteMax": "W",   # nameplate max discharge rate
}

# EMS-specific fields ('' = dimensionless vendor status/enum word)
EMS_FIELDS: dict[str, str] = {
    # active power setpoint (absolute W) — written ONLY by the PowerAllocator.
    # SunSpec models limiting as WMaxLimPct (percent, model 123); our devices
    # take an absolute W command, hence an EMS name.
    "WSet": "W",
    # vendor status/enum words (raw register value, device-specific meaning)
    "Status": "",
    "OperatingMode": "",
    "Alarm": "",
}

DEVICE_FIELDS: dict[str, str] = {**SUNSPEC_FIELDS, **EMS_FIELDS}

# field -> human-readable meaning, shown next to raw tags in the UI and docs.
# Wording follows the project's grid-code terminology (CLAUDE.md): the
# electrical quantity is always explicit. Covers every DEVICE_FIELDS key —
# enforced by tests/test_device_fields.py.
FIELD_LABELS: dict[str, str] = {
    "W": "Active power, total (P)",
    "WphA": "Active power, phase A",
    "WphB": "Active power, phase B",
    "WphC": "Active power, phase C",
    "WSet": "Active power setpoint (written by PowerAllocator)",
    "VAR": "Reactive power, total (Q)",
    "VARphA": "Reactive power, phase A",
    "VARphB": "Reactive power, phase B",
    "VARphC": "Reactive power, phase C",
    "VA": "Apparent power, total (S)",
    "VAphA": "Apparent power, phase A",
    "VAphB": "Apparent power, phase B",
    "VAphC": "Apparent power, phase C",
    "Hz": "Frequency (f)",
    "AphA": "Current, phase A",
    "AphB": "Current, phase B",
    "AphC": "Current, phase C",
    "PhVphA": "Voltage, phase A to neutral",
    "PhVphB": "Voltage, phase B to neutral",
    "PhVphC": "Voltage, phase C to neutral",
    "PPVphAB": "Voltage, line A-B",
    "PPVphBC": "Voltage, line B-C",
    "PPVphCA": "Voltage, line C-A",
    "SoC": "State of charge (% of energy capacity)",
    "SoH": "State of health",
    "WChaRteMax": "Max charge rate, nameplate",
    "WDisChaRteMax": "Max discharge rate, nameplate",
    "Status": "Vendor status word",
    "OperatingMode": "Vendor operating mode",
    "Alarm": "Vendor alarm bitfield",
}


def field_label(channel: str) -> str:
    """Human-readable meaning of a `<device>.<field>` tag ('' if unknown)."""
    field = channel.split(".", 1)[1] if "." in channel else channel
    return FIELD_LABELS.get(field, "")


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
