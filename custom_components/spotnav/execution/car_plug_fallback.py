"""The car's own plug sensor, read in place of a charger that cannot tell whether a car is plugged in.

The field case of 2026-11-02: a Charge Amps charger through its cloud integration is stopped by turning its connector
off, and a connector that is off says `Unavailable` with or without a car. A person's Stop holds until the car is
unplugged, so with nothing that saw the unplug it held for days.

So while a person's Stop holds and the charger's own reading says nothing (its status `unknown` or `unavailable`, at a
charger that otherwise says when a car is plugged in), the car's plug sensor stands in for it:

* **Armed** (`arm`) by the controller when both hold, from that moment (`since`). It stays armed while the Stop
  holds, and after an unplug it gave (which ended the Stop) until the plug-in after it (`awaits_plug_in`); it is
  disarmed otherwise, when the person acts (Start, Resume, Stop again), and as soon as the charger's own reading says
  something again: the charger always wins.
* **The car** is the one the charger's plan is for (`AutoSettings.target`, the car identified for this plug-in or the
  charger's only one; never a guest car), and only its plug source as `identification_sources` resolves it, detected
  or chosen.
* **Fresh only**: a reading counts when SpotNav saw the sensor change to it, or its integration write it again, since
  the fallback was armed (`SourceChanges`, so a state re-created at a restart is no report). A reading from before the
  stop never counts.
* **At home**: "plugged in" counts only while the car's location source (its tracker, detected or chosen) does not
  say it is away: a car plugged in at work is no plug-in here. A car with no location source, or one that says
  nothing, is read by its plug sensor alone. "Unplugged" needs no location.

What it gives is the controller's connection reading (`ChargerAdapter.connection_fallback`), so an unplug and a
plug-in from it run through the one place a charger's own do (`ChargingController._observe_connection`): the
person's Stop ends at the unplug, the plug-in ends, the guest car leaves, and so on. Nothing here sends anything to
the charger or wakes the car.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..vehicles.identification_changes import SourceChanges
from ..vehicles.identification_sources import identification_sources, location_reading, plug_reading

#: The event source that says an unplug or a plug-in came from the car's own plug sensor.
SOURCE_CAR_PLUG: Final = "car_plug"


def plan_vehicle(hass: HomeAssistant, entry_id: str) -> str | None:
    """The car a charger's plan is for (the identified car, or its only one), `None` for a guest car or none."""
    from ..runtime import domain_data
    from ..vehicles.vehicle_discovery import resolve_target_vehicle

    store = domain_data(hass).auto_store
    if store is None:
        return None
    settings = store.settings(entry_id)
    return resolve_target_vehicle(hass, settings.target.vehicle_id, settings.vehicle_ids)[0]


class CarPlugFallback:
    """One charger's stand-in reading from its car's plug sensor. `vehicle` names the car; `on_update` is told when
    the watched sensor changes or reports."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        vehicle: Callable[[], str | None],
        on_update: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._vehicle = vehicle
        self._changes = SourceChanges(hass, on_update)
        #: When it was armed (`None`: not armed).
        self._since: datetime | None = None
        self._vehicle_id: str | None = None
        self._entity_id: str | None = None
        #: The car's location source (`None`: none).
        self._location_id: str | None = None
        #: The last unplug or plug-in it gave: when, which, from which sensor.
        self._last: dict[str, Any] | None = None
        #: It gave an unplug, and no plug-in since: it stays armed for that plug-in.
        self._awaits_plug_in = False

    @property
    def armed(self) -> bool:
        return self._since is not None

    @property
    def awaits_plug_in(self) -> bool:
        """Whether it gave an unplug (which ended the Stop) and the plug-in after it is still to come."""
        return self._since is not None and self._awaits_plug_in

    def arm(self) -> None:
        """Read the car's sensor from now on (nothing it said before counts)."""
        if self._since is not None:
            return
        self._since = dt_util.utcnow()
        self._awaits_plug_in = False
        self.refresh()

    def disarm(self) -> None:
        """The charger says again: its own reading is what counts."""
        if self._since is None:
            return
        self._since = None
        self._awaits_plug_in = False
        self._vehicle_id = self._entity_id = self._location_id = None
        self._changes.watch(())

    def refresh(self) -> None:
        """Find the car and its plug source again (a car identified or a source chosen meanwhile)."""
        if self._since is None:
            return
        vehicle = self._vehicle()
        sources = None if vehicle is None else identification_sources(self._hass, vehicle)
        entity_id = None if sources is None else sources[0].entity_id
        self._location_id = None if sources is None or entity_id is None else sources[1].entity_id
        if (vehicle, entity_id) != (self._vehicle_id, self._entity_id) and self._vehicle_id is not None:
            # Another car (or another sensor of it): nothing it said before now counts.
            self._since = dt_util.utcnow()
            self._awaits_plug_in = False
        self._vehicle_id, self._entity_id = vehicle, entity_id
        # The location is watched too, so a car that comes home is read again.
        self._changes.watch(tuple(item for item in (entity_id, self._location_id) if item is not None))

    def reading(self) -> bool | None:
        """What the car's plug sensor says, fresh since it was armed: plugged in, unplugged, or `None` (not armed, no
        sensor, nothing it says, or nothing said since)."""
        since, entity_id = self._since, self._entity_id
        if since is None or entity_id is None:
            return None
        state = self._hass.states.get(entity_id)
        value = plug_reading(state)
        if value is None:
            return None
        stamps = [stamp for stamp in (self._changes.changed(state), self._changes.reported(state)) if stamp is not None]
        if not stamps or max(stamps) < since:
            return None
        if value and self._location_id is not None and location_reading(self._hass.states.get(self._location_id)) is False:
            # Plugged in elsewhere (at work): no plug-in at this charger.
            return None
        return value

    @property
    def usable(self) -> bool:
        """Whether the car's sensor can tell an unplug now: it says something fresh since it was armed."""
        return self.reading() is not None

    def note(self, connected: bool) -> None:
        """The controller took an unplug or a plug-in from it."""
        self._awaits_plug_in = not connected
        self._last = {
            "at": dt_util.utcnow().isoformat(),
            "connected": connected,
            "vehicle_id": self._vehicle_id,
            "entity_id": self._entity_id,
        }

    def shutdown(self) -> None:
        self._changes.stop()

    def diagnostics(self) -> dict[str, Any]:
        """Ids and times only."""
        return {
            "armed_since": None if self._since is None else self._since.isoformat(),
            "vehicle_id": self._vehicle_id,
            "entity_id": self._entity_id,
            "reading": self.reading(),
            "awaits_plug_in": self.awaits_plug_in,
            "last": self._last,
        }
