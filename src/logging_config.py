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


def setup_logging(level: int = logging.INFO) -> None:
    """Install a single stderr handler on the root logger. Idempotent."""
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
    root.setLevel(level)
