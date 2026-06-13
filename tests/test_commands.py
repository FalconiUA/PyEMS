"""Operator command channel (src/pyems/commands.py) — fail-closed semantics.

The gate is an operational interlock, so the reader must default to "disabled"
on every uncertainty: missing/corrupt file, stale command, or a leftover enable
from a previous EMS run. Only a fresh `true` issued AFTER this run started enables
generation; a `false` is always honored.
"""
import json
import math

import pytest

from pyems.channels import Channel, SystemState
from pyems.commands import (
    CommandFileReader,
    read_command_file,
    update_command_file,
    write_command_file,
    write_inverter_command,
)
from pyems.system_tags import (
    COMMAND_AGE_CHANNEL,
    GENERATION_ALLOWED_CHANNEL,
    INVERTER_COMMAND_CHANNEL,
    INVERTER_COMMAND_ID_CHANNEL,
)


def make_state() -> SystemState:
    return SystemState(
        [
            Channel(GENERATION_ALLOWED_CHANNEL, min_val=0, max_val=1, writable=True),
            Channel(COMMAND_AGE_CHANNEL, unit="s", value=float("inf")),
            Channel(INVERTER_COMMAND_CHANNEL, min_val=0, max_val=1, value=float("nan")),
            Channel(INVERTER_COMMAND_ID_CHANNEL, value=float("nan")),
        ]
    )


def write_raw(path, *, enabled, issued_at) -> None:
    path.write_text(
        json.dumps({"generation_enabled": enabled, "issued_at": issued_at}),
        encoding="utf-8",
    )


def allowed(state) -> float:
    return state.get(GENERATION_ALLOWED_CHANNEL)


def test_missing_file_is_disabled(tmp_path):
    reader = CommandFileReader(tmp_path / "commands.json", run_start_wall=0.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=100.0)
    assert allowed(state) == 0.0
    assert math.isinf(state.get(COMMAND_AGE_CHANNEL))


def test_fresh_true_after_run_start_enables(tmp_path):
    path = tmp_path / "commands.json"
    write_raw(path, enabled=True, issued_at=1010.0)
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1020.0)
    assert allowed(state) == 1.0
    assert state.get(COMMAND_AGE_CHANNEL) == pytest.approx(10.0)


def test_true_from_previous_run_does_not_re_enable(tmp_path):
    """Restart guard: a leftover enable (issued at/before this run started) is
    ignored — generation must default OFF after every restart."""
    path = tmp_path / "commands.json"
    write_raw(path, enabled=True, issued_at=999.0)  # before run_start
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1001.0)  # fresh in age, but old run
    assert allowed(state) == 0.0


def test_true_stays_enabled_after_initial_fresh_acceptance(tmp_path):
    path = tmp_path / "commands.json"
    write_raw(path, enabled=True, issued_at=1010.0)
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1011.0)  # first read sees a fresh start
    assert allowed(state) == 1.0

    later = make_state()
    reader.apply(later, now_wall=1100.0)  # age 90 s > 30 s, but already latched
    assert allowed(later) == 1.0


def test_stale_true_never_seen_fresh_is_disabled(tmp_path):
    path = tmp_path / "commands.json"
    write_raw(path, enabled=True, issued_at=1010.0)
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1100.0)  # first read is stale
    assert allowed(state) == 0.0


def test_false_is_always_honored_even_when_fresh(tmp_path):
    path = tmp_path / "commands.json"
    write_raw(path, enabled=False, issued_at=1010.0)
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1011.0)
    assert allowed(state) == 0.0


def test_malformed_file_is_disabled(tmp_path):
    path = tmp_path / "commands.json"
    path.write_text("{not valid json", encoding="utf-8")
    reader = CommandFileReader(path, run_start_wall=0.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1.0)
    assert allowed(state) == 0.0


def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "commands.json"
    doc = write_command_file(path, generation_enabled=True)
    assert doc["generation_enabled"] is True
    on_disk = read_command_file(path)
    assert on_disk["generation_enabled"] is True
    assert isinstance(on_disk["issued_at"], (int, float))


def test_writer_reader_enable_path(tmp_path):
    """The real writer stamps issued_at = now; a reader whose run started earlier
    honors it (the live UI → EMS path)."""
    path = tmp_path / "commands.json"
    doc = write_command_file(path, generation_enabled=True)
    reader = CommandFileReader(path, run_start_wall=doc["issued_at"] - 100.0, max_age_s=60)
    state = make_state()
    reader.apply(state, now_wall=doc["issued_at"] + 1.0)
    assert allowed(state) == 1.0


# ── command file is shared (merge) by the soft gate and the hard switch ───────
def test_update_command_file_merges_keys(tmp_path):
    path = tmp_path / "commands.json"
    write_command_file(path, generation_enabled=True)
    write_inverter_command(path, action="stop")
    doc = read_command_file(path)
    # the inverter write preserved the generation keys, and vice versa
    assert doc["generation_enabled"] is True
    assert doc["inverter_command"] == "stop"
    assert "issued_at" in doc and "inverter_command_id" in doc
    write_command_file(path, generation_enabled=False)
    doc = read_command_file(path)
    assert doc["generation_enabled"] is False
    assert doc["inverter_command"] == "stop"  # still preserved


def test_write_inverter_command_rejects_bad_action(tmp_path):
    with pytest.raises(ValueError, match="start.*stop"):
        write_inverter_command(tmp_path / "commands.json", action="boom")


# ── hard inverter switch: latched, restart-safe ──────────────────────────────
def _inv(state):
    return state.get(INVERTER_COMMAND_CHANNEL), state.get(INVERTER_COMMAND_ID_CHANNEL)


def write_inv_raw(path, command, command_id):
    update_command_file(path, inverter_command=command, inverter_command_id=command_id)


def test_fresh_inverter_command_published(tmp_path):
    path = tmp_path / "commands.json"
    write_inv_raw(path, "start", 1010.0)
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1011.0)
    cmd, cmd_id = _inv(state)
    assert cmd == 1.0 and cmd_id == 1010.0


def test_inverter_command_from_previous_run_ignored(tmp_path):
    """Restart guard: a leftover hard command (id ≤ run start) must NOT fire."""
    path = tmp_path / "commands.json"
    write_inv_raw(path, "stop", 999.0)  # issued before this run started
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1001.0)
    cmd, cmd_id = _inv(state)
    assert math.isnan(cmd) and math.isnan(cmd_id)  # no action published


def test_no_inverter_command_is_nan(tmp_path):
    path = tmp_path / "commands.json"
    write_command_file(path, generation_enabled=True)  # only generation, no inverter
    reader = CommandFileReader(path, run_start_wall=1000.0, max_age_s=30)
    state = make_state()
    reader.apply(state, now_wall=1001.0)
    cmd, cmd_id = _inv(state)
    assert math.isnan(cmd) and math.isnan(cmd_id)
