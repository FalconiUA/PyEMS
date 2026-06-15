# Deploying PyEMS on a Raspberry Pi

Target: Raspberry Pi 3 or newer, Raspberry Pi OS **Bookworm or newer** (needs
Python >= 3.10 — check with `python3 --version`). PyEMS runs as **two separate,
systemd-supervised processes**, the industrial-controller model:

| Process         | Role                             | Unit               | Enabled on boot |
|-----------------|----------------------------------|--------------------|-----------------|
| EMS runtime     | the control loop (≈ a PLC CPU)   | `pyems.service`    | no              |
| UI / HMI        | configuration + RUN/STOP console | `pyems-ui.service` | yes             |

The HMI is always on; it runs no control logic. Its **RUN/STOP** buttons issue
`systemctl start/stop pyems` (granted by a polkit rule), so systemd — not the UI
— owns the control-loop process. The EMS survives a UI restart, a crash
self-heals, and a clean operator STOP stays stopped.

## 1. Install (one command)

```bash
sudo apt update && sudo apt install -y git          # if git is missing
git clone <repo-url> /home/pi/PyEMS
cd /home/pi/PyEMS
bash install.sh
```

`install.sh` is idempotent and does the rest: checks Python >= 3.10, installs
`python3-venv` and `polkit`, creates the virtualenv, `pip install -e .`,
generates the two systemd units and the polkit rule **with your real user and
paths**, enables the HMI on boot, and prints the console URL.

The editable install is required, not optional: `profiles/` and `config/` are
resolved relative to the repo checkout (see `ROOT` in `src/pyems/ems.py`), and
the UI's static files ship with the source tree — so keep the cloned repo in
place.

## 2. Configure and run — from the browser

Open the printed URL (e.g. `http://192.168.1.50:8765`) from any machine on the
LAN. In the UI:

1. Set the devices (host/slave id), profiles, the export limit, the unit
   envelope/gradients, and `safety.device_comms_watchdog_s`. Use **Test read**
   to confirm the bus before going live.
2. Press **RUN** to start the EMS runtime. A binding typo or a safety/allocation
   mismatch fails at startup by design — the UI surfaces it (and
   `journalctl -u pyems -e` has the detail).
3. Enable **generation** (it always starts disabled, fail-closed).

There is no separate "edit site.yaml over SSH" step; the UI writes it.

## 3. Manual install (without install.sh)

The committed reference files under `deploy/` use placeholders (`pi`,
`/home/pi/PyEMS`) — edit `User=`, `WorkingDirectory=`, `ExecStart=` and the
polkit `subject.user` to match yours, then:

```bash
sudo cp deploy/pyems.service     /etc/systemd/system/pyems.service
sudo cp deploy/pyems-ui.service  /etc/systemd/system/pyems-ui.service
sudo cp deploy/pyems-polkit.rules /etc/polkit-1/rules.d/49-pyems.rules
sudo systemctl daemon-reload
sudo systemctl enable --now pyems-ui     # HMI on boot; do NOT enable pyems
```

## 4. Operate

```bash
systemctl status pyems pyems-ui   # are they running, last log lines
journalctl -u pyems -f            # follow the control loop live
journalctl -u pyems-ui -f         # follow the HMI live
```

RUN/STOP and generation are normally driven from the UI. From the shell they map
to `sudo systemctl start pyems` / `sudo systemctl stop pyems`.

Per-cycle DEBUG detail: add `Environment=PYEMS_LOG_LEVEL=DEBUG` to
`pyems.service`, then `daemon-reload` + `restart`.

For retentive RUN across reboots once commissioned: `sudo systemctl enable pyems`.

## 5. Update

```bash
cd /home/pi/PyEMS
git pull
bash install.sh                 # re-runs the editable install; idempotent
sudo systemctl restart pyems-ui
```
