#!/usr/bin/env bash
#
# PyEMS one-command installer for Raspberry Pi (Raspberry Pi OS Bookworm+).
#
# Installs the two-process, industrial-controller setup:
#   * pyems-ui.service  — the HMI/console, always on, reachable on the LAN
#   * pyems.service     — the EMS control-loop runtime, supervised separately
#   * controller-clock helpers — scheduled/manual time independent of EMS
#   * a polkit rule     — lets the HMI run only its fixed privileged actions
#
# After this, do everything else from the browser: open the printed URL,
# configure the site, then press RUN. The EMS runs as its own supervised
# process; the UI only commands it (like an HMI driving a PLC).
#
# Usage (from the cloned repo):
#   bash install.sh
#
set -euo pipefail

# ── Resolve who/where ────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
VENV="$REPO_DIR/.venv"
UI_PORT=8765
TIME_STATE=$REPO_DIR/logs/time-settings.json
TIME_STATUS=$REPO_DIR/logs/time-sync-status.json

if [ "$RUN_USER" = "root" ]; then
    echo "ERROR: run this as the normal Pi user (e.g. 'pi'), not as root." >&2
    echo "       The services must not run as root. Try:  bash install.sh" >&2
    exit 1
fi

# Run a command as root (via sudo) / as the service user, regardless of how the
# script itself was invoked.
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
as_user() {
    if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
        sudo -u "$RUN_USER" "$@"
    else
        "$@"
    fi
}

echo "PyEMS installer"
echo "  repo:    $REPO_DIR"
echo "  user:    $RUN_USER"
echo

# ── 1. Python >= 3.10 (Bookworm ships 3.11; Bullseye's 3.9 is too old) ───────
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Raspberry Pi OS Bookworm or newer." >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    have="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    echo "ERROR: Python >= 3.10 required, found $have." >&2
    echo "       Use Raspberry Pi OS Bookworm or newer (Bullseye's 3.9 won't work)." >&2
    exit 1
fi
echo "Python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])') OK"

# ── 2. System packages: git, venv tooling, and polkit (for RUN/STOP) ─────────
if command -v apt-get >/dev/null 2>&1; then
    echo "Installing system packages (git, python3-venv, polkit)…"
    as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-venv
    # polkit daemon: 'polkitd' on Bookworm, 'policykit-1' on older releases.
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq polkitd \
        || as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq policykit-1 \
        || echo "WARNING: could not install polkit; UI RUN/STOP may need a password."
else
    echo "WARNING: apt-get not found; skipping system packages (ensure git, "
    echo "         python3-venv and polkit are present)."
fi

# ── 3. Virtualenv + editable install (data dirs resolve from the checkout) ───
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtualenv at $VENV…"
    as_user python3 -m venv "$VENV"
fi
echo "Installing PyEMS into the virtualenv…"
as_user "$VENV/bin/python" -m pip install --upgrade -q pip
as_user "$VENV/bin/python" -m pip install -q -e "$REPO_DIR"

# ── 4. systemd units (generated with the real user/paths) ────────────────────
echo "Installing systemd units…"
tmp_ems="$(mktemp)"
cat >"$tmp_ems" <<EOF
[Unit]
Description=PyEMS — EMS control-loop runtime (Modbus TCP/RTU)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$VENV/bin/pyems
# A crash self-heals; a clean operator STOP (from the UI) stays stopped.
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
as_root install -m 0644 "$tmp_ems" /etc/systemd/system/pyems.service
rm -f "$tmp_ems"

tmp_ui="$(mktemp)"
cat >"$tmp_ui" <<EOF
[Unit]
Description=PyEMS UI / HMI (configuration + RUN/STOP console)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$VENV/bin/pyems-ui --host 0.0.0.0 --ems-unit pyems
Restart=on-failure
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
as_root install -m 0644 "$tmp_ui" /etc/systemd/system/pyems-ui.service
rm -f "$tmp_ui"

echo "Installing controller-clock helper units…"

tmp_time_apply="$(mktemp)"
cat >"$tmp_time_apply" <<EOF
[Unit]
Description=PyEMS apply controller time settings

[Service]
Type=oneshot
User=root
ExecStart=$VENV/bin/pyems-time-helper --state $TIME_STATE --status $TIME_STATUS --apply
EOF
as_root install -m 0644 "$tmp_time_apply" /etc/systemd/system/pyems-time-apply.service
rm -f "$tmp_time_apply"

tmp_time_manual="$(mktemp)"
cat >"$tmp_time_manual" <<EOF
[Unit]
Description=PyEMS set controller time manually

[Service]
Type=oneshot
User=root
ExecStart=$VENV/bin/pyems-time-helper --state $TIME_STATE --status $TIME_STATUS --set-manual-time
EOF
as_root install -m 0644 "$tmp_time_manual" /etc/systemd/system/pyems-time-manual.service
rm -f "$tmp_time_manual"

tmp_time_sync_now="$(mktemp)"
cat >"$tmp_time_sync_now" <<EOF
[Unit]
Description=PyEMS synchronize controller time now
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=$VENV/bin/pyems-time-helper --state $TIME_STATE --status $TIME_STATUS --sync-now
EOF
as_root install -m 0644 "$tmp_time_sync_now" /etc/systemd/system/pyems-time-sync-now.service
rm -f "$tmp_time_sync_now"

tmp_time_sync="$(mktemp)"
cat >"$tmp_time_sync" <<EOF
[Unit]
Description=PyEMS scheduled controller time synchronization
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=$VENV/bin/pyems-time-helper --state $TIME_STATE --status $TIME_STATUS --sync-if-due
EOF
as_root install -m 0644 "$tmp_time_sync" /etc/systemd/system/pyems-time-sync.service
rm -f "$tmp_time_sync"

tmp_time_timer="$(mktemp)"
cat >"$tmp_time_timer" <<EOF
[Unit]
Description=PyEMS controller time synchronization schedule

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s
Unit=pyems-time-sync.service

[Install]
WantedBy=timers.target
EOF
as_root install -m 0644 "$tmp_time_timer" /etc/systemd/system/pyems-time-schedule.timer
rm -f "$tmp_time_timer"

# ── 5. polkit rule: fixed HMI RUN/STOP, time, and network actions ───────────
echo "Installing polkit rule (RUN/STOP + time + network, without a password)…"
tmp_rule="$(mktemp)"
cat >"$tmp_rule" <<EOF
// Generated by install.sh — grant '$RUN_USER' only fixed PyEMS operations:
//   1. start/stop/restart pyems.service
//   2. apply/sync controller time through fixed helper units
//   3. set the Pi IP via NetworkManager.
polkit.addRule(function(action, subject) {
    if (subject.user != "$RUN_USER") {
        return polkit.Result.NOT_HANDLED;
    }
    if (action.id == "org.freedesktop.systemd1.manage-units") {
        var unit = action.lookup("unit");
        if (unit == "pyems.service" ||
            unit == "pyems-time-apply.service" ||
            unit == "pyems-time-manual.service" ||
            unit == "pyems-time-sync-now.service") {
            var verb = action.lookup("verb");
            if (verb == "start" || verb == "stop" || verb == "restart") {
                return polkit.Result.YES;
            }
        }
    }
    if (action.id == "org.freedesktop.NetworkManager.settings.modify.system" ||
        action.id == "org.freedesktop.NetworkManager.network-control") {
        return polkit.Result.YES;
    }
    return polkit.Result.NOT_HANDLED;
});
EOF
as_root install -d -m 0755 /etc/polkit-1/rules.d
as_root install -m 0644 "$tmp_rule" /etc/polkit-1/rules.d/49-pyems.rules
rm -f "$tmp_rule"

# ── 6. Enable the HMI on boot; leave the EMS runtime under UI control ─────────
as_root systemctl daemon-reload
as_root systemctl enable --now pyems-ui
as_root systemctl enable --now pyems-time-schedule.timer
# Reload the HMI code/static pages on both a first install and an update. This
# does not affect the separately supervised EMS control-loop process.
as_root systemctl restart pyems-ui
# pyems.service is intentionally NOT enabled: RUN/STOP is driven from the UI.

# ── Done ─────────────────────────────────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="$(hostname).local"
echo
echo "✅ PyEMS installed."
echo
echo "   Open the console:   http://$IP:$UI_PORT"
echo "   Then: configure the site, press RUN to start the EMS, enable generation."
echo
echo "   HMI logs:   journalctl -u pyems-ui -f"
echo "   EMS logs:   journalctl -u pyems -f"
