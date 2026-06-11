# Working with tags: vocabulary → profile → site → controllers

The practical workflow for the three name families and the order to work in.
Reference docs: `internal-tags.md` (sys.* tags, requesters, binding keys),
`src/pyems/device_fields.py` (the canonical field vocabulary).

## The mental model (read this first)

A tag is `<device id>.<field>`, e.g. `pv1.W`:

| Half | Comes from | Who decides it |
|---|---|---|
| `<field>` (`W`, `WSet`, `PhVphA`) | `src/pyems/device_fields.py` — ONE vocabulary for all device models | the EMS (semantic contract) |
| `<device id>` (`pv1`, `grid`) | `devices:` list in `config/site.yaml` | the site installer |

A profile (`profiles/*.yaml`) is the *bridge*: it maps one device model's
Modbus registers ONTO the vocabulary. The vendor doc contributes only
**address / type / gain**; the *meaning and SI unit* of every field is fixed
by the vocabulary, identically for every meter/inverter/genset. That is why
swapping a device model is a one-line `profile:` change in site.yaml and
zero changes anywhere else.

Never start from the vendor register map when naming things. Start from the
quantity ("this register is phase-A current") and look up its canonical name.

## Adding a NEW device profile — step by step

1. **Open the vocabulary** `src/pyems/device_fields.py`. These are the only
   field names a profile may use. Decide which quantities the device exposes
   that the EMS needs: at minimum the active power measurement `W`, and for a
   controllable unit the setpoint `WSet`. Everything else (per-phase power,
   currents, voltages, Hz, status words) is telemetry — add what the device
   has and the UI/recorder can use.

2. **For each chosen field, find the register in the vendor doc.** Note:
   - `address` and Modbus `type` (`int16`/`uint16`/`int32`/`uint32`);
   - the register's published unit and gain.

3. **Compute `scale`** — a MULTIPLIER on the raw register that yields the
   canonical SI unit (see the unit column in `device_fields.py`):

       value = raw * scale            # what the driver computes

   Vendor "gain" is usually a DIVISOR (`real = raw / gain`), so:

       scale = (canonical-unit factor) / gain

   Examples (Huawei SmartLogger meter block):
   | register | gain | published unit | canonical | scale |
   |---|---|---|---|---|
   | active power | 1000 | kW | W | `1` (raw is already W) |
   | current | 10 | A | A | `0.1` |
   | voltage | 100 | V | V | `0.01` |

   The unit in the profile must be EXACTLY the canonical one (`W`, never
   `kW`) — `DeviceProfile.load()` rejects anything else at startup.

4. **Write the YAML.** Channel class prefix = the device's generic class
   (`grid`, `pv`, `gen`, `bat`) — it is a placeholder that `namespaced()`
   replaces with the site's device id:

   ```yaml
   model: <human readable model name>
   protocol: modbus_tcp          # or modbus_rtu
   default_port: 502
   registers:
   - channel: grid.W             # <class>.<Field>, Field from the vocabulary
     address: 32278
     type: int32
     scale: 1
     unit: W                     # canonical SI unit, exactly
     access: read                # read | read_write (setpoints only)
     min_val: -10000000          # plausibility bounds on critical
     max_val: 10000000           # measurements: out-of-range fails the poll
   ```

   `access: read_write` ONLY for registers the EMS commands (e.g. `WSet`).
   Remember controllers never write them directly — the PowerAllocator does.

5. **Validate.** The test suite globs `profiles/` automatically:

       .venv/Scripts/python.exe -m pytest tests/test_device_fields.py -q

   A wrong field name, whitespace, or a non-canonical unit fails here (and
   at every EMS startup) with a message saying what to fix.

6. **Wire it into a site** (`config/site.yaml`):

   ```yaml
   devices:
   - id: grid                    # becomes the tag namespace: grid.W
     profile: meters/<your>.yaml
     host: 192.168.1.50
     slave_id: 1
   ```

   If the device is a controlled unit, also give its setpoint channel an
   `allocation.channels` entry (envelope, ramp, deadband) and list it under
   `safety.unit_active_power_setpoint_channels`.

7. **Inspect the result** — the live cross-reference of every tag, its
   origin, readers and writers:

       pyems-tags --site config/site.yaml

## Editing an EXISTING profile

- **Allowed freely:** `address`, `type`, `scale`, `min_val`/`max_val`,
  adding registers for vocabulary fields the profile didn't expose yet,
  removing telemetry registers nothing binds to.
- **Never casually:** renaming a `channel:` field. The field names are the
  contract site.yaml bindings rely on — renaming breaks every site using
  the profile. If a field name is wrong, fix it everywhere in one change
  (profile + all site.yaml bindings) and say so in the commit.
- **Changing `access`** read↔read_write changes the driver's poll/flush
  classification; startup direction validation will catch a mismatch with
  the bindings, but treat it as a semantic change, not a tweak.

## The quantity is NOT in the vocabulary yet

Add it ONCE in `src/pyems/device_fields.py`: pick a name in the SunSpec
style of the existing entries, set its canonical SI unit, add a one-line
comment for what it means. Naming rules from CLAUDE.md apply (explicit
electrical quantity, unit-suffixed where ambiguous). Tests introspect the
dict — no test edits. Then use it in profiles like any other field.

## EMS-internal names (not device tags)

`sys.*` status words and requester names live in `src/pyems/system_tags.py`
— see the checklist in `internal-tags.md`. Don't add them to the device
vocabulary; they are status words the EMS produces, not registers.

## Quick reference: where does each name live?

| Name | Example | Defined in |
|---|---|---|
| field | `W`, `WSet`, `PhVphA` | `src/pyems/device_fields.py` |
| device id | `pv1`, `grid` | `config/site.yaml` `devices:` |
| tag | `pv1.W` | derived: id + field (`namespaced()`) |
| system tag | `sys.safe_mode` | `src/pyems/system_tags.py` |
| requester | `export_limit` | `src/pyems/system_tags.py` |
| binding key | `unit_active_power_channel` | site.yaml keys, table in `internal-tags.md` |
