"""Backward-compatible import path for logging utilities.

New code should import from `pyems.logging`.
"""

from pyems.logging import resolve_level, setup_logging

__all__ = ["resolve_level", "setup_logging"]
