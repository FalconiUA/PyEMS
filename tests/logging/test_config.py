"""Tests for log-level resolution."""
import logging

import pytest

from pyems.logging import resolve_level


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
