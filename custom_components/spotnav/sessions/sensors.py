"""The session sensors of one charger, for Home Assistant's own dashboards and automations.

Energy and cost this month and last month, and the last session's energy and cost. They read the session
store (memory), refresh when it changes and every ten minutes (so "this month" turns over at midnight
without a session), and are `unknown` when there is nothing to say, never zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..entity import SpotNavChargingEntity
from ..execution.controller import ChargingController
from ..runtime import domain_data
from .inputs import local_zone
from .summary import month_key, month_summary, previous_month

REFRESH_S = 600

#: (key, which, quantity)
SESSION_SENSORS = (
    ("sessions_energy_this_month", "this_month", "energy"),
    ("sessions_cost_this_month", "this_month", "cost"),
    ("sessions_energy_last_month", "last_month", "energy"),
    ("sessions_cost_last_month", "last_month", "cost"),
    ("sessions_last_energy", "last_session", "energy"),
    ("sessions_last_cost", "last_session", "cost"),
)


def session_entities(hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController) -> list[Any]:
    return [SessionSensor(hass, entry, controller, *spec) for spec in SESSION_SENSORS]


class SessionSensor(SpotNavChargingEntity, SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        controller: ChargingController,
        key: str,
        which: str,
        quantity: str,
    ) -> None:
        super().__init__(entry, controller)
        self._hass = hass
        self._which = which
        self._quantity = quantity
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        if quantity == "energy":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_suggested_display_precision = 2
        else:
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_suggested_display_precision = 2
        if which == "this_month":
            self._attr_state_class = SensorStateClass.TOTAL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        store = domain_data(self._hass).session_store
        if store is not None:
            self.async_on_remove(store.add_listener(self._entry.entry_id, self._changed))
        self.async_on_remove(
            async_track_time_interval(self._hass, self._tick, timedelta(seconds=REFRESH_S))
        )

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @callback
    def _tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    def _figures(self) -> tuple[float | None, str | None, dict[str, Any]]:
        """(energy kWh or cost, currency, extra attributes) for this sensor's slice."""
        store = domain_data(self._hass).session_store
        if store is None:
            return None, None, {}
        zone = local_zone(self._hass)
        closed = store.closed(self._entry.entry_id)
        if self._which == "last_session":
            if not closed:
                return None, None, {}
            last = max(closed, key=lambda item: item.start)
            record = last.public()
            attributes = {
                "start": record["start"],
                "end": record["end"],
                "started_by": record["started_by"],
                "estimated": record["estimated"],
                "average_price_minor_per_kwh": record["average_price_minor_per_kwh"],
                "strategy": record["strategy"],
                "vehicle": record["vehicle"],
            }
            return (
                record["energy_kwh"] if self._quantity == "energy" else record["cost"],
                last.currency,
                attributes,
            )
        today = dt_util.now().astimezone(zone).date()
        key = month_key(today) if self._which == "this_month" else previous_month(today)
        summary = month_summary(closed, zone, key)
        attributes = {
            "month": key,
            "sessions": summary["sessions"],
            "estimated": summary["estimated"],
            "average_price_minor_per_kwh": summary["average_price_minor_per_kwh"],
            "solar_share": summary["solar_share"],
            "savings_vs_day_average": summary["savings"],
            "savings_is_estimate": True,
        }
        if summary["sessions"] == 0:
            return (0.0 if self._quantity == "energy" else None), summary["currency"], attributes
        return (
            summary["energy_kwh"] if self._quantity == "energy" else summary["cost"],
            summary["currency"],
            attributes,
        )

    @property
    def native_value(self) -> float | None:
        return self._figures()[0]

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self._quantity == "energy":
            return UnitOfEnergy.KILO_WATT_HOUR
        return self._figures()[1]

    @property
    def last_reset(self) -> datetime | None:
        if self._which != "this_month":
            return None
        zone = local_zone(self._hass)
        today = dt_util.now().astimezone(zone)
        return today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._figures()[2]
