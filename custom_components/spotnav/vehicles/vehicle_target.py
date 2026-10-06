"""A car's target follows the car: one target percent per vehicle, the same at every charger.

The target is a vehicle property (`vehicle_properties.KEY_TARGET`). Each charger's settings still carry their
planned car's in `target.target_percent`, which is what planning, the dashboard and an older app read; this
module keeps the two the same:

* A committed settings change that changes its target (the car or the percent) to a car and a percent other than
  the car's makes that the car's newest target. A change of anything else (a departure, the current) moves no
  target. The newest target wins: it is noted at once, before any write is awaited, and every follow-up writes
  the newest one, so two quick edits at two chargers end on the later one everywhere (`_async_follow`).
* Every other charger planning for that car follows, by a settings write of its own (`async_spread`).
* `update_vehicle` setting a car's target is a newest target as well (`note_target`, then `async_spread`).
* A switch of car takes the new car's target (`api/settings.replacement_mutator`, vehicle identification).
* At a charger's set-up, a car with no target of its own takes the highest target any charger planned it to before
  this release (a car is never left short), and every charger planning for it is aligned once (`async_adopt`).

None of this starts, stops or owns a charge: a follower's write is an ordinary settings write.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from homeassistant.core import callback, HomeAssistant
from homeassistant.util.hass_dict import HassKey

from ..const import DOMAIN
from ..planning.auto_settings import AutoSettings, TargetSocIntent
from ..runtime import domain_data, preview_for
from . import vehicle_properties

_LOGGER = logging.getLogger(__name__)

#: The newest target each car was given, noted when it was given (before any write is awaited).
_NEWEST: HassKey[dict[str, float]] = HassKey(f"{DOMAIN}_newest_vehicle_target")


def car_target(hass: HomeAssistant, vehicle_id: str | None) -> float | None:
    """The target the car is charged to at every charger, or `None` when it has none."""
    return vehicle_properties.stored_properties(hass, vehicle_id).target_percent


def note_target(hass: HomeAssistant, vehicle_id: str, percent: float | None) -> None:
    """Make `percent` the car's newest target: every follow-up from now on writes it (`None`: it has none)."""
    newest = hass.data.setdefault(_NEWEST, {})
    if percent is None:
        newest.pop(vehicle_id, None)
    else:
        newest[vehicle_id] = percent


def _newest(hass: HomeAssistant, vehicle_id: str) -> float | None:
    newest = hass.data.get(_NEWEST, {}).get(vehicle_id)
    return car_target(hass, vehicle_id) if newest is None else newest


async def _async_set_car_target(hass: HomeAssistant, vehicle_id: str, percent: float) -> None:
    store = domain_data(hass).decision_store
    if store is None or car_target(hass, vehicle_id) == percent:
        return
    await vehicle_properties.async_update_vehicle_properties(
        hass, store, vehicle_id, {vehicle_properties.KEY_TARGET: percent}
    )


async def async_spread(hass: HomeAssistant, vehicle_id: str, *, besides: str | None = None) -> None:
    """Every charger (but `besides`) planning for `vehicle_id` takes the car's newest target."""
    store = domain_data(hass).auto_store
    if store is None:
        return
    for entry_id in store.entry_ids():
        percent = _newest(hass, vehicle_id)
        target = store.settings(entry_id).target
        if percent is None or entry_id == besides or target.vehicle_id != vehicle_id or target.target_percent == percent:
            continue

        def follow(current: AutoSettings, percent: float = percent) -> AutoSettings:
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


async def _async_follow(hass: HomeAssistant, vehicle_id: str) -> None:
    """The car and every charger planning for it take the car's newest target (not the one this task saw)."""
    percent = _newest(hass, vehicle_id)
    if percent is None:
        return
    await _async_set_car_target(hass, vehicle_id, percent)
    await async_spread(hass, vehicle_id)


@callback
def async_setup_vehicle_targets(hass: HomeAssistant) -> None:
    """Listen to every committed settings change, once for the installation."""
    store = domain_data(hass).auto_store
    if store is None:
        return

    @callback
    def _written(_entry_id: str, before: AutoSettings, after: AutoSettings) -> None:
        target = after.target
        if target == before.target or target.vehicle_id is None or target.target_percent is None:
            # Only a write that changed the target itself gives the car one.
            return
        if _newest(hass, target.vehicle_id) == target.target_percent:
            return
        note_target(hass, target.vehicle_id, target.target_percent)
        hass.async_create_task(_async_follow(hass, target.vehicle_id), eager_start=False)

    store.add_write_listener(_written)


async def async_adopt(hass: HomeAssistant, entry_id: str) -> None:
    """A car with no target of its own takes the highest target any charger planned it to, and every charger
    planning for it is aligned once."""
    store = domain_data(hass).auto_store
    if store is None:
        return
    car = store.settings(entry_id).target.vehicle_id
    if car is None or car_target(hass, car) is not None:
        return
    planned = [
        settings.target.target_percent
        for settings in (store.settings(other) for other in store.entry_ids())
        if settings.target.vehicle_id == car and settings.target.target_percent is not None
    ]
    if not planned:
        return
    note_target(hass, car, max(planned))
    await _async_set_car_target(hass, car, max(planned))
    await async_spread(hass, car)
