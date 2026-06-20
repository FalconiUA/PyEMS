"""Small, dependency-free primitives for the controller's system clock.

The EMS loop must never be responsible for keeping wall-clock time.  These
helpers are used by the UI and by the root-owned systemd helper service instead
of tying timekeeping to the control process.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import re
import socket
import struct
import tempfile
import time
from pathlib import Path
from typing import Any


# The HMI runs as the normal PyEMS service user. Store its state beside the
# existing telemetry/command files, not in /var/lib (which is normally root
# owned and caused configuration saves to fail on deployed controllers).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIME_STATE_PATH = PROJECT_ROOT / "logs" / "time-settings.json"
DEFAULT_TIME_STATUS_PATH = PROJECT_ROOT / "logs" / "time-sync-status.json"
NTP_PORT = 123
NTP_EPOCH_OFFSET_S = 2_208_988_800
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)
_SYNC_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d$")
_TIMEZONE_RE = re.compile(
    r"(?:UTC|[A-Za-z][A-Za-z0-9_.+-]*(?:/[A-Za-z][A-Za-z0-9_.+-]*)+)$"
)
_FIXED_TIMEZONE_RE = re.compile(r"Etc/(?:UTC|GMT[+-](?:[1-9]|1[0-4]))$")


def validate_ntp_server(value: object) -> str:
    """Return a safe NTP host name or IP address; reject shell/config injection."""
    server = str(value or "").strip()
    if not server:
        raise ValueError("NTP server is required")
    if any(ch.isspace() for ch in server):
        raise ValueError(
            f"NTP server {server!r} contains spaces; enter only a host name such as ua.pool.ntp.org"
        )
    if "/" in server or "\\" in server:
        raise ValueError("NTP server must be a host name or IP address, without a port")
    try:
        return str(ipaddress.ip_address(server))
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(server):
        raise ValueError(f"invalid NTP server: {server!r}")
    return server.rstrip(".").lower()


def validate_sync_time(value: object) -> str:
    sync_at = str(value or "").strip()
    if not _SYNC_TIME_RE.fullmatch(sync_at):
        raise ValueError("daily sync time must be HH:MM (00:00–23:59)")
    return sync_at


def validate_manual_time(value: object) -> dt.datetime:
    """Parse the value produced by ``<input type=datetime-local>``."""
    raw = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    raise ValueError("manual time must be YYYY-MM-DD HH:MM (optional :SS)")


def validate_timezone(value: object) -> str:
    """Validate an IANA time-zone identifier without accepting path traversal."""
    timezone = str(value or "").strip()
    if not _TIMEZONE_RE.fullmatch(timezone) or ".." in timezone:
        raise ValueError("time zone must be an IANA name, for example Europe/Kyiv")
    return timezone


def fixed_timezone_options() -> list[dict[str, str]]:
    """UTC offsets represented by IANA Etc/GMT zones (which never observe DST)."""
    options = []
    for offset in range(-12, 15):
        if offset == 0:
            zone = "Etc/UTC"
        elif offset > 0:
            # IANA Etc/GMT signs are intentionally the inverse of UTC offsets.
            zone = f"Etc/GMT-{offset}"
        else:
            zone = f"Etc/GMT+{-offset}"
        sign = "+" if offset >= 0 else ""
        options.append({"id": zone, "label": f"UTC{sign}{offset:02d}:00 — fixed, no DST"})
    return options


def available_timezones() -> list[str]:
    """Return IANA zones installed on Linux, with a useful dev-machine fallback."""
    for path in (Path("/usr/share/zoneinfo/zone.tab"), Path("/usr/share/zoneinfo/zone1970.tab")):
        try:
            zones = sorted(
                {
                    parts[2]
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line and not line.startswith("#")
                    for parts in [line.split("\t")]
                    if len(parts) >= 3
                }
            )
        except OSError:
            continue
        if zones:
            return ["UTC", "Etc/UTC", *zones]
    return [
        "UTC",
        "Etc/UTC",
        "Europe/Kyiv",
        "Europe/Warsaw",
        "Europe/Berlin",
        "Europe/London",
        "America/New_York",
        "Asia/Tokyo",
    ]


def normalize_timezone_policy(value: dict[str, Any]) -> dict[str, str]:
    """Validate the DST policy stored together with the clock source settings."""
    result: dict[str, str] = {}
    raw_timezone = value.get("timezone")
    if raw_timezone not in (None, ""):
        result["timezone"] = validate_timezone(raw_timezone)
    dst_mode = str(value.get("dst_mode", "automatic"))
    if dst_mode not in ("automatic", "fixed"):
        raise ValueError("DST mode must be 'automatic' or 'fixed'")
    result["dst_mode"] = dst_mode
    if dst_mode == "fixed":
        fixed_timezone = validate_timezone(value.get("fixed_timezone"))
        if not _FIXED_TIMEZONE_RE.fullmatch(fixed_timezone):
            raise ValueError("fixed DST mode requires a fixed UTC offset")
        result["fixed_timezone"] = fixed_timezone
    return result


def effective_timezone(settings: dict[str, str]) -> str | None:
    if settings.get("dst_mode") == "fixed":
        return settings.get("fixed_timezone")
    return settings.get("timezone")


def normalize_time_settings(value: object) -> dict[str, str]:
    """Validate the unprivileged settings file before a root helper reads it."""
    if not isinstance(value, dict):
        raise ValueError("time settings must be a JSON object")
    policy = normalize_timezone_policy(value)
    mode = str(value.get("mode", "manual"))
    if mode == "ntp":
        return {
            "mode": "ntp",
            "server": validate_ntp_server(value.get("server")),
            "sync_at": validate_sync_time(value.get("sync_at")),
            **policy,
        }
    if mode == "manual":
        result = {"mode": "manual", **policy}
        manual_time = value.get("manual_time")
        if manual_time not in (None, ""):
            result["manual_time"] = validate_manual_time(manual_time).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        return result
    raise ValueError("time mode must be 'manual' or 'ntp'")


def load_time_settings(path: str | Path = DEFAULT_TIME_STATE_PATH) -> dict[str, str]:
    state_path = Path(path)
    if not state_path.exists():
        return {"mode": "manual", "dst_mode": "automatic"}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read time settings: {exc}") from exc
    return normalize_time_settings(raw)


def write_time_settings(
    settings: dict[str, Any], path: str | Path = DEFAULT_TIME_STATE_PATH
) -> dict[str, str]:
    """Atomically store validated settings in the UI-owned state directory."""
    normalized = normalize_time_settings(settings)
    state_path = Path(path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ValueError(
            f"cannot save time settings at {state_path}; the UI service needs write access to "
            "the PyEMS logs directory (rerun install.sh after updating)"
        ) from exc
    fd, temp_name = tempfile.mkstemp(prefix=".time-settings-", dir=state_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # The target is Linux, where the settings must not be world
            # writable. Windows has no fchmod; keeping this portable lets the
            # same validation code run in local development tests.
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, state_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return normalized


def load_time_sync_status(
    path: str | Path = DEFAULT_TIME_STATUS_PATH,
) -> dict[str, Any] | None:
    status_path = Path(path)
    if not status_path.exists():
        return None
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "operation": "status",
            "recorded_at": "",
            "message": "could not read the last synchronization status",
        }
    return value if isinstance(value, dict) else None


def write_time_sync_status(
    value: dict[str, Any], path: str | Path = DEFAULT_TIME_STATUS_PATH
) -> None:
    """Write the root-owned record shown by the HMI after each NTP attempt."""
    status_path = Path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".time-sync-status-", dir=status_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o644)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, status_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def controller_clock() -> dict[str, Any]:
    """The OS wall clock, deliberately unrelated to the EMS telemetry file."""
    now = dt.datetime.now().astimezone()
    return {
        "ok": True,
        "local_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "local_datetime": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": str(now.tzinfo or ""),
        "unix_ms": round(now.timestamp() * 1000),
    }


def _unix_to_ntp(value: float) -> bytes:
    seconds = value + NTP_EPOCH_OFFSET_S
    whole = int(seconds)
    fraction = int((seconds - whole) * (1 << 32))
    return struct.pack("!II", whole, fraction)


def _ntp_to_unix(value: bytes) -> float:
    seconds, fraction = struct.unpack("!II", value)
    if seconds == 0:
        raise ValueError("NTP response contains an empty timestamp")
    return seconds - NTP_EPOCH_OFFSET_S + fraction / (1 << 32)


def ntp_probe(server: object, timeout_s: float = 3.0) -> dict[str, Any]:
    """Query an NTP server over UDP and return a measured clock sample.

    This is both the UI's non-destructive connection test and the source used by
    the root-owned scheduled synchronizer.  The standard four-timestamp offset
    calculation compensates for the network round trip before the clock is set.
    """
    host = validate_ntp_server(server)
    try:
        addresses = socket.getaddrinfo(host, NTP_PORT, type=socket.SOCK_DGRAM)
    except OSError as exc:
        raise ValueError(f"cannot resolve NTP server {host}: {exc}") from exc
    if not addresses:
        raise ValueError(f"cannot resolve NTP server {host}")

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout_s)
            sent_at = time.time()
            request = bytearray(48)
            request[0] = 0x23  # NTPv4 client request
            request[40:48] = _unix_to_ntp(sent_at)
            sock.sendto(request, sockaddr)
            response, peer = sock.recvfrom(512)
            received_at = time.time()
        except OSError as exc:
            last_error = exc
            continue
        finally:
            sock.close()

        if len(response) < 48:
            raise ValueError(f"NTP server {host} returned a truncated response")
        mode = response[0] & 0x07
        stratum = response[1]
        if mode not in (4, 5) or stratum == 0:
            raise ValueError(f"NTP server {host} returned an unsynchronised response")
        try:
            received_by_server = _ntp_to_unix(response[32:40])
            sent_by_server = _ntp_to_unix(response[40:48])
        except (struct.error, ValueError) as exc:
            raise ValueError(f"NTP server {host} returned an invalid timestamp: {exc}") from exc
        offset_s = ((received_by_server - sent_at) + (sent_by_server - received_at)) / 2
        corrected_unix_s = received_at + offset_s
        server_time = dt.datetime.fromtimestamp(corrected_unix_s, dt.timezone.utc)
        peer_host = peer[0] if isinstance(peer, tuple) and peer else str(peer)
        return {
            "ok": True,
            "server": host,
            "peer": peer_host,
            "stratum": int(stratum),
            "round_trip_ms": round((received_at - sent_at) * 1000, 1),
            "offset_ms": round(offset_s * 1000, 1),
            "unix_time_s": corrected_unix_s,
            "server_time_utc": server_time.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
    detail = str(last_error) if last_error else "no UDP address could be contacted"
    raise ValueError(f"NTP server {host} did not respond within {timeout_s:g} s: {detail}")
