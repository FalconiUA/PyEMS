"""
Controller package.

Importing this package populates the controller registry as a side effect:
every concrete Controller is imported here so its @register(...) decorator runs.
build_ems() relies on this — `import pyems.controllers` makes all controller
types resolvable by name from site.yaml `tasks:`.

To add a new controller type: create its module, decorate the class with
@register("<type>"), then import it below so it joins the registry.
"""
from pyems.controllers import safety  # noqa: F401  (populate registry)
from pyems.controllers import grid_export_limit  # noqa: F401  (populate registry)
from pyems.controllers.registry import (
    BuildContext,
    build_controller,
    register,
    registered_types,
)

__all__ = ["BuildContext", "build_controller", "register", "registered_types"]
