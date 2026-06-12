import pytest

from pyems.allocation.allocator import PowerAllocator, SetpointChannelConfig
from pyems.allocation.request import RequestBoard
from pyems.channels import Channel, SystemState
from pyems.control.pid import PIDGains
from pyems.controllers.connection_point_power import ConnectionPointPowerController

CH = "plant.WSet"


def make_state() -> SystemState:
    return SystemState(
        [
            Channel("grid.W", unit="W"),
            Channel("plant.W", unit="W"),
            Channel(CH, unit="W", min_val=0, max_val=100000, writable=True),
        ]
    )


def make_ctrl(**kw) -> ConnectionPointPowerController:
    params = dict(
        name="connection_point_power",
        priority=10,
        export_limit_w=50000.0,
        connection_point_active_power_channel="grid.W",
        unit_active_power_channel="plant.W",
        unit_active_power_setpoint_channel=CH,
        gains=PIDGains(kp=0.4, ki=0.08, tt=5.0),
    )
    params.update(kw)
    return ConnectionPointPowerController(**params)


def make_import_ctrl(**kw) -> ConnectionPointPowerController:
    params = dict(
        name="connection_point_import_limit",
        export_limit_w=0.0,
        import_limit_w=50000.0,
        mode="import_limit",
        deadband_w=0.0,
    )
    params.update(kw)
    return make_ctrl(**params)


def posted(board: RequestBoard):
    reqs = board.valid_requests(CH, now=0.0)
    assert len(reqs) == 1
    return reqs[0]


def test_over_export_posts_hard_cap():
    state = make_state()
    state.apply_driver_value("grid.W", -70000.0)
    state.apply_driver_value("plant.W", 70000.0)
    board = RequestBoard([CH])
    board.tick(0.0)

    make_ctrl().execute(state, board)

    req = posted(board)
    assert req.max_w == pytest.approx(50000.0)
    assert req.target_w == pytest.approx(50000.0)


def test_import_limit_posts_floor():
    state = make_state()
    state.apply_driver_value("grid.W", 30000.0)
    state.apply_driver_value("plant.W", 10000.0)
    board = RequestBoard([CH])
    board.tick(0.0)

    make_ctrl(import_limit_w=10000.0).execute(state, board)

    req = posted(board)
    assert req.min_w == pytest.approx(30000.0)
    assert req.target_w >= req.min_w


def test_import_mode_posts_minimum_generation_request():
    state = make_state()
    state.apply_driver_value("grid.W", 80000.0)
    state.apply_driver_value("plant.W", 10000.0)
    board = RequestBoard([CH])
    board.tick(0.0)

    make_import_ctrl().execute(state, board)

    req = posted(board)
    assert req.min_w == pytest.approx(40000.0)
    assert req.target_w == pytest.approx(40000.0)


def test_import_mode_withdraws_when_inside_limit():
    state = make_state()
    state.apply_driver_value("grid.W", 80000.0)
    state.apply_driver_value("plant.W", 10000.0)
    board = RequestBoard([CH])
    board.tick(0.0)
    ctrl = make_import_ctrl()
    ctrl.execute(state, board)
    assert board.valid_requests(CH, now=0.0)

    state.apply_driver_value("grid.W", 30000.0)
    board.tick(1.0)
    ctrl.execute(state, board)

    assert board.valid_requests(CH, now=1.0) == []


def test_allocator_limits_rise_but_allows_fast_drop():
    state = make_state()
    board = RequestBoard([CH])
    alloc = PowerAllocator(
        [
            SetpointChannelConfig(
                setpoint_channel=CH,
                p_min_w=0.0,
                p_max_w=100000.0,
                default_w=100000.0,
                ramp_up_w_per_s=1000.0,
                ramp_down_w_per_s=1e9,
                deadband_w=0.0,
            )
        ],
        board,
        cycle_s=1.0,
    )
    ctrl = make_ctrl(gains=PIDGains(kp=0.0, ki=0.0))

    state.apply_driver_value("plant.W", 0.0)
    state.apply_driver_value("grid.W", 0.0)
    board.tick(0.0)
    ctrl.execute(state, board)
    alloc.resolve(state, now=0.0)
    assert state.get(CH) == pytest.approx(50000.0)

    state.apply_driver_value("plant.W", 50000.0)
    state.apply_driver_value("grid.W", 10000.0)
    board.tick(1.0)
    ctrl.execute(state, board)
    alloc.resolve(state, now=1.0)
    assert state.get(CH) == pytest.approx(51000.0)

    state.apply_driver_value("plant.W", 51000.0)
    state.apply_driver_value("grid.W", -80000.0)
    board.tick(2.0)
    ctrl.execute(state, board)
    alloc.resolve(state, now=2.0)
    assert state.get(CH) == pytest.approx(21000.0)


def test_external_anti_windup_under_allocator_ramp():
    state = make_state()
    state.apply_driver_value("plant.W", 0.0)
    state.apply_driver_value("grid.W", 0.0)
    board = RequestBoard([CH])
    ctrl = make_ctrl(gains=PIDGains(kp=0.0, ki=1.0, tt=1.0))

    board.tick(0.0)
    ctrl.execute(state, board)
    state.apply_driver_value(CH, 1000.0)
    before = ctrl.pid.integral

    board.tick(1.0)
    ctrl.execute(state, board)

    assert ctrl.pid.integral <= before


def test_task_skip_uses_board_now_delta():
    state = make_state()
    state.apply_driver_value("plant.W", 10000.0)
    state.apply_driver_value("grid.W", 0.0)
    board = RequestBoard([CH])
    ctrl = make_ctrl(gains=PIDGains(kp=0.0, ki=1.0, tt=100.0))

    board.tick(10.0)
    ctrl.execute(state, board)
    board.tick(15.0)
    ctrl.execute(state, board)

    assert ctrl.pid.integral > 200000.0
