# PyEMS

Local energy management system controlling energy sources over Modbus. The
control logic is the core; Modbus is an edge adapter. Architecture follows
IEC 61131-3 (RESOURCE / TASK / FUNCTION_BLOCK) and grid-code terminology
(EN 50549, ENTSO-E NC RfG). See [CLAUDE.md](CLAUDE.md) for conventions.

## Install on a Raspberry Pi (one command)

Target: **Raspberry Pi 3 or newer**, **Raspberry Pi OS Bookworm or newer**
(needs Python ≥ 3.10 — Bookworm ships 3.11; the older Bullseye's 3.9 will not
work). Then:

```bash
git clone <repo-url> ~/PyEMS
cd ~/PyEMS
bash install.sh
```

`install.sh` checks the Python version, creates a virtualenv, installs PyEMS,
sets up the HMI/EMS services, the independent controller-clock scheduler and
the polkit rule, then starts the HMI. When it finishes it prints a URL like
`http://192.168.1.50:8765`.

**Everything else is done from the browser** — open that URL, configure the
site (devices, profiles, limits, safety), press **RUN**, then enable generation.

## How it runs — the industrial-controller model

Two separate, OS-supervised processes, exactly like an HMI driving a PLC:

| Process            | Role                              | systemd unit       | On boot |
|--------------------|-----------------------------------|--------------------|---------|
| **EMS runtime**    | the control loop (≈ a PLC CPU)    | `pyems.service`    | stopped |
| **UI / HMI**       | configuration + RUN/STOP console  | `pyems-ui.service` | running |

- The **HMI is always on** and reachable on the LAN. It runs no control logic.
- The **controller clock** is an operating-system responsibility, not EMS
  telemetry. The **Time** tab can set it manually or synchronize with one NTP
  server daily at a selected local time. It also separates the geographical
  IANA time zone from the seasonal-clock policy: automatic DST follows the
  zone's rules, while a fixed UTC offset disables summer/winter changes. Its
  systemd timer continues to run while the EMS runtime is stopped; changing
  time does not restart the Pi, EMS, or HMI.
- The **EMS runtime** is a separate supervised process. The HMI's **RUN/STOP**
  buttons issue `systemctl start/stop pyems` (granted by a narrow polkit rule —
  see [`deploy/pyems-polkit.rules`](deploy/pyems-polkit.rules)); systemd, not
  the UI, owns the process. So the EMS survives a UI restart, a crash self-heals
  (`Restart=on-failure`), and a clean operator STOP stays stopped.
- Two control levels, like a PLC: **RUN/STOP** is the process (CPU) level;
  the optional **generation gate** is the finer "output enable" (contactor)
  level — generation always starts disabled (fail-closed) until you enable it.

For retentive RUN across reboots once commissioned: `sudo systemctl enable pyems`.

## Local development (no hardware)

One command brings up a device simulator, the EMS (against the simulation site)
and the UI together — no Pi, no Modbus hardware:

```bash
.venv/Scripts/python.exe -m pip install -e .[dev]   # Windows; use .venv/bin on POSIX
pyems-dev                                            # then open http://127.0.0.1:8765
```

Tests: `.venv/Scripts/python.exe -m pytest`.

## More

- Field deployment / operation details: [deploy/DEPLOY.md](deploy/DEPLOY.md)
- Project conventions and naming: [CLAUDE.md](CLAUDE.md)
