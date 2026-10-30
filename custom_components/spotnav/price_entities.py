"""The entities of an electricity price entry's device: Price now, Cheap now, Expensive now and their hours, and
the area and fees the device prices with.

Each reads its entry's `PriceDevice` and is told by it when anything it shows may have changed (a new
interval, new prices, other hours, other fees); none polls. Attributes are a handful of small values and none of
them is recorded, so the history holds the states only.

The area select, the three fee policy selects and the three fee numbers write the entry's area and fees in place
(`pricing/price_source.py:async_write_price_entry`): every charger that prices with the entry follows. They were a
charger's own until the price device took the area and fees over; a charger's set is removed from the entity
registry at its setup (`remove_charger_price_entities`).
"""

from __future__ import annotations

from typing import Any

from dataclasses import replace

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .entity import (
    FISCAL_COMPONENTS,
    FISCAL_MANUAL,
    FISCAL_OPTIONS,
    fiscal_policy,
    FISCAL_SUGGESTED,
    fiscal_view,
    with_fiscal_policy,
)
from .planning.auto_settings import AreaAutoSettings, AutoSettingsError
from .price_device import PriceDevice
from .pricing.price_points import (
    CHEAP,
    default_fee_mode,
    EXPENSIVE,
    fee_override,
    HOURS_STEP,
    included,
    MAX_HOURS,
    resolution_minutes,
    rounded,
    suggestion,
)
from .pricing.price_source import async_write_price_entry


#: Currencies whose major unit is worth about a euro or more: their prices need three decimals to move.
_FINE_CURRENCIES = frozenset({"EUR", "GBP", "CHF"})


def _iso(moment: Any) -> str | None:
    return None if moment is None else moment.isoformat()


class SpotNavPriceEntity(Entity):
    """One entity of an electricity price device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, device: PriceDevice, key: str) -> None:
        self._entry = entry
        self.device = device
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Sensnology",
            model="SpotNav electricity price",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.device.add_listener(self.async_write_ha_state))


class PriceNowSensor(SpotNavPriceEntity, SensorEntity):
    """The price of the current interval with fees and VAT, in the area's major unit per kWh."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"area", "currency", "resolution_minutes", "spot_price", "next_change", "tomorrow_published", "source"}
    )

    def __init__(self, entry: ConfigEntry, device: PriceDevice) -> None:
        super().__init__(entry, device, "price_now")

    @property
    def available(self) -> bool:
        return self.device.current() is not None

    @property
    def native_value(self) -> float | None:
        point = self.device.current()
        return None if point is None else rounded(point.price)

    @property
    def native_unit_of_measurement(self) -> str | None:
        area = self.device.area
        return None if area is None or not area.major_unit else f"{area.major_unit}/kWh"

    @property
    def suggested_display_precision(self) -> int | None:
        area = self.device.area
        return 3 if area is not None and area.currency.upper() in _FINE_CURRENCIES else 2

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.device
        area = device.area
        point = device.current()
        return {
            "area": device.area_id,
            "currency": None if area is None else area.currency,
            "resolution_minutes": None if point is None else resolution_minutes((point,)),
            "spot_price": None if point is None else rounded(point.spot),
            "next_change": _iso(device.next_price_change()),
            "tomorrow_published": device.tomorrow_published,
            # Where the prices come from: `nordpool`, `entsoe`, `pvpc`, `octopus`, or `None` with no source.
            "source": device.source,
        }


class PriceRankSensor(SpotNavPriceEntity, BinarySensorEntity):
    """On during the day's cheapest (or dearest) hours, as many as its number says."""

    _unrecorded_attributes = frozenset({"threshold", "next_change"})

    def __init__(self, entry: ConfigEntry, device: PriceDevice, kind: str) -> None:
        super().__init__(entry, device, f"{kind}_now")
        self._kind = kind

    @property
    def available(self) -> bool:
        return self.device.current() is not None

    @property
    def is_on(self) -> bool | None:
        return self.device.is_on(self._kind)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        threshold = self.device.threshold(self._kind)
        return {
            "threshold": None if threshold is None else rounded(threshold),
            "next_change": _iso(self.device.next_switch(self._kind)),
        }


class PriceHoursNumber(SpotNavPriceEntity, RestoreNumber):
    """How many hours a day `Cheap now` (or `Expensive now`) is on: 0 to 24 in quarter-hours, restored."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0.0
    _attr_native_max_value = MAX_HOURS
    _attr_native_step = HOURS_STEP
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(self, entry: ConfigEntry, device: PriceDevice, kind: str) -> None:
        super().__init__(entry, device, f"{kind}_hours")
        self._kind = kind

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        value = None if last is None else last.native_value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_HOURS:
            self.device.set_hours(self._kind, value)

    @property
    def native_value(self) -> float:
        return self.device.hours(self._kind)

    async def async_set_native_value(self, value: float) -> None:
        self.device.set_hours(self._kind, value)
        self.async_write_ha_state()


#: The largest fee figure the numbers offer, in the area's minor unit per kWh (UI help, not a backend limit).
MAX_FEE_MINOR_UNITS = 10000.0

#: The fee numbers' keys (unique id suffix and translation key), per component.
FEE_NUMBER_KEYS = {"vat": "vat_rate", "tax": "energy_tax", "transfer": "transfer_fee"}

#: The fee policy selects' keys, per component.
FEE_POLICY_KEYS = {component: f"fiscal_{component}_policy" for component in FISCAL_COMPONENTS}

#: The area and fee entities a charger had before the price device took them over, by platform: removed from the
#: entity registry at the charger's setup.
CHARGER_PRICE_KEYS = {
    "select": ("price_area", *FEE_POLICY_KEYS.values()),
    "number": tuple(FEE_NUMBER_KEYS.values()),
}


def remove_charger_price_entities(hass: HomeAssistant, platform: str, charger_entry_id: str) -> None:
    """Remove a charger's former area and fee entities of one platform from the entity registry. Idempotent: an
    entity already gone is passed over."""
    registry = er.async_get(hass)
    for key in CHARGER_PRICE_KEYS[platform]:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{charger_entry_id}_{key}")
        if entity_id is not None:
            registry.async_remove(entity_id)


class SpotNavPriceSetting(SpotNavPriceEntity):
    """An entity that writes the entry's area or fees."""

    _attr_entity_category = EntityCategory.CONFIG

    def write(self, overrides: AreaAutoSettings) -> None:
        """Store the area and fees on the entry, in place; a refusal is the person's to read."""
        try:
            async_write_price_entry(self.hass, self._entry, overrides)
        except AutoSettingsError as err:
            raise ServiceValidationError(str(err), translation_domain=DOMAIN, translation_key=err.code) from err


class PriceAreaSelect(SpotNavPriceSetting, SelectEntity):
    """The market whose prices the device uses, as the bundled area list states it."""

    def __init__(self, entry: ConfigEntry, device: PriceDevice) -> None:
        super().__init__(entry, device, "price_area")

    @property
    def options(self) -> list[str]:
        """The catalogue's area ids, plus the configured one when the catalogue cannot say."""
        catalogue = self.device.catalogue()
        ids = [] if catalogue.catalogue is None else list(catalogue.catalogue.area_ids)
        if self.device.area_id and self.device.area_id not in ids:
            ids.append(self.device.area_id)
        return ids

    @property
    def current_option(self) -> str | None:
        return self.device.area_id or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The catalogue names for the options, and the chosen area's money facts."""
        catalogue = self.device.catalogue()
        names: dict[str, str] = {}
        if catalogue.catalogue is not None:
            names = {area.id: area.name for area in catalogue.catalogue.areas}
        chosen = self.device.area
        return {
            "area_names": names,
            "catalogue_state": catalogue.state,
            "currency": None if chosen is None else chosen.currency,
            "major_unit": None if chosen is None else chosen.major_unit,
            "minor_unit": None if chosen is None else chosen.minor_unit,
            "selected_name": None if chosen is None else chosen.name,
        }

    async def async_select_option(self, option: str) -> None:
        """Another area: its fees start from the area's own suggestions, as a new electricity price's do (a fee
        figure is in one area's unit and never carried to another)."""
        if option == self.device.area_id:
            return
        area = self.device.catalogue().area(option)
        if area is None:
            raise ServiceValidationError(
                f"{option!r} is not a price area", translation_domain=DOMAIN, translation_key="invalid_area"
            )
        self.write(
            AreaAutoSettings(
                area_id=option,
                **{component: fee_override(default_fee_mode(area, component), None) for component in FISCAL_COMPONENTS},
            )
        )


class PriceFeePolicySelect(SpotNavPriceSetting, SelectEntity):
    """One fee's three-state policy: off, the area's suggestion, or a figure of the person's own."""

    _attr_options = list(FISCAL_OPTIONS)

    def __init__(self, entry: ConfigEntry, device: PriceDevice, component: str) -> None:
        super().__init__(entry, device, FEE_POLICY_KEYS[component])
        self._component = component

    @property
    def available(self) -> bool:
        """Unavailable without an area, and for a fee the area's published price already includes."""
        return bool(self.device.area_id) and not included(self.device.area, self._component)

    @property
    def current_option(self) -> str | None:
        return fiscal_policy(getattr(self.device.overrides, self._component))

    async def async_select_option(self, option: str) -> None:
        overrides = self.device.overrides
        try:
            replaced = with_fiscal_policy(getattr(overrides, self._component), option)
        except AutoSettingsError as err:
            raise ServiceValidationError(str(err), translation_domain=DOMAIN, translation_key=err.code) from err
        self.write(replace(overrides, **{self._component: replaced}))


class PriceFeeValueNumber(SpotNavPriceSetting, NumberEntity):
    """One fee figure, in the area's own unit (`%` for VAT, the minor unit per kWh otherwise).

    Setting it also selects `manual` for the same fee in one write, so a figure is never stored beside an "off" or
    "suggested" policy.
    """

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_step = 0.1

    def __init__(self, entry: ConfigEntry, device: PriceDevice, component: str) -> None:
        super().__init__(entry, device, FEE_NUMBER_KEYS[component])
        self._component = component

    @property
    def available(self) -> bool:
        return bool(self.device.area_id) and not included(self.device.area, self._component)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._component == "vat":
            return PERCENTAGE
        area = self.device.area
        return None if area is None or not area.minor_unit else f"{area.minor_unit}/kWh"

    @property
    def native_max_value(self) -> float:
        return 100.0 if self._component == "vat" else MAX_FEE_MINOR_UNITS

    @property
    def view(self) -> tuple[str, float | None, str]:
        """`(policy, effective value, source)`, from the one implementation of that reading."""
        return fiscal_view(getattr(self.device.overrides, self._component), suggestion(self.device.area, self._component))

    @property
    def native_value(self) -> float | None:
        """The figure in effect: the person's own, or the area's suggestion; `off` still shows a kept figure."""
        if self.view[2] == "suggested":
            return self.view[1]
        return getattr(self.device.overrides, self._component).value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The figures and the policy, so an off-but-kept figure is never read as charged."""
        stored = getattr(self.device.overrides, self._component)
        policy, effective, source = self.view
        return {
            "policy": policy,
            "explicit_value": stored.value,
            "suggested_value": suggestion(self.device.area, self._component),
            "effective_value": effective,
            "value_source": source,
            "effective": policy in (FISCAL_MANUAL, FISCAL_SUGGESTED) and effective is not None,
        }

    async def async_set_native_value(self, value: float) -> None:
        overrides = self.device.overrides
        updated = replace(getattr(overrides, self._component), enabled=True, value=value)
        try:
            updated.validated(f"{self._component} for {overrides.area_id}")
        except AutoSettingsError as err:
            raise ServiceValidationError(str(err), translation_domain=DOMAIN, translation_key=err.code) from err
        self.write(replace(overrides, **{self._component: updated}))


def price_sensors(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [PriceNowSensor(entry, device)]


def price_binary_sensors(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [PriceRankSensor(entry, device, CHEAP), PriceRankSensor(entry, device, EXPENSIVE)]


def price_numbers(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [
        PriceHoursNumber(entry, device, CHEAP),
        PriceHoursNumber(entry, device, EXPENSIVE),
        *(PriceFeeValueNumber(entry, device, component) for component in FISCAL_COMPONENTS),
    ]


def price_selects(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [
        PriceAreaSelect(entry, device),
        *(PriceFeePolicySelect(entry, device, component) for component in FISCAL_COMPONENTS),
    ]
