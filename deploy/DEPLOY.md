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

## 5. Update

```bash
cd /home/pi/PyEMS
git pull
.venv/bin/pip install -e .     # only needed if dependencies changed
sudo systemctl restart pyems
```
