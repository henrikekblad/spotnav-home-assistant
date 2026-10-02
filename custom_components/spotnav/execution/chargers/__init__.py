"""The charger paths, one module per vendor behaviour: importing the package registers them all."""

from . import (  # noqa: F401 - the imports register the paths
    abb,
    base,
    easee,
    generic,
    ocpp,
    ohme,
    peblar,
    registry,
    smartevse,
    webasto,
    zaptec,
)
