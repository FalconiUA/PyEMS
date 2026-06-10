"""Tests for log-level resolution (src/pyems/logging_config.py)."""
import logging

import pytest

from pyems.logging_config import resolve_level


@pytest.mark.parametrize("spec,expected", [
    ("DEBUG", logging.DEBUG),
    ("info", logging.INFO),
    (" Warning ", logging.WARNING),
    (logging.ERROR, logging.ERROR),
])
def test_resolve_level(spec, expected):
    assert resolve_level(spec) == expected


def test_resolve_level_rejects_unknown():
    with pytest.raises(ValueError, match="VERBOSE"):
        resolve_level("VERBOSE")
