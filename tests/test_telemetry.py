import json

from pyems.channels import Channel, SystemState
from pyems.ems import build_publisher
from pyems.telemetry import LiveSnapshotPublisher


def make_state():
    return SystemState(
        [
            Channel("grid.W", value=-1234.5, unit="W"),
            Channel("pv.WSet", value=5000.0, unit="W", writable=True),
            Channel("sys.comms_age_s", value=float("inf"), unit="s"),
        ]
    )


def test_publish_writes_expected_document(tmp_path):
    path = tmp_path / "live.json"
    channels = [
        Channel("grid.W", unit="W"),
        Channel("pv.WSet", unit="W", writable=True),
        Channel("sys.comms_age_s", unit="s"),
    ]
    pub = LiveSnapshotPublisher(path, channels=channels)
    pub.publish(now=12.345, state=make_state())

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["ok"] is True
    assert doc["monotonic_s"] == 12.345
    assert isinstance(doc["timestamp"], str)
    assert doc["values"]["grid.W"] == -1234.5
    assert doc["values"]["pv.WSet"] == 5000.0
    # +inf has no JSON form — published as null so it parses in a browser.
    assert doc["values"]["sys.comms_age_s"] is None
    meta = {item["name"]: item for item in doc["channels"]}
    assert meta["pv.WSet"]["writable"] is True
    assert meta["grid.W"]["writable"] is False
    assert meta["grid.W"]["unit"] == "W"


def test_publish_is_strict_json(tmp_path):
    """No bare Infinity/NaN tokens: the document must parse with strict JSON."""
    path = tmp_path / "live.json"
    LiveSnapshotPublisher(path).publish(now=1.0, state=make_state())
    text = path.read_text(encoding="utf-8")
    assert "Infinity" not in text and "NaN" not in text
    json.loads(text)  # strict by default; raises if invalid


def test_publish_overwrites_atomically_without_leftover_tmp(tmp_path):
    path = tmp_path / "live.json"
    pub = LiveSnapshotPublisher(path)
    pub.publish(now=1.0, state=make_state())
    pub.publish(now=2.0, state=make_state())

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["monotonic_s"] == 2.0  # second publish replaced the first
    # the atomic write must not litter the directory with temp files
    assert [p.name for p in tmp_path.iterdir()] == ["live.json"]


def test_publish_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "logs" / "live.json"
    LiveSnapshotPublisher(path).publish(now=1.0, state=make_state())
    assert path.exists()


def test_metadata_is_merged_into_document(tmp_path):
    path = tmp_path / "live.json"
    LiveSnapshotPublisher(path).publish(
        now=1.0, state=make_state(), metadata={"cycle_s": 1.0, "cycle_overrun": False}
    )
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["cycle_s"] == 1.0
    assert doc["cycle_overrun"] is False


def test_publish_without_channels_omits_metadata(tmp_path):
    path = tmp_path / "live.json"
    LiveSnapshotPublisher(path).publish(now=1.0, state=make_state())
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "channels" not in doc


def test_build_publisher_absent_section_returns_none():
    assert build_publisher({}, [Channel("pv.WSet", writable=True)]) is None


def test_build_publisher_resolves_relative_path_against_root(tmp_path):
    site = {"telemetry": {"live_json": "logs/some_live.json"}}
    pub = build_publisher(site, [Channel("pv.WSet", writable=True)])
    assert pub is not None
    # relative paths anchor to the repo root, like the recorder
    assert pub.path.is_absolute()
    assert pub.path.name == "some_live.json"
