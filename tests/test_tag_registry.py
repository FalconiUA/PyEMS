import yaml

from pyems.ems import ROOT
from pyems.system_tags import (
    COMMS_AGE_CHANNEL,
    SAFE_MODE_CHANNEL,
    WRITE_AGE_CHANNEL,
    comms_age_channel,
)
from pyems.tag_registry import collect, render_markdown, render_text, requester_rows

SIM_SITE = ROOT / "config" / "site.sim.yaml"


def sim_site() -> dict:
    return yaml.safe_load(SIM_SITE.read_text(encoding="utf-8"))


def test_collect_cross_references_the_sim_site():
    entries = collect(sim_site())

    grid_w = entries["grid.W"]
    assert "reg @32278" in grid_w.origin
    assert any("connection_point_" in r for r in grid_w.reads)
    assert any("freeze guard" in r for r in grid_w.reads)

    wset = entries["pv.WSet"]
    assert any("SOLE WRITER" in w for w in wset.writes)
    assert any("PRIORITY-0" in w for w in wset.writes)
    assert any("setpoint_headroom" in w for w in wset.writes)

    assert COMMS_AGE_CHANNEL in entries
    assert SAFE_MODE_CHANNEL in entries
    assert any("trip" in r for r in entries[COMMS_AGE_CHANNEL].reads)
    assert comms_age_channel("grid") in entries
    assert any("grid age" in r for r in entries[comms_age_channel("grid")].reads)

    # the sim site sets safety.max_write_age_s, so the write-age tag is present
    # and read by safety for the write-path trip
    assert WRITE_AGE_CHANNEL in entries
    assert any("trip" in r for r in entries[WRITE_AGE_CHANNEL].reads)


def test_binding_to_unknown_tag_is_flagged_not_hidden():
    site = sim_site()
    site["safety"]["frozen_measurement_channels"] = ["typo.W"]
    entries = collect(site)
    assert entries["typo.W"].origin.startswith("!!")


def test_renderers_cover_all_tags_and_requesters():
    site = sim_site()
    entries = collect(site)
    text = render_text(entries, site)
    md = render_markdown(entries, site, str(SIM_SITE))
    for tag in entries:
        assert tag in text and f"`{tag}`" in md
    for requester, _prio, _role in requester_rows(site):
        assert requester in text and requester in md
