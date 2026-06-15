"""Tests for log-level resolution and handler setup."""
import contextlib
import logging
import logging.handlers

import pytest

from pyems.logging import resolve_level, setup_logging


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


@contextlib.contextmanager
def unconfigured_root():
    """Start with an empty root logger so setup_logging() actually installs.

    Done inside the test body (call phase), not a fixture: pytest's own logging
    plugin re-adds a capture handler to root for the call phase, and
    setup_logging is a no-op when any handler is already present."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for h in root.handlers:
            h.close()
        root.handlers, root.level = saved_handlers, saved_level


def test_setup_logging_writes_to_file(tmp_path):
    log_file = tmp_path / "logs" / "pyems.log"  # parent does not exist yet
    with unconfigured_root() as root:
        setup_logging("INFO", log_file=log_file)
        logging.getLogger("pyems.test").warning("safety trip on grid")
        handlers = list(root.handlers)

    assert log_file.exists()  # parent dir created
    text = log_file.read_text(encoding="utf-8")
    assert "WARNING" in text and "safety trip on grid" in text
    # stderr handler installed alongside the file handler.
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
    assert any(type(h) is logging.StreamHandler for h in handlers)


def test_setup_logging_is_idempotent(tmp_path):
    with unconfigured_root() as root:
        setup_logging("INFO", log_file=tmp_path / "a.log")
        before = list(root.handlers)
        setup_logging("DEBUG", log_file=tmp_path / "b.log")  # no-op: handlers exist
        assert root.handlers == before


def test_setup_logging_without_file_has_no_file_handler():
    with unconfigured_root() as root:
        setup_logging("INFO")
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
        )


@pytest.fixture
def restore_pymodbus_level():
    pm = logging.getLogger("pymodbus")
    saved = pm.level
    try:
        yield pm
    finally:
        pm.setLevel(saved)


def test_setup_logging_caps_pymodbus_logger(restore_pymodbus_level):
    # pymodbus logs every failed transaction at ERROR; default cap is CRITICAL so
    # a sustained bus outage cannot flood the journal / rotate real history away.
    restore_pymodbus_level.setLevel(logging.NOTSET)
    with unconfigured_root():
        setup_logging("INFO")
    assert restore_pymodbus_level.level == logging.CRITICAL


def test_setup_logging_pymodbus_level_env_override(monkeypatch, restore_pymodbus_level):
    monkeypatch.setenv("PYEMS_PYMODBUS_LOG_LEVEL", "WARNING")
    with unconfigured_root():
        setup_logging("INFO")
    assert restore_pymodbus_level.level == logging.WARNING


def test_setup_logging_caps_pymodbus_even_when_root_preconfigured(restore_pymodbus_level):
    # The cap must apply even on re-entry (root already has handlers under pytest),
    # because the per-transaction spam is the worst long-run noise.
    restore_pymodbus_level.setLevel(logging.NOTSET)
    setup_logging("INFO")  # root already configured by pytest -> early return path
    assert restore_pymodbus_level.level == logging.CRITICAL
