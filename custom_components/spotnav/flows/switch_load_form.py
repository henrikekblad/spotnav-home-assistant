"""The site options' switched load page: a switch, its rated power, its runtime per 24 hours and an optional comfort
floor (a temperature sensor and the lowest temperature). Empty switch: no load. Stored under the site entry's
`CONF_SWITCH_LOAD` (`loads/switch_load.SwitchLoadConfig`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from ..loads.switch_load import (
    FLOOR_C_MAX,
    FLOOR_C_MIN,
    KEY_ENTITY,
    KEY_FLOOR_C,
    KEY_RATED_KW,
    KEY_RUNTIME_H,
    KEY_TEMPERATURE,
    RATED_KW_MAX,
    RATED_KW_MIN,
    RUNTIME_H_MAX,
    RUNTIME_H_MIN,
    SWITCH_DOMAINS,
    SwitchLoadConfig,
)

FIELD_SWITCH = "switch_entity"
FIELD_RATED_KW = "rated_kw"
FIELD_RUNTIME_H = "runtime_h"
FIELD_TEMPERATURE = "temperature_entity"
FIELD_FLOOR_C = "floor_c"


def _number(minimum: float, maximum: float, step: float, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=selector.NumberSelectorMode.BOX
        )
    )


def _optional(key: str, value: Any) -> vol.Optional:
    return vol.Optional(key) if value is None else vol.Optional(key, description={"suggested_value": value})


def switch_load_schema(stored: Any) -> vol.Schema:
    """The page, pre-filled from the stored load (`suggested_value`, so a field can be emptied)."""
    config = SwitchLoadConfig.from_data(stored)
    values: Mapping[str, Any] = {} if config is None else config.as_dict()
    return vol.Schema(
        {
            _optional(FIELD_SWITCH, values.get(KEY_ENTITY)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=list(SWITCH_DOMAINS))
            ),
            _optional(FIELD_RATED_KW, values.get(KEY_RATED_KW)): _number(RATED_KW_MIN, RATED_KW_MAX, 0.1, "kW"),
            _optional(FIELD_RUNTIME_H, values.get(KEY_RUNTIME_H)): _number(RUNTIME_H_MIN, RUNTIME_H_MAX, 0.25, "h"),
            _optional(FIELD_TEMPERATURE, values.get(KEY_TEMPERATURE)): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            _optional(FIELD_FLOOR_C, values.get(KEY_FLOOR_C)): _number(FLOOR_C_MIN, FLOOR_C_MAX, 1.0, "°C"),
        }
    )


def parse_switch_load(user_input: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """`(load, errors)`: the load to store (`{}`: none, the switch was left empty), or the form's errors."""
    switch = user_input.get(FIELD_SWITCH)
    if not switch:
        return {}, {}
    sensor, floor = user_input.get(FIELD_TEMPERATURE) or None, user_input.get(FIELD_FLOOR_C)
    if (sensor is None) != (floor is None):
        return None, {"base": "switch_load_floor_pair"}
    data = {
        KEY_ENTITY: switch,
        KEY_RATED_KW: user_input.get(FIELD_RATED_KW),
        KEY_RUNTIME_H: user_input.get(FIELD_RUNTIME_H),
        **({} if sensor is None else {KEY_TEMPERATURE: sensor, KEY_FLOOR_C: floor}),
    }
    config = SwitchLoadConfig.from_data(data)
    if config is None:
        return None, {"base": "switch_load_incomplete"}
    return config.as_dict(), {}
