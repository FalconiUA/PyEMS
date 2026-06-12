import pytest

from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.controllers.connection_point_import_limit import ConnectionPointImportLimitController


CH = "pv.WSet"


def make_state() -> SystemState:
    return SystemState(
        [
            Channel("grid.W", unit="W"),
            Channel("pv.W", unit="W"),
            Channel(CH, unit="W", min_val=0, max_val=100000, writable=True),
        ]
    )


def make_ctrl() -> ConnectionPointImportLimitController:
    return ConnectionPointImportLimitController(
        name="connection_point_import_limit",
        priority=10,
        import_limit_w=50000.0,
        connection_point_active_power_channel="grid.W",
        unit_active_power_channel="pv.W",
        unit_active_power_setpoint_channel=CH,
        deadband_w=0.0,
    )


def test_over_import_posts_minimum_generation_request():
    state = make_state()
    state.apply_driver_value("grid.W", 80000.0)
    state.apply_driver_value("pv.W", 10000.0)
    board = RequestBoard([CH])
    board.tick(0.0)

    make_ctrl().execute(state, board)

    req = board.valid_requests(CH, now=0.0)[0]
    assert req.min_w == pytest.approx(40000.0)
    assert req.target_w == pytest.approx(40000.0)


def test_inside_import_limit_withdraws_request():
    state = make_state()
    state.apply_driver_value("grid.W", 80000.0)
    board = RequestBoard([CH])
    board.tick(0.0)
    ctrl = make_ctrl()
    ctrl.execute(state, board)
    assert board.valid_requests(CH, now=0.0)

    state.apply_driver_value("grid.W", 30000.0)
    board.tick(1.0)
    ctrl.execute(state, board)

    assert board.valid_requests(CH, now=1.0) == []
