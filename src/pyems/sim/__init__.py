"""Device simulation harness — run the real EMS against simulated hardware.

The sim side speaks genuine Modbus TCP (pymodbus server per device, register
maps taken from the same profiles/*.yaml the production driver uses), so the
EMS process runs completely unmodified against `config/site.sim.yaml`.
"""
