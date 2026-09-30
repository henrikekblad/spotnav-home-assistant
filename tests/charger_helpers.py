"""Builders shared by the charger adapter, controller and flow tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.spotnav.config_flow.charger_detection import DetectedCharger
from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_CHARGER_PLATFORM,
    CONF_CHARGING_STATE,
    CONF_CONTROL_PATH,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_MODE,
    MODE_DETECTED,
)
from custom_components.spotnav.execution.charger_adapter import build_adapter, ChargerAdapter


class Clock:
    """A clock a test moves by hand, for the adapter's rate limiter."""

    def __init__(self) -> None:
        self.current = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def detected_config(found: DetectedCharger, *, current_control: str | None = None) -> dict[str, Any]:
    """The config-entry data a detected charger is stored with (what the flow writes), opted in to
    the current control detection suggested unless `current_control` says otherwise.
    """
    return {
        CONF_MODE: MODE_DETECTED,
        CONF_CHARGER_PLATFORM: found.platform,
        CONF_CHARGE_CONTROL: found.charge_control,
        CONF_CONTROL_PATH: found.control_path,
        CONF_CURRENT_LIMIT: found.current_limit or "",
        CONF_CURRENT_CONTROL: found.current_control if current_control is None else current_control,
        CONF_ENERGY_REGISTER_ENTITY: found.energy_register or "",
        CONF_CHARGING_STATE: found.charging_state,
        CONF_CHARGER_CURRENT_ENTITIES: list(found.current_entities),
    }


def adapter_for(
    hass: HomeAssistant,
    found: DetectedCharger,
    *,
    clock: Clock | None = None,
    current_control: str | None = None,
) -> ChargerAdapter:
    config = detected_config(found, current_control=current_control)
    return build_adapter(
        hass,
        config,
        ocpp_target=lambda: None,
        energy_entity_id=found.energy_register,
        now=clock or Clock(),
    )
