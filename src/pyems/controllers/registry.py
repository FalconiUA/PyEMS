"""
Controller registry — the seam that makes a control *scenario* data, not code.

Devices are already data (profiles/ + site.yaml `devices`). This module extends
the same philosophy to the control logic: which FUNCTION_BLOCKs run, in which
TASKs, with which tag bindings is declared in site.yaml `tasks:` (data) and
assembled here — never hardcoded in build_ems().

Adding a new scenario:
  - reuse existing control logic → just add a `tasks:` entry in site.yaml.
  - new control logic → write a Controller subclass, decorate it with
    @register("<type>") and a from_config() classmethod; then bind it in YAML.
No edits to build_ems() either way.

IEC 61131-3 analogy: the registry is the library of FUNCTION_BLOCK *types*;
site.yaml `tasks:` is the CONFIGURATION that instantiates and wires them.
"""
from __future__ import annotations

from dataclasses import dataclass

from pyems.controllers.base import Controller

# type name (as written in site.yaml) → Controller subclass
_REGISTRY: dict[str, type[Controller]] = {}


@dataclass
class BuildContext:
    """What a controller needs from the resource at build time, beyond its own
    params. Also the place binding validation lives, so a mistyped tag in a
    scenario fails loudly at startup, not silently at runtime.

    - cycle_s        : the period of the TASK this controller runs in (for
                       ramp/gradient math that depends on the scan interval).
    - channel_names  : every tag in the merged pool (device profiles + sys.*).
    - writable_names : the subset that is a setpoint (VAR_OUTPUT to hardware).
    """

    cycle_s: float
    channel_names: frozenset[str]
    writable_names: frozenset[str]

    def channel(self, name: str) -> str:
        """Validate a read binding: the tag must exist in the pool."""
        if name not in self.channel_names:
            raise ValueError(
                f"channel binding '{name}' is not a known tag — check the "
                f"device profiles and `devices:` ids in site.yaml"
            )
        return name

    def writable(self, name: str) -> str:
        """Validate a setpoint binding: the tag must exist AND be writable."""
        self.channel(name)
        if name not in self.writable_names:
            raise ValueError(
                f"setpoint binding '{name}' is read-only — a VAR_OUTPUT must "
                f"map to a writable register (access: read_write in the profile)"
            )
        return name


def register(type_name: str):
    """Class decorator: make a Controller buildable from site.yaml by `type`."""

    def deco(cls: type[Controller]) -> type[Controller]:
        if type_name in _REGISTRY:
            raise ValueError(f"controller type '{type_name}' already registered")
        if not hasattr(cls, "from_config"):
            raise TypeError(f"{cls.__name__} must define a from_config() classmethod")
        cls.type_name = type_name  # type: ignore[attr-defined]
        _REGISTRY[type_name] = cls
        return cls

    return deco


def build_controller(spec: dict, ctx: BuildContext) -> Controller:
    """Instantiate one controller from a `tasks[].controllers[]` entry."""
    type_name = spec["type"]
    try:
        cls = _REGISTRY[type_name]
    except KeyError:
        raise ValueError(
            f"unknown controller type '{type_name}'; "
            f"registered types: {sorted(_REGISTRY)}"
        ) from None
    return cls.from_config(spec.get("params", {}), ctx)  # type: ignore[attr-defined]


def registered_types() -> dict[str, type[Controller]]:
    return dict(_REGISTRY)
