import pytest

from pyems.sim.sources import (
    ManualSource,
    ReplaySource,
    SourceBox,
    SyntheticSource,
    parse_csv_series,
)


def test_parse_csv_plain_values():
    assert parse_csv_series("100\n200.5\n\n300\n") == [100.0, 200.5, 300.0]


def test_parse_csv_timestamp_value_and_header():
    text = "time,power_w\n2024-06-01 12:00:00,1500\n2024-06-01 12:00:01,1600\n"
    assert parse_csv_series(text) == [1500.0, 1600.0]


def test_parse_csv_semicolon():
    assert parse_csv_series("12:00:00;250\n12:00:01;260") == [250.0, 260.0]


def test_manual_source_constant():
    src = ManualSource(4200.0)
    assert src.value_w(0.0) == 4200.0
    assert src.value_w(999.0) == 4200.0


def test_synthetic_source_floor_and_period():
    src = SyntheticSource(base_w=0.0, amplitude_w=1000.0, period_s=100.0, seed=1)
    # second half of the sine period would be negative — clamped at the floor
    assert src.value_w(75.0) == 0.0
    assert src.value_w(25.0) == pytest.approx(1000.0)


def test_replay_indexes_one_sample_per_second():
    src = ReplaySource([10.0, 20.0, 30.0], speed=1.0, loop=False)
    assert src.value_w(100.0) == 10.0   # first call anchors t0
    assert src.value_w(101.0) == 20.0
    assert src.value_w(102.0) == 30.0
    assert src.value_w(150.0) == 30.0   # past the end: hold last


def test_replay_loop_and_speed():
    src = ReplaySource([1.0, 2.0], speed=2.0, loop=True)
    assert src.value_w(0.0) == 1.0
    assert src.value_w(0.5) == 2.0      # 0.5 s * speed 2 = sample 1
    assert src.value_w(1.0) == 1.0      # wrapped around


def test_replay_rejects_empty_and_bad_speed():
    with pytest.raises(ValueError):
        ReplaySource([])
    with pytest.raises(ValueError):
        ReplaySource([1.0], speed=0.0)


def test_source_box_swaps_at_runtime():
    box = SourceBox("pv", ManualSource(100.0))
    assert box.value_w(0.0) == 100.0
    box.set_source(ManualSource(500.0))
    assert box.value_w(1.0) == 500.0
    info = box.describe()
    assert info["mode"] == "manual"
    assert info["last_w"] == 500.0
