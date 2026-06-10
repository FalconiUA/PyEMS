"""One simulated field device = a real pymodbus TCP server + register codec.

The register map comes from the SAME profiles/*.yaml the production driver
loads, so the sim serves exactly the registers the EMS will poll — adding a
device model to the sim is the same zero-code step as adding it to the EMS.

Fault injection (per device, toggled live from the sim UI):
  offline           server is shut down — the EMS client sees connection
                    errors/timeouts, comms age grows, safety must trip
  freeze            server keeps answering but register values stop updating —
                    drives the frozen-measurement guard
  modbus_exception  every request gets a true Modbus exception response
                    (device failure) — the gateway-serving-errors scenario
  reject_writes     reads work, setpoint writes are rejected (illegal value) —
                    the "remote control not enabled" write path
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
from typing import Callable

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from pyems.drivers.modbus_device import DeviceProfile, RegisterDef, _decode, _encode

logger = logging.getLogger(__name__)

FAULTS = ("offline", "freeze", "modbus_exception", "reject_writes")


class SimulatedDevice:
    """Serves one device profile over Modbus TCP and mirrors a field-value dict.

    - read-only registers are refreshed from `fields` on every read request
      (unless the freeze fault is active);
    - writable registers belong to the EMS: an accepted write is decoded and
      reported through `on_setpoint(field, value)`.
    """

    def __init__(
        self,
        device_id: str,
        profile: DeviceProfile,
        host: str,
        port: int,
        slave_id: int = 1,
        on_setpoint: Callable[[str, float], None] | None = None,
    ) -> None:
        self.device_id = device_id
        self.profile = profile
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.on_setpoint = on_setpoint

        regs = profile.registers
        self._block_start = min(r.address for r in regs)
        self._block_len = max(r.address + r.count for r in regs) - self._block_start
        # field name ("W", "WSet"...) per register, for fields()/setpoint routing
        self._field = {r.channel: r.channel.split(".", 1)[-1] for r in regs}
        self._read_regs = [r for r in regs if not r.writable]
        self._write_regs = [r for r in regs if r.writable]

        self._lock = threading.Lock()
        self._fields: dict[str, float] = {}
        self._faults: dict[str, bool] = {name: False for name in FAULTS}
        self._registers: list[int] | None = None  # live server block, captured in _action
        self._server: ModbusTcpServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._rng = random.Random()

    # ── world-side API ────────────────────────────────────────────────────────
    def set_fields(self, fields: dict[str, float]) -> None:
        """Publish the latest physical values; served on the next read."""
        with self._lock:
            self._fields.update(fields)

    def set_fault(self, name: str, active: bool) -> None:
        if name not in FAULTS:
            raise ValueError(f"unknown fault {name!r}; known: {FAULTS}")
        with self._lock:
            was = self._faults[name]
            self._faults[name] = active
        if was != active:
            logger.info("sim device %s: fault %s %s", self.device_id, name,
                        "ACTIVE" if active else "cleared")
        if name == "offline":
            if active:
                self._stop_server()
            else:
                self._start_server()

    def faults(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._faults)

    def online(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── register codec ────────────────────────────────────────────────────────
    def _encode_into(self, registers: list[int], reg: RegisterDef, value: float) -> None:
        raw = int(round(value / reg.scale))
        words = _encode(raw, reg)
        offset = reg.address - self._block_start
        registers[offset : offset + reg.count] = words

    def _refresh_registers(self, registers: list[int]) -> None:
        with self._lock:
            fields = dict(self._fields)
        for reg in self._read_regs:
            field = self._field[reg.channel]
            if field in fields:
                self._encode_into(registers, reg, fields[field])

    def _route_write(self, address: int, values: list[int]) -> None:
        """Decode any writable register fully covered by this write request."""
        end = address + len(values)
        for reg in self._write_regs:
            if address <= reg.address and reg.address + reg.count <= end:
                start = reg.address - address
                value = _decode(list(values[start : start + reg.count]), reg) * reg.scale
                logger.info(
                    "sim device %s: EMS wrote %s = %.1f", self.device_id, reg.channel, value
                )
                if self.on_setpoint is not None:
                    self.on_setpoint(self._field[reg.channel], value)

    # ── pymodbus action hook (runs on the server's asyncio thread) ────────────
    async def _action(self, _fc, start_address, address, _count, current_registers, set_values):
        self._registers = current_registers  # keep in sync if pymodbus rebuilds it
        assert start_address == self._block_start
        with self._lock:
            faults = dict(self._faults)
        if faults["modbus_exception"]:
            return ExcCodes.DEVICE_FAILURE
        if set_values is not None:  # write request
            if faults["reject_writes"]:
                return ExcCodes.ILLEGAL_VALUE
            self._route_write(address, list(set_values))
            return None
        if not faults["freeze"]:
            self._refresh_registers(current_registers)
        return None

    # ── server lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.faults()["offline"]:
            self._start_server()

    def stop(self) -> None:
        self._stop_server()

    def _start_server(self) -> None:
        if self.online():
            return
        ready = threading.Event()
        startup_error: list[BaseException] = []

        def serve() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def run() -> None:
                block = SimData(
                    self._block_start,
                    count=self._block_len,
                    values=0,
                    datatype=DataType.REGISTERS,
                )
                server = ModbusTcpServer(
                    SimDevice(id=self.slave_id, simdata=[block], action=self._action),
                    address=(self.host, self.port),
                )
                self._server = server
                ready.set()
                await server.serve_forever()

            try:
                loop.run_until_complete(run())
            except BaseException as exc:  # surface bind errors to the caller
                startup_error.append(exc)
                ready.set()
            finally:
                loop.close()

        self._thread = threading.Thread(
            target=serve, name=f"sim-{self.device_id}", daemon=True
        )
        self._thread.start()
        if not ready.wait(timeout=5.0):
            raise RuntimeError(f"sim device {self.device_id} never started")
        if startup_error:
            raise RuntimeError(
                f"sim device {self.device_id} failed to start on "
                f"{self.host}:{self.port}"
            ) from startup_error[0]
        logger.info(
            "sim device %s (%s) serving on %s:%d slave %d",
            self.device_id, self.profile.model, self.host, self.port, self.slave_id,
        )

    def _stop_server(self) -> None:
        server, loop, thread = self._server, self._loop, self._thread
        self._server = None
        if server is not None and loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(server.shutdown(), loop).result(timeout=5.0)
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None
