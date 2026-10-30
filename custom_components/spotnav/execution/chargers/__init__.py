"""The charger paths, one module per vendor behaviour: importing the package registers them all."""

from . import (  # noqa: F401 - the imports register the paths
    abb,
    base,
    easee,
    generic,
    goe,
    lektrico,
    nrgkick,
    ocpp,
    ohme,
    peblar,
    phase_switches,
    registry,
    smartevse,
    wattpilot,
    webasto,
    zaptec,
)
