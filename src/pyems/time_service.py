"""Root-owned systemd helper for PyEMS controller time operations.

The web UI writes a small, validated settings file as its normal service user.
This helper is the only code that changes the operating system clock; systemd
units expose only fixed actions, so the UI never gains arbitrary root command
execution.
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
from pathlib import Path

from pyems.time_sync import (
    DEFAULT_TIME_STATE_PATH,
    load_time_settings,
    ntp_probe,
    validate_manual_time,
)


def _run_checked(args: list[str]) -> None:
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except OSError as exc:
        raise ValueError(f"cannot run {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        raise ValueError(f"{' '.join(args)} failed (code {result.returncode}): {detail}")


def _disable_automatic_ntp() -> None:
    # PyEMS synchronizes at the configured daily time, so systemd-timesyncd
    # must not quietly adjust the clock at another time in between.
    _run_checked(["timedatectl", "set-ntp", "false"])


def _set_system_time(unix_time_s: float) -> str:
    value = dt.datetime.fromtimestamp(unix_time_s).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    _run_checked(["timedatectl", "set-time", value])
    return value


def apply_settings(state_path: str | Path) -> str:
    settings = load_time_settings(state_path)
    _disable_automatic_ntp()
    if settings["mode"] == "manual" and settings.get("manual_time"):
        value = validate_manual_time(settings["manual_time"])
        set_value = _set_system_time(value.timestamp())
        return f"manual controller time set to {set_value}"
    if settings["mode"] == "ntp":
        return f"scheduled NTP synchronization configured for {settings['sync_at']} via {settings['server']}"
    return "automatic NTP disabled; controller time is manual"


def synchronize(state_path: str | Path, only_if_due: bool) -> str:
    settings = load_time_settings(state_path)
    if settings["mode"] != "ntp":
        return "" if only_if_due else "scheduled NTP synchronization is disabled (manual time mode)"
    now = dt.datetime.now().astimezone()
    if only_if_due and now.strftime("%H:%M") != settings["sync_at"]:
        # The timer wakes once per minute. Keep the 1,439 no-op invocations out
        # of the journal; only a real synchronization should be operator-visible.
        return ""
    _disable_automatic_ntp()
    sample = ntp_probe(settings["server"])
    set_value = _set_system_time(float(sample["unix_time_s"]))
    return (
        f"controller synchronized with {sample['server']} (offset {sample['offset_ms']:+.1f} ms); "
        f"time set to {set_value}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Apply or synchronize PyEMS controller time.")
    parser.add_argument("--state", default=str(DEFAULT_TIME_STATE_PATH), help="time settings JSON")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true", help="apply manual/NTP mode")
    action.add_argument("--sync-now", action="store_true", help="synchronize from configured NTP server")
    action.add_argument("--sync-if-due", action="store_true", help="synchronize only at the scheduled minute")
    args = parser.parse_args(argv)
    if args.apply:
        message = apply_settings(args.state)
    else:
        message = synchronize(args.state, only_if_due=args.sync_if_due)
    if message:
        print(message)


if __name__ == "__main__":
    main()
