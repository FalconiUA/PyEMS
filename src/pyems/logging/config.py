"""
Logging setup for the EMS entrypoint.

Library modules only ever call `logging.getLogger(__name__)` and log; they
never configure handlers (Python logging best practice). The process entrypoint
(build_ems().run()) calls setup_logging() once to install a handler.

Control-system logging policy:
  - State transitions (safety trip/release, bus up/down) log at WARNING/INFO
    ONCE on change, never every cycle; a 1 s loop must not spam the log.
  - Routine per-cycle detail is DEBUG.
  - The monotonic clock drives control; log timestamps are wall-clock for humans.

A rotating FILE handler is installed alongside the stderr handler when a log
file is configured (site.yaml `logging.file`, or the PYEMS_LOG_FILE env var), so
the web UI can show the EMS log without journalctl/SSH. The file uses the SAME
formatter as stderr, so the UI can parse lines back into structured rows.
"""
import logging
import logging.handlers
import os
from pathlib import Path

# One formatter shared by every handler: the UI parses file lines back into
# {time, level, logger, message}, so the on-disk format must match this exactly.
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Rotating file defaults: ~2 MB per file, keep a handful of generations. Small
# enough for an SD card, long enough to cover a commissioning session.
_LOG_MAX_BYTES = 2_000_000
_LOG_BACKUP_COUNT = 5


def resolve_level(spec: int | str) -> int:
    """Turn a level spec ('DEBUG', 'info', 20) into a logging level int."""
    if isinstance(spec, int):
        return spec
    level = logging.getLevelName(str(spec).strip().upper())
    if not isinstance(level, int):  # getLevelName returns 'Level X' for unknown
        raise ValueError(
            f"unknown log level {spec!r}; use DEBUG, INFO, WARNING, ERROR or CRITICAL"
        )
    return level


def setup_logging(
    level: int | str | None = None,
    log_file: str | Path | None = None,
) -> None:
    """Install a stderr handler (and an optional rotating file handler). Idempotent.

    Level precedence: explicit `level` argument (e.g. from --log-level), else
    the PYEMS_LOG_LEVEL environment variable, else INFO. In the field, DEBUG
    per-cycle detail is enabled with `PYEMS_LOG_LEVEL=DEBUG pyems`; no code
    change.

    File precedence: explicit `log_file` argument (e.g. from site.yaml
    `logging.file`), else the PYEMS_LOG_FILE environment variable, else no file
    (stderr/journal only). The file rotates so it never fills the SD card, and
    uses the same formatter as stderr so the UI can parse it back. A file we
    cannot open (read-only mount, bad path) must not stop the EMS — it logs a
    warning to stderr and carries on.

    The noisy `pymodbus` library logger is capped (default CRITICAL, override
    with PYEMS_PYMODBUS_LOG_LEVEL) — see _tame_pymodbus_logger.
    """
    resolved = resolve_level(
        level if level is not None else os.environ.get("PYEMS_LOG_LEVEL", "INFO")
    )
    # Always enforce the pymodbus cap, even if the root logger was pre-configured
    # (e.g. under pytest) — its per-transaction spam is the worst long-run noise.
    _tame_pymodbus_logger()
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. tests / re-entry); leave it
        return
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(resolved)

    file_spec = log_file if log_file is not None else os.environ.get("PYEMS_LOG_FILE")
    if file_spec:
        try:
            path = Path(file_spec)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            # Stderr/journal logging already works; the file is a convenience for
            # the UI, never a hard dependency of the control loop.
            root.warning("Could not open log file %r; logging to stderr only", file_spec)


def _tame_pymodbus_logger() -> None:
    """Cap the pymodbus library logger (default CRITICAL).

    pymodbus logs EVERY failed transaction at ERROR ("Connection refused;
    Repeating...."). During a sustained bus outage that is ~8 lines/s — it floods
    the journal and rotates the real EMS history out of the size-bounded file log
    exactly when it is needed. Our own layers (CompositeDriver / CachedDriver /
    ModbusDeviceDriver) already report bus-down/up transitions WITH device
    context, so this per-transaction noise is redundant. WARNING/ERROR do not
    silence it (the spam IS at ERROR), so the default is CRITICAL; set
    PYEMS_PYMODBUS_LOG_LEVEL=WARNING (or DEBUG) to see pymodbus detail when
    debugging the bus itself.
    """
    spec = os.environ.get("PYEMS_PYMODBUS_LOG_LEVEL", "CRITICAL")
    logging.getLogger("pymodbus").setLevel(resolve_level(spec))
