# Deploying PyEMS on a Raspberry Pi

Target: Raspberry Pi OS (Bookworm or newer — Python >= 3.10 required; check
with `python3 --version`). The EMS runs as a systemd service so it starts on
boot, restarts on crash, and logs to the journal.

## 1. Install

```bash
sudo apt update && sudo apt install -y git python3-venv
git clone <repo-url> /home/pi/PyEMS
cd /home/pi/PyEMS
python3 -m venv .venv
.venv/bin/pip install -e .
```

The editable install is required, not optional: `profiles/` and `config/`
are resolved relative to the repo checkout (see `ROOT` in
`src/pyems/ems.py`), and the UI's static files ship with the source tree.

## 2. Commission config/site.yaml

The checked-in `config/site.yaml` carries development values. Before first
start, set the real ones:

- `devices[*].host` / `slave_id` — actual meter and inverter addresses
- `export_limit.limit_w` — the real export limit at the connection point
- `allocation.channels[*]` — the unit's true envelope and gradients
- `safety.device_comms_watchdog_s` — the watchdog period the unit was
  commissioned with (startup validation depends on it)

Smoke-test in the foreground before installing the service:

```bash
.venv/bin/pyems            # Ctrl-C to stop
```

A binding typo or a safety/allocation mismatch fails here, at startup,
by design.

## 3. Install the service

```bash
sudo cp deploy/pyems.service /etc/systemd/system/pyems.service
sudo systemctl daemon-reload
sudo systemctl enable --now pyems
```

If the repo is not at `/home/pi/PyEMS` or the user is not `pi`, edit
`User=`, `WorkingDirectory=` and `ExecStart=` in the unit first.

## 4. Operate

```bash
systemctl status pyems          # is it running, last log lines
journalctl -u pyems -f          # follow the log live
journalctl -u pyems --since today
sudo systemctl restart pyems    # e.g. after editing site.yaml
```

Per-cycle DEBUG detail: uncomment the `Environment=PYEMS_LOG_LEVEL=DEBUG`
line in the unit, then `daemon-reload` + `restart`.

## 5. Web UI (status, generation control, log viewer)

The EMS ships a local web UI as a *separate* process (`pyems-ui`). It reads the
live telemetry snapshot and the EMS log off disk and writes the operator command
file — it never touches the Modbus bus, so it cannot stall the control loop.

```bash
sudo cp deploy/pyems-ui.service /etc/systemd/system/pyems-ui.service
sudo systemctl daemon-reload
sudo systemctl enable --now pyems-ui
```

The UI binds `127.0.0.1:8765` and has **no authentication**, so reach it over an
SSH tunnel from your laptop:

```bash
ssh -L 8765:127.0.0.1:8765 pi@<pi>   # then open http://localhost:8765/
```

Only switch the unit to `--host 0.0.0.0` on a trusted, isolated LAN — that
exposes config edits and generation start/stop to anyone who can reach the port.

What you get without SSH/journalctl:

- **Overview** — live power flows, per-device comms status, and *why* safe-mode
  is (not) green right now.
- **Logs** — the EMS control-loop log (safety trips, bus up/down, write
  failures), filterable by level. This is the rotating file at `logging.file`
  in site.yaml (default `logs/pyems.log`); `journalctl -u pyems` remains the
  fallback for the full system journal.
- **Operation** — start/stop generation (soft curtail) and the hard inverter
  switch, when configured.

The systemd-managed EMS is still started/stopped with `systemctl` (the UI does
not manage it). Config edited in the UI is read at EMS startup, so apply changes
with `sudo systemctl restart pyems`.

## 6. Clock / time sync (the Pi has no RTC)

The control loop times itself with the **monotonic** clock, so it is correct
regardless of the wall clock. But the **wall clock** drives operator-command
freshness and every log/telemetry/CSV timestamp — and a Raspberry Pi has **no
battery-backed RTC**, so at boot it may read a stale or epoch-near time until
NTP syncs. The EMS logs a WARNING at startup if the clock looks unsynced, and
the units order themselves `After=time-sync.target`.

For a clock that is correct immediately at boot (e.g. offline sites, or to make
post-mortem timestamps trustworthy):

```bash
sudo apt install -y chrony            # keep the clock disciplined when online
# Offline between boots? add fake-hwclock to restore the last-known time:
sudo apt install -y fake-hwclock
# Best: fit a hardware RTC module (DS3231) and enable its kernel overlay.
```

## 7. Update

```bash
cd /home/pi/PyEMS
git pull
.venv/bin/pip install -e .     # only needed if dependencies changed
sudo systemctl restart pyems
```
