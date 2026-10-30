"""go-e (HACS, API v2 and MQTT): phases through the `psm` select (auto / one phase / three phases).

One or three phases is written as the select's own option, never auto, and auto reads as unknown. The `modelStatus`
sensor's "not charging because of phase switch" (code 23) is the switching status. Both integrations share the
select and the status with Fronius Wattpilot (`phase_switches.phase_mode_select`); only their option values differ.
"""

from __future__ import annotations

from typing import Final

from .phase_switches import phase_mode_select
from .registry import register_phase_switch

GOE_API2_DOMAIN: Final = "goecharger_api2"
GOE_MQTT_DOMAIN: Final = "goecharger_mqtt"

# go-e API v2 (marq24/ha-goecharger-api2): options "0" auto, "1" force one phase, "2" force three phases.
register_phase_switch(GOE_API2_DOMAIN, phase_mode_select(GOE_API2_DOMAIN, ("1",), ("2",)))
# go-e MQTT (syssi/homeassistant-goecharger-mqtt): options auto, one_phase, three_phases.
register_phase_switch(GOE_MQTT_DOMAIN, phase_mode_select(GOE_MQTT_DOMAIN, ("one_phase",), ("three_phases",)))
