"""Numbers: a charger's Auto inputs, and an electricity price device's hours and fee figures (`price_entities.py`).

Entity bounds are UI assistance, never a second validator: the backend still refuses what it
cannot store (`AutoSettings.validated`). An absent setting reads `unknown`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .price_entities import price_numbers, remove_charger_price_entities
from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavAutoEntity
from .execution.controller import ABSOLUTE_MAX_AMPS, ABSOLUTE_MIN_AMPS, ChargingController
from .runtime import ChargerConfigEntry, price_data


#: The widest inputs the UI offers; not a backend limit (energy has no upper bound in the model).
MAX_ENERGY_KWH = 1000.0


def whole_number(value: float) -> float:
    """The value as an `int` when exactly integral, unchanged otherwise.

    Home Assistant hands number entities floats (`13.0`) but the model stores whole amperes and
    current as `int`. `13.4` passes through so the backend refuses it by name.
    """
    return int(value) if float(value).is_integer() else value


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if (price := price_data(hass, entry.entry_id)) is not None:
        # An electricity price entry's device: its hours and fee figures (`price_entities.py`).
        async_add_entities(price_numbers(entry, price.device))
        return
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return
    controller = entry.runtime_data.controller
    auto = AutoSurface.resolve(hass, entry.entry_id)
    # The charge periods became a select with "automatic" (`select.py`): the number an older release created
    # is removed.
    registry = er.async_get(hass)
    leftover = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_maximum_periods")
    if leftover is not None:
        registry.async_remove(leftover)
    # The fee figures moved to the electricity price device: the charger's former ones are removed.
    remove_charger_price_entities(hass, "number", entry.entry_id)
    async_add_entities(
        [
            AutoCurrentNumber(entry, controller, auto),
            AutoEnergyNumber(entry, controller, auto),
        ]
    )


class AutoSettingNumber(SpotNavAutoEntity, NumberEntity):
    """Base for the numbers that write one field of the settings record.

    Writes go through `async_write_settings` with the displayed revision as `expected_revision`.
    """

    _attr_entity_category = EntityCategory.CONFIG

    _attr_mode = NumberMode.BOX

    async def async_set_native_value(self, value: float) -> None:
        """Store the value as given; the backend decides whether it can be stored."""
        await self.async_write_settings(self.store_value(value))

    def store_value(self, value: float) -> Any:
        """The mutator for this number's own field. Subclasses implement exactly this."""
        raise NotImplementedError


class AutoCurrentNumber(AutoSettingNumber):
    """The current Auto plans with, in whole amperes."""

    _attr_translation_key = "charging_current"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_step = 1
    _attr_native_min_value = float(ABSOLUTE_MIN_AMPS)
    _attr_native_max_value = float(ABSOLUTE_MAX_AMPS)

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="charging_current")

    @property
    def native_min_value(self) -> float:
        """Never wider than the backend's bounds; narrower when the charger says so.

        Narrowing reads the current-limit entity's own `min`/`max` attributes.
        """
        return float(max(ABSOLUTE_MIN_AMPS, self._limit_attr("min", ABSOLUTE_MIN_AMPS)))

    @property
    def native_max_value(self) -> float:
        return float(min(ABSOLUTE_MAX_AMPS, self._limit_attr("max", ABSOLUTE_MAX_AMPS)))

    def _limit_attr(self, name: str, fallback: int) -> float:
        """One bound from the configured current-limit entity, or the fallback."""
        entity_id = self.controller.current_limit
        if not entity_id:
            return float(fallback)
        state = self.hass.states.get(entity_id)
        if state is None:
            return float(fallback)
        value = state.attributes.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return float(fallback)
        return float(value)

    @property
    def native_value(self) -> float | None:
        """The stored current, or `unknown` when Auto has none yet."""
        settings = self.settings
        return None if settings is None else settings.amps

    def store_value(self, value: float) -> Any:
        whole = whole_number(value)
        return lambda settings: replace(settings, amps=whole)

class AutoEnergyNumber(AutoSettingNumber):
    """How much energy Auto should deliver before the departure time.

    The step is 0.1 kWh because the number card walks `min + k * step` from the minimum: with
    `min = 0.1` and `step = 0.5` a plain `42.0` would be off the grid and refused by the box.
    """

    _attr_translation_key = "requested_energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 0.1
    _attr_native_max_value = MAX_ENERGY_KWH
    _attr_native_step = 0.1

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="requested_energy")

    @property
    def native_value(self) -> float | None:
        settings = self.settings
        return None if settings is None else settings.requested_kwh

    def store_value(self, value: float) -> Any:
        # An amount, so not "Fill" any more.
        return lambda settings: replace(settings, requested_kwh=value, fill_to_limit=False)
