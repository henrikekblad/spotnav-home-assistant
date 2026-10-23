"""The entities of an electricity price entry's device: Price now, Cheap now, Expensive now and their hours.

Each reads its entry's `PriceDevice` and is told by it when anything it shows may have changed (a new
interval, new prices, other hours); none polls. Attributes are a handful of small values and none of them
is recorded, so the history holds the states only.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .price_device import PriceDevice
from .pricing.price_points import CHEAP, EXPENSIVE, HOURS_STEP, MAX_HOURS, resolution_minutes, rounded


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
        {"area", "currency", "resolution_minutes", "spot_price", "next_change", "tomorrow_published"}
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


def price_sensors(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [PriceNowSensor(entry, device)]


def price_binary_sensors(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [PriceRankSensor(entry, device, CHEAP), PriceRankSensor(entry, device, EXPENSIVE)]


def price_numbers(entry: ConfigEntry, device: PriceDevice) -> list[Any]:
    return [PriceHoursNumber(entry, device, CHEAP), PriceHoursNumber(entry, device, EXPENSIVE)]
