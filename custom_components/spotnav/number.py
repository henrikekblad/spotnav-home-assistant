"""Numbers for the Auto price path: this charger's inputs and the fiscal figures.

Entity bounds are UI assistance, never a second validator: the backend still refuses what it
cannot store (`AutoSettings.validated`). An absent setting reads `unknown`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .entity import (
    AutoSurface,
    FISCAL_COMPONENTS,
    FISCAL_MANUAL,
    FISCAL_OFF,
    fiscal_policy,
    FISCAL_SUGGESTED,
    fiscal_view,
    SpotNavAutoEntity,
)
from .execution.controller import ABSOLUTE_MAX_AMPS, ABSOLUTE_MIN_AMPS, ChargingController
from .planning.auto_controller import component_included
from .planning.auto_settings import AutoSettingsError
from .runtime import ChargerConfigEntry


#: The widest inputs the UI offers; not a backend limit (energy has no upper bound in the model).
MAX_ENERGY_KWH = 1000.0
MAX_FISCAL_MINOR_UNITS = 10000.0

VAT_COMPONENT = "vat"

FISCAL_ENTITY_KEYS = {"vat": "vat_rate", "tax": "energy_tax", "transfer": "transfer_fee"}


def whole_number(value: float) -> float:
    """The value as an `int` when exactly integral, unchanged otherwise.

    Home Assistant hands number entities floats (`13.0`) but the model stores whole amperes and
    periods as `int`. `13.4` passes through so the backend refuses it by name.
    """
    return int(value) if float(value).is_integer() else value


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return
    controller = entry.runtime_data.controller
    auto = AutoSurface.resolve(hass, entry.entry_id)
    entities: list[Any] = [
        AutoCurrentNumber(entry, controller, auto),
        AutoEnergyNumber(entry, controller, auto),
        AutoPeriodsNumber(entry, controller, auto),
    ]
    entities.extend(
        AutoFiscalValueNumber(entry, controller, auto, component)
        for component in FISCAL_COMPONENTS
    )
    async_add_entities(entities)


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
        return lambda settings: replace(settings, requested_kwh=value)


class AutoPeriodsNumber(AutoSettingNumber):
    """How many separate charging periods one plan may use, 1 to 8."""

    _attr_translation_key = "maximum_periods"
    _attr_native_min_value = 1
    _attr_native_max_value = 8
    _attr_native_step = 1

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="maximum_periods")

    @property
    def native_value(self) -> float | None:
        settings = self.settings
        return None if settings is None else settings.max_periods

    def store_value(self, value: float) -> Any:
        whole = whole_number(value)
        return lambda settings: replace(settings, max_periods=whole)


class AutoFiscalValueNumber(SpotNavAutoEntity, NumberEntity):
    """One fiscal figure for the selected market, in that market's own unit.

    Setting it also selects `manual` for the same component in one atomic change, so a figure is
    never stored beside an "off" or "suggested" policy. The unit comes from the catalogue entry
    (`%` for VAT, the area's minor unit per kWh otherwise), never from an area id or locale.
    """

    _attr_entity_category = EntityCategory.CONFIG

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_step = 0.1

    def __init__(
        self,
        entry: ConfigEntry,
        controller: ChargingController,
        auto: AutoSurface,
        component: str,
    ) -> None:
        super().__init__(entry, controller, auto, key=FISCAL_ENTITY_KEYS[component])
        self._component = component
        if component == VAT_COMPONENT:
            self._attr_translation_key = "vat_rate"
        else:
            self._attr_translation_key = "energy_tax" if component == "tax" else "transfer_fee"

    @property
    def available(self) -> bool:
        """Unavailable without a market (a fiscal figure is denominated in one), and for a component the
        market's published price already includes: no figure is added for it."""
        return (
            super().available
            and self.override is not None
            and not component_included(self.area, self._component)
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        """`%` for VAT, and the selected market's minor unit per kWh for the others."""
        if self._component == VAT_COMPONENT:
            return PERCENTAGE
        chosen = self.area
        if chosen is None or not chosen.minor_unit:
            return None
        return f"{chosen.minor_unit}/kWh"

    @property
    def native_max_value(self) -> float:
        return 100.0 if self._component == VAT_COMPONENT else MAX_FISCAL_MINOR_UNITS

    @property
    def component(self):
        """This component's stored record for the selected market, or `None`."""
        overrides = self.override
        return None if overrides is None else getattr(overrides, self._component)

    @property
    def policy(self) -> str | None:
        """`off`, `suggested` or `manual`, or `None` when no market is selected."""
        component = self.component
        return None if component is None else fiscal_policy(component)

    def suggested(self) -> float | None:
        """The selected market's own suggestion for this component, or `None`.

        Read from the catalogue, never derived from the explicit value nor written back.
        """
        chosen = self.area
        if chosen is None:
            return None
        return {
            VAT_COMPONENT: chosen.vat_percent,
            "tax": chosen.suggested_tax,
            "transfer": chosen.suggested_grid_fee,
        }[self._component]

    @property
    def view(self) -> tuple[str, float | None, str]:
        """`(policy, effective value, source)`, from the one implementation of that reading."""
        component = self.component
        if component is None:
            return (FISCAL_OFF, None, "off")
        return fiscal_view(component, self.suggested())

    @property
    def value_source(self) -> str:
        """Where the number a reader sees comes from, as a stable code."""
        return self.view[2]

    @property
    def effective(self) -> float | None:
        """The figure the planner actually applies, or `None` when nothing is applied."""
        return self.view[1]

    @property
    def native_value(self) -> float | None:
        """What this figure is right now (read, never written).

        `manual` shows the explicit value; `suggested` shows the catalogue's suggestion, i.e. the number
        that enters the plan; `off` still shows a retained explicit value. Unknown only when nothing
        exists to show.
        """
        if self.value_source == "manual":
            component = self.component
            return None if component is None else component.value
        if self.value_source == "suggested":
            return self.suggested()
        component = self.component
        return None if component is None else component.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The four figures and the policy, so an off-but-retained value is never read as charged."""
        component = self.component
        return {
            "policy": self.policy,
            "explicit_value": None if component is None else component.value,
            "suggested_value": self.suggested(),
            "effective_value": self.effective,
            "value_source": self.value_source,
            "effective": self.policy in (FISCAL_MANUAL, FISCAL_SUGGESTED) and self.effective is not None,
        }

    async def async_set_native_value(self, value: float) -> None:
        """Store the figure for this market and select its manual policy, atomically."""
        overrides = self.override
        if overrides is None:
            raise ServiceValidationError(
                "Select a market first: a fiscal figure belongs to one.",
                translation_domain=DOMAIN,
                translation_key="invalid_area",
            )
        component = getattr(overrides, self._component)
        updated = replace(component, enabled=True, value=value)
        try:
            updated.validated(f"{self._component} for {overrides.area_id}")
        except AutoSettingsError as err:
            raise ServiceValidationError(
                str(err), translation_domain=DOMAIN, translation_key=err.code
            ) from err
        await self.async_write_settings(
            lambda settings: settings.with_override(
                replace(overrides, **{self._component: updated})
            )
        )
