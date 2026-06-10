"""
Logging setup for the EMS entrypoint.

Library modules only ever call `logging.getLogger(__name__)` and log — they
never configure handlers (Python logging best practice). The process entrypoint
(build_ems().run()) calls setup_logging() once to install a handler.

Control-system logging policy:
  - State transitions (safety trip/release, bus up/down) log at WARNING/INFO
    ONCE on change, never every cycle — a 1 s loop must not spam the log.
  - Routine per-cycle detail is DEBUG.
  - The monotonic clock drives control; log timestamps are wall-clock for humans.
"""
import logging
import os


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


def setup_logging(level: int | str | None = None) -> None:
    """Install a single stderr handler on the root logger. Idempotent.

    Level precedence: explicit `level` argument (e.g. from --log-level), else
    the PYEMS_LOG_LEVEL environment variable, else INFO. In the field, DEBUG
    per-cycle detail is enabled with `PYEMS_LOG_LEVEL=DEBUG pyems` — no code
    change.
    """
    resolved = resolve_level(
        level if level is not None else os.environ.get("PYEMS_LOG_LEVEL", "INFO")
    )
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. tests / re-entry) — leave it
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(resolved)
