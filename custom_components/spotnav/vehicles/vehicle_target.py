"""A car's target follows the car: one target percent per vehicle, the same at every charger.

The target is a vehicle property (`vehicle_properties.KEY_TARGET`). Each charger's settings still carry their
planned car's in `target.target_percent`, which is what planning, the dashboard and an older app read; this
module keeps the two the same:

* A committed settings change whose target names a car and a percent other than the car's sets the car's
  (`AutoSettingsStore.add_write_listener`), and every other charger planning for that car follows, by a settings
  write of its own (`async_spread`).
* `update_vehicle` setting a car's target spreads it the same way (`api/entity_config.py`).
* A switch of car takes the new car's target (`api/settings.replacement_mutator`, vehicle identification).
* At a charger's set-up, a target stored before targets were the car's becomes its selected car's, unless the
  car already has one (`async_adopt`).

None of this starts, stops or owns a charge: a follower's write is an ordinary settings write.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from homeassistant.core import callback, HomeAssistant

from ..planning.auto_settings import AutoSettings, TargetSocIntent
from ..runtime import domain_data, preview_for
from . import vehicle_properties

_LOGGER = logging.getLogger(__name__)


def car_target(hass: HomeAssistant, vehicle_id: str | None) -> float | None:
    """The target the car is charged to at every charger, or `None` when it has none."""
    return vehicle_properties.stored_properties(hass, vehicle_id).target_percent


async def _async_set_car_target(hass: HomeAssistant, vehicle_id: str, percent: float) -> None:
    store = domain_data(hass).decision_store
    if store is None:
        return
    await vehicle_properties.async_update_vehicle_properties(
        hass, store, vehicle_id, {vehicle_properties.KEY_TARGET: percent}
    )


async def async_spread(hass: HomeAssistant, vehicle_id: str, *, besides: str | None = None) -> None:
    """Every charger (but `besides`) planning for `vehicle_id` takes the car's target."""
    percent = car_target(hass, vehicle_id)
    store = domain_data(hass).auto_store
    if percent is None or store is None:
        return
    for entry_id in store.entry_ids():
        target = store.settings(entry_id).target
        if entry_id == besides or target.vehicle_id != vehicle_id or target.target_percent == percent:
            continue

        def follow(current: AutoSettings) -> AutoSettings:
            if current.target.vehicle_id != vehicle_id:
                return current
            return replace(current, target=TargetSocIntent(vehicle_id=vehicle_id, target_percent=percent))

        try:
            preview = preview_for(hass, entry_id)
            if preview is not None:
                await preview.async_apply_settings(mutate=follow)
            else:
                await store.async_update(entry_id, mutate=follow)
        except Exception as err:  # noqa: BLE001 - one charger that cannot follow keeps its own target
            _LOGGER.warning("SpotNav could not give a charger its car's target: %s", getattr(err, "code", type(err).__name__))


async def _async_follow(hass: HomeAssistant, entry_id: str, settings: AutoSettings) -> None:
    vehicle_id, percent = settings.target.vehicle_id, settings.target.target_percent
    if vehicle_id is None or percent is None or car_target(hass, vehicle_id) == percent:
        return
    await _async_set_car_target(hass, vehicle_id, percent)
    await async_spread(hass, vehicle_id, besides=entry_id)


@callback
def async_setup_vehicle_targets(hass: HomeAssistant) -> None:
    """Listen to every committed settings change, once for the installation."""
    store = domain_data(hass).auto_store
    if store is None:
        return

    @callback
    def _written(entry_id: str, settings: AutoSettings) -> None:
        target = settings.target
        if target.vehicle_id is None or target.target_percent is None:
            return
        if car_target(hass, target.vehicle_id) == target.target_percent:
            return
        hass.async_create_task(_async_follow(hass, entry_id, settings), eager_start=False)

    store.add_write_listener(_written)


async def async_adopt(hass: HomeAssistant, entry_id: str) -> None:
    """A charger's target from before targets were the car's becomes its car's, unless the car has one."""
    store = domain_data(hass).auto_store
    if store is None:
        return
    target = store.settings(entry_id).target
    if target.vehicle_id is None or target.target_percent is None:
        return
    if car_target(hass, target.vehicle_id) is None:
        await _async_set_car_target(hass, target.vehicle_id, target.target_percent)
