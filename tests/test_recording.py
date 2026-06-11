import csv

import pytest

from pyems.channels import Channel, SystemState
from pyems.ems import build_recorder
from pyems.recording import CycleRecorder
from pyems.scheduler import Scheduler, Task


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_recorder_writes_header_and_rows(tmp_path):
    state = SystemState([Channel("grid.W", value=-1234.5), Channel("pv.W", value=5000.0)])
    rec = CycleRecorder(tmp_path / "cycles.csv", ["grid.W", "pv.W"])
    rec.record(now=1.0, state=state)
    state._channels["grid.W"].value = 100.0
    rec.record(now=2.0, state=state)
    rec.close()

    rows = read_rows(tmp_path / "cycles.csv")
    assert rows[0] == ["timestamp", "monotonic_s", "grid.W", "pv.W"]
    assert rows[1][1:] == ["1.000", "-1234.5", "5000.0"]
    assert rows[2][1:] == ["2.000", "100.0", "5000.0"]


def test_recorder_appends_without_second_header(tmp_path):
    path = tmp_path / "cycles.csv"
    state = SystemState([Channel("pv.W", value=1.0)])
    for _ in range(2):
        rec = CycleRecorder(path, ["pv.W"])
        rec.record(now=0.0, state=state)
        rec.close()
    rows = read_rows(path)
    assert len(rows) == 3  # one header + two data rows across two sessions
    assert rows[0][0] == "timestamp"


def test_recorder_rejects_empty_channel_list(tmp_path):
    with pytest.raises(ValueError):
        CycleRecorder(tmp_path / "cycles.csv", [])


def test_scheduler_records_each_cycle_and_survives_recorder_failure(
    tmp_path, channels, fake_driver
):
    rec = CycleRecorder(tmp_path / "cycles.csv", ["grid.W", "pv.WSet"])
    state = SystemState(channels)
    sched = Scheduler(
        tasks=[Task("idle", 1.0, priority=1)],
        state=state,
        driver=fake_driver,
        recorder=rec,
    )
    fake_driver.measurements = {"grid.W": -500.0}
    sched.step(now=0.0)
    sched.step(now=1.0)

    # a dying recorder must not take the scan cycle down
    rec._fh.close()
    sched.step(now=2.0)  # would raise ValueError on closed file if uncontained
    rows = read_rows(tmp_path / "cycles.csv")
    assert len(rows) == 1 + 2  # header + the two successful cycles
    assert rows[1][2] == "-500.0"


def make_site(tmp_path, **recording):
    return {
        "export_limit": {
            "limit_w": 1000.0,
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "connection_point_active_power": {
            "connection_point_active_power_channel": "grid.W",
            "unit_active_power_channel": "pv.W",
            "unit_active_power_setpoint_channel": "pv.WSet",
        },
        "safety": {"unit_active_power_setpoint_channels": ["pv.WSet"]},
        "allocation": {"channels": [{"setpoint_channel": "pv.WSet"}]},
        "recording": recording,
    }


def test_build_recorder_absent_section_returns_none(tmp_path):
    site = make_site(tmp_path)
    site.pop("recording")
    assert build_recorder(site, ["grid.W"]) is None


def test_build_recorder_validates_channels(tmp_path):
    site = make_site(
        tmp_path, cycle_csv=str(tmp_path / "c.csv"), channels=["grid.W", "nope.W"]
    )
    with pytest.raises(ValueError, match="nope.W"):
        build_recorder(site, ["grid.W", "pv.W"])


def test_build_recorder_defaults_to_bound_channels(tmp_path):
    site = make_site(tmp_path, cycle_csv=str(tmp_path / "c.csv"))
    available = ["grid.W", "pv.W", "pv.WSet", "sys.comms_age_s", "sys.safe_mode"]
    rec = build_recorder(site, available)
    try:
        assert set(rec.channels) == set(available)
    finally:
        rec.close()
