"""The charger paths, one module per vendor behaviour: importing the package registers them all."""

from . import base, easee, generic, ocpp, registry, zaptec  # noqa: F401 - the imports register the paths
