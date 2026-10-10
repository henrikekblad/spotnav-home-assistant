"""The state of charge a target is planned and stopped against, read honestly.

A car's state-of-charge sensor is often an hourly cloud poll that goes stale mid-charge; a
charger that reports the car's state of charge is live. Either way the question is the current
state of charge and how sure we are, answered in three layers:

* `resolve_soc` is pure: raw reading, kept anchor, the charger's cumulative energy register and
  the battery size in; effective reading and updated anchor out. The whole policy lives here.
* `SocReader` is the stateful shell: reads the raw sources, keeps the anchor (persisted so a
  restart mid-charge keeps the estimate), watches the sources, and serves both the planner and
  the stop decision, so the planned and the enforced state of charge are one number.
* `target_need_kwh` is the wall energy a target needs, including the charging loss.

    soc_est = soc_at_last_fresh_reading + delivered_since_kwh x efficiency / capacity_kwh x 100

`delivered_since_kwh` is the change of the charger's energy register since the reading. The
result is flagged `estimated`, a fresh reading (no older than `target_stop.SOC_FRESH_MAX_AGE_S`)
always replaces it, and it is never invented: with no register, no capacity or a register that
went backwards the raw reading is reported as is. Nothing here writes to a charger or a car.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..execution.target_stop import (
    charge_ceiling_percent,
    charger_soc_entity_id,
    SOC_FRESH_MAX_AGE_S,
    SocReading,
    SocSource,
)
from ..planning.planner import resolve_target_energy, TargetEnergyRequest
from ..util import finite_number
from .vehicle_discovery import (
    _device_name,
    charger_vehicle_ids,
    discover_vehicles,
    resolve_target_vehicle,
    vehicle_soc_entity_id,
)


_LOGGER = logging.getLogger(__name__)

#: Wall energy to battery energy (AC charging loses roughly 8-12 %). Used in both directions, so
#: an error cancels between planning and stopping instead of compounding.
CHARGE_EFFICIENCY: Final = 0.9

#: A register step back smaller than this is noise; a bigger one is a meter reset and voids the
#: estimate (never negative delivery).
REGISTER_TOLERANCE_KWH: Final = 0.05

#: Two readings whose instants differ by less than this are the same reading seen twice.
SAME_READING_S: Final = 1.0

#: How long a vehicle's reported capacity and ceiling are reused (a pack size does not change).
FACTS_CACHE_S: Final = 300.0

#: A reading that moved less than this since the planner last heard of it does not trigger a
#: new calculation.
RECALCULATE_DELTA_PERCENT: Final = 2.0

_STORE_VERSION: Final = 1
_STORE_KEY_PREFIX: Final = f"{DOMAIN}.soc_anchor"


@dataclass(frozen=True, slots=True)
class SocAnchor:
    """The last real reading and the energy register when it was taken.

    `register_kwh` is `None` when the register was unreadable then: such an anchor carries a
    reading, and an estimate only once the register's first readable value has become its baseline
    (`resolve_soc`). `read_at` is when the reading was made, not noticed. `register_entity_id` names
    the register `register_kwh` was read from: a baseline from another register (one found again, or
    chosen by a person) is dropped and taken again, never subtracted across two meters.
    """

    soc_percent: float
    register_kwh: float | None
    read_at: datetime
    source: SocSource
    vehicle_id: str | None
    register_entity_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "soc_percent": self.soc_percent,
            "register_kwh": self.register_kwh,
            "read_at": self.read_at.isoformat(),
            "source": self.source,
            "vehicle_id": self.vehicle_id,
            "register_entity_id": self.register_entity_id,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SocAnchor | None:
        """A stored anchor, or `None` for anything that is not exactly one (storage is untrusted)."""
        if not isinstance(raw, dict):
            return None
        soc = finite_number(raw.get("soc_percent"))
        register = raw.get("register_kwh")
        register = None if register is None else finite_number(register)
        source = raw.get("source")
        vehicle_id = raw.get("vehicle_id")
        read_at_raw = raw.get("read_at")
        if soc is None or not 0.0 <= soc <= 100.0 or source not in ("vehicle", "charger"):
            return None
        if vehicle_id is not None and not isinstance(vehicle_id, str):
            return None
        if raw.get("register_kwh") is not None and register is None:
            return None
        register_entity_id = raw.get("register_entity_id")
        if register_entity_id is not None and not isinstance(register_entity_id, str):
            return None
        if register_entity_id is None:
            # Stored before the register was named (or with none): which meter the baseline came from is
            # unknown, so it is taken again from the register read next.
            register = None
        read_at = dt_util.parse_datetime(read_at_raw) if isinstance(read_at_raw, str) else None
        if read_at is None or read_at.tzinfo is None:
            return None
        return cls(soc, register, read_at, source, vehicle_id, register_entity_id)


@dataclass(frozen=True, slots=True)
class SocResolution:
    """`resolve_soc`'s answer: what to use now, and the anchor to keep."""

    reading: SocReading | None
    anchor: SocAnchor | None


def is_fresh(reading: SocReading | None) -> bool:
    """Whether this reading is recent enough to be the state of charge as it is."""
    return (
        reading is not None
        and reading.soc_percent is not None
        and reading.age_s is not None
        and reading.age_s <= SOC_FRESH_MAX_AGE_S
    )


def estimate_soc_percent(
    anchor: SocAnchor,
    *,
    register_kwh: float | None,
    capacity_kwh: float | None,
    ceiling_percent: float | None = None,
) -> float | None:
    """The state of charge carried forward from `anchor` by the energy delivered since, or `None`.

    `None` without a register (now or at the anchor), without a positive capacity, or when the
    register went backwards. Clamped to the car's own charge limit (`ceiling_percent`, else 100): a car
    takes nothing past it, so energy counted beyond it is charging loss, not charge. Never below the
    anchor's own reading.
    """
    if register_kwh is None or anchor.register_kwh is None:
        return None
    if capacity_kwh is None or not capacity_kwh > 0:
        return None
    delivered = register_kwh - anchor.register_kwh
    if delivered < -REGISTER_TOLERANCE_KWH:
        return None
    delivered = max(0.0, delivered)
    ceiling = charge_ceiling_percent(ceiling_percent)
    estimate = min(ceiling, anchor.soc_percent + delivered * CHARGE_EFFICIENCY / capacity_kwh * 100.0)
    return max(anchor.soc_percent, estimate)


def resolve_soc(
    *,
    reading: SocReading | None,
    anchor: SocAnchor | None,
    register_kwh: float | None,
    capacity_kwh: float | None,
    now: datetime,
    vehicle_id: str | None,
    register_entity_id: str | None = None,
    plugged_in_throughout: bool = False,
    ceiling_percent: float | None = None,
) -> SocResolution:
    """The state of charge to plan and stop against, and the anchor to keep. Pure.

    * a fresh usable reading is used as is and becomes the anchor;
    * otherwise the anchor carries the reading forward by delivered energy and the result is
      flagged `estimated` (also when the entity is `unavailable`, as a sleeping car is);
    * a stale reading newer than the anchor replaces it only while nothing has been delivered
      since the anchor (a car driven or charged elsewhere while idle);
    * with no anchor a stale reading starts one;
    * an anchor's baseline from another register than `register_entity_id` is dropped, and an anchor
      without a baseline (the register could not be read then, or it was dropped) takes the register's
      first readable value: energy before it is not credited, so either can only under-count;
    * a reading that is the car's entity coming back (`SocReading.restored`: a reload or late load of its
      integration) with the anchor's value, while the car is shown to have stayed plugged in through the
      gap (`plugged_in_throughout`), is the anchor's own reading, not a new one: the anchor and its baseline
      stay, whatever the register reads, and the reading's age is the anchor's. Without that continuity (a
      restart of Home Assistant, an unplug, a connection not known) it is a new reading as any other: when
      in doubt the delivered energy is forgotten (a second charge), never invented (a car left short);
    * the estimate is bounded by the car's own charge limit (`ceiling_percent`, `estimate_soc_percent`);
    * where no estimate can be made the raw reading is returned untouched, or `None`.
    """
    if anchor is not None and anchor.vehicle_id != vehicle_id:
        anchor = None
    if (
        anchor is not None
        and anchor.register_kwh is not None
        and anchor.register_entity_id != register_entity_id
    ):
        # A baseline read from another meter (a register found again, or chosen): never subtracted.
        anchor = replace(anchor, register_kwh=None)
    if anchor is not None and anchor.register_kwh is None and register_kwh is not None:
        # Anchored while the register could not be read (a restart before the charger's integration had
        # its register): its first readable value is the baseline.
        anchor = replace(anchor, register_kwh=register_kwh, register_entity_id=register_entity_id)
    usable = reading is not None and reading.soc_percent is not None
    restored = (
        usable
        and reading is not None
        and reading.restored
        and plugged_in_throughout
        and anchor is not None
        and reading.soc_percent == anchor.soc_percent
    )
    if usable and not restored:
        assert reading is not None and reading.soc_percent is not None
        age = reading.age_s
        read_at = now - timedelta(seconds=max(0.0, age)) if age is not None else now
        newer = anchor is None or (read_at - anchor.read_at).total_seconds() > SAME_READING_S
        if is_fresh(reading):
            if newer:
                anchor = SocAnchor(
                    reading.soc_percent, register_kwh, read_at, reading.source, vehicle_id, register_entity_id
                )
            return SocResolution(reading, anchor)
        if newer:
            idle = (
                anchor is None
                or register_kwh is None
                or anchor.register_kwh is None
                or register_kwh - anchor.register_kwh <= REGISTER_TOLERANCE_KWH
            )
            if idle:
                anchor = SocAnchor(
                    reading.soc_percent, register_kwh, read_at, reading.source, vehicle_id, register_entity_id
                )
    if anchor is None:
        return SocResolution(reading, None)
    if restored:
        assert reading is not None
        # The anchor's own reading, set again by the start: as old as the anchor.
        reading = replace(reading, age_s=max(0.0, (now - anchor.read_at).total_seconds()))
    estimate = estimate_soc_percent(
        anchor, register_kwh=register_kwh, capacity_kwh=capacity_kwh, ceiling_percent=ceiling_percent
    )
    delivered_something = (
        estimate is not None
        and anchor.register_kwh is not None
        and register_kwh is not None
        and register_kwh - anchor.register_kwh > REGISTER_TOLERANCE_KWH
    )
    if estimate is None or (usable and not delivered_something):
        # Nothing to add to the reading (or nothing to add it with): it stands as reported.
        return SocResolution(reading, anchor)
    return SocResolution(
        SocReading(
            soc_percent=estimate,
            source=anchor.source,
            entity_id=reading.entity_id if reading is not None else None,
            vehicle_id=vehicle_id,
            age_s=max(0.0, (now - anchor.read_at).total_seconds()),
            estimated=True,
        ),
        anchor,
    )


def target_need_kwh(
    *,
    soc_percent: float,
    capacity_kwh: float | None,
    target_percent: float | None,
    vehicle_max_percent: float | None = None,
) -> tuple[str, float | None]:
    """`(reason, wall kWh)` a target needs from `soc_percent`, charging loss included.

    `reason` is `planner.resolve_target_energy`'s; the energy is
    `(target - soc) x capacity / CHARGE_EFFICIENCY`, `None` without a capacity.
    """
    resolution = resolve_target_energy(
        TargetEnergyRequest(
            soc_percent=soc_percent,
            capacity_kwh=capacity_kwh,
            stored_target_percent=None if target_percent is None else round(target_percent),
            vehicle_max_percent=vehicle_max_percent,
            apply_default_target=False,
        )
    )
    if resolution.kwh is None:
        return resolution.reason, None
    return resolution.reason, resolution.kwh / CHARGE_EFFICIENCY


def battery_room_kwh(
    *, soc_percent: float | None, capacity_kwh: float | None, vehicle_max_percent: float | None = None
) -> float | None:
    """The wall energy the battery still has room for: `capacity x (ceiling - soc) / 100 / efficiency`,
    the ceiling being the car's own charge limit (else 100), zero at or above it; `None` without a level
    or a battery size."""
    if soc_percent is None:
        return None
    _, kwh = target_need_kwh(
        soc_percent=soc_percent,
        capacity_kwh=capacity_kwh,
        target_percent=100,
        vehicle_max_percent=vehicle_max_percent,
    )
    return kwh


def read_energy_register_kwh(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A cumulative energy register in kWh, or `None` when unreadable or not a meter.

    Requires a numeric state, `Wh`/`kWh` and `state_class: total_increasing`, so a session or
    momentary value that falls back to zero is never misread as a reset.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    attributes = state.attributes or {}
    if attributes.get("device_class") != "energy":
        return None
    if attributes.get("state_class") != "total_increasing":
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    unit = attributes.get("unit_of_measurement")
    if unit == "Wh":
        return value / 1000.0
    if unit == "kWh":
        return value
    return None


class SocReader:
    """One charger's state-of-charge source: raw sources in, the effective reading out.

    `raw_reader(vehicle_id)` is the preferred-source read (charger while live, else vehicle);
    this class adds the anchor, estimate, persistence and watch. Public methods are safe to call
    from the event loop and none awaits.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        raw_reader: Callable[[str | None], SocReading | None],
        register_entity_id: Callable[[], str | None],
        charge_control: Callable[[], str | None],
        remembered_capacity: Callable[[str | None], float | None],
        now: Callable[[], datetime] | None = None,
        plugged_in_since: Callable[[], datetime | None] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._now: Callable[[], datetime] = now if now is not None else dt_util.utcnow
        self._raw_reader = raw_reader
        self._register_entity_id = register_entity_id
        self._charge_control = charge_control
        self._remembered_capacity = remembered_capacity
        # Since when the charger has itself seen the car plugged in without a gap (`None`: not shown).
        self._plugged_in_since = plugged_in_since
        # Per source entity: the instant of the state with which it came back (from `unavailable`,
        # `unknown` or absent) and since when it had been gone, until it updates a value it had.
        self._comebacks: dict[str, tuple[datetime, datetime | None]] = {}
        self._gone_at: dict[str, datetime] = {}
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}")
        self._anchor: SocAnchor | None = None
        self._facts_cache: dict[str, tuple[datetime, float | None, float | None]] = {}
        self._watch_cancel: Callable[[], None] | None = None
        self._watched: tuple[str, ...] = ()
        self._watch_vehicle: str | None = None
        self._on_reading: Callable[[], None] | None = None
        self._last_notified: float | None = None
        self._notify_next = False
        # While a calculation lacks a level: the stored vehicle it was for, and the listener for a battery
        # sensor's state anywhere (one with no state yet resolves to nothing `ensure_watch` could watch).
        self._awaited_vehicle: str | None = None
        self._arrival_cancel: Callable[[], None] | None = None
        self._closed = False

    async def async_load(self) -> None:
        """Restore the anchor, so a restart in the middle of a charge keeps the estimate."""
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self._anchor = SocAnchor.from_dict(saved.get("anchor"))

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The entry is gone for good: forget its anchor."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    @callback
    def async_shutdown(self) -> None:
        self._closed = True
        self._drop_watch()
        self._drop_arrival()

    def await_reading(self, stored_vehicle_id: str | None) -> None:
        """A calculation found no usable level: tell `set_on_reading`'s callback of the first one that
        arrives, whatever it is (the same value as the last one heard included). At start-up the car's
        sensor may get its first state only after the planner ran; until then it resolves to nothing to
        watch, so any battery sensor's state is listened for and the vehicle resolved again on each."""
        if self._closed:
            return
        self._notify_next = True
        self._awaited_vehicle = stored_vehicle_id
        if self._arrival_cancel is None:
            self._arrival_cancel = self._hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._on_any_battery_state, event_filter=_battery_state
            )

    def _drop_arrival(self) -> None:
        if self._arrival_cancel is not None:
            self._arrival_cancel()
        self._arrival_cancel = None

    @callback
    def _on_any_battery_state(self, _event: Event[EventStateChangedData]) -> None:
        vehicle_id, _ = resolve_target_vehicle(
            self._hass, self._awaited_vehicle, charger_vehicle_ids(self._hass, self._entry_id)
        )
        self.ensure_watch(vehicle_id)
        reading = self.read(vehicle_id)
        if reading is not None and reading.soc_percent is not None:
            self._tell(reading)

    def set_on_reading(self, callback_: Callable[[], None] | None) -> None:
        """Ask to be told when a reading moved enough to make a new plan worth calculating."""
        self._on_reading = callback_

    def notify_next_reading(self) -> None:
        """Tell `set_on_reading`'s callback of the next reading that arrives, however little it moved: the
        planner waits for the car's new level after a charge (`vehicle_update_wait`), and even the same
        value reported again is the answer it waits for."""
        self._notify_next = True

    def _vehicle_facts(self, vehicle_id: str | None) -> tuple[float | None, float | None]:
        """`(reported capacity kWh, vehicle ceiling %)` for `vehicle_id`, reused for a few minutes."""
        if not vehicle_id:
            return (None, None)
        now = self._now()
        cached = self._facts_cache.get(vehicle_id)
        if cached is not None and (now - cached[0]).total_seconds() < FACTS_CACHE_S:
            return (cached[1], cached[2])
        capacity: float | None = None
        ceiling: float | None = None
        for candidate in discover_vehicles(self._hass):
            if candidate.id == vehicle_id:
                capacity = candidate.battery_capacity_kwh
                ceiling = candidate.target_soc_percent_max
                break
        if capacity is None and cached is not None:
            # A sleeping car drops out of discovery; its pack size has not changed.
            capacity = cached[1]
        if ceiling is None and cached is not None:
            ceiling = cached[2]
        self._facts_cache[vehicle_id] = (now, capacity, ceiling)
        return (capacity, ceiling)

    def capacity_with_source(self, vehicle_id: str | None) -> tuple[float | None, str | None]:
        """`(kWh, "reported" | "stored")`: the vehicle's own figure, else the stored one; `(None, None)` if missing."""
        reported, _ = self._vehicle_facts(vehicle_id)
        if reported is not None and reported > 0:
            return reported, "reported"
        remembered = self._remembered_capacity(vehicle_id)
        if remembered is not None and remembered > 0:
            return remembered, "stored"
        return None, None

    def capacity_kwh(self, vehicle_id: str | None) -> float | None:
        """The battery size: reported by the vehicle, else the one stored, else missing."""
        return self.capacity_with_source(vehicle_id)[0]

    def reported_capacity_kwh(self, vehicle_id: str | None) -> float | None:
        reported = self._vehicle_facts(vehicle_id)[0]
        return reported if reported is not None and reported > 0 else None

    def vehicle_max_percent(self, vehicle_id: str | None) -> float | None:
        return self._vehicle_facts(vehicle_id)[1]

    def vehicle_name(self, vehicle_id: str | None) -> str | None:
        if not vehicle_id:
            return None
        device = dr.async_get(self._hass).async_get(vehicle_id)
        return None if device is None else _device_name(device)

    def has_source(self, vehicle_id: str | None) -> bool:
        """Whether any state-of-charge entity resolves (value not required)."""
        return charger_soc_entity_id(self._hass, self._charge_control()) is not None or (
            vehicle_soc_entity_id(self._hass, vehicle_id) is not None
        )

    def read(self, vehicle_id: str | None) -> SocReading | None:
        """The effective state of charge now: a fresh reading, else the estimate, else the raw one.

        For the car the charger plans for: its reading moves the one anchor (a fresh reading, or another car
        chosen). A read for no car at all is only a look (`peek`) while a car holds the anchor: it never drops
        that car's anchor.
        """
        if not vehicle_id and self._anchor is not None and self._anchor.vehicle_id:
            return self.peek(vehicle_id)
        resolution = self._resolve(vehicle_id, self._anchor)
        if resolution.anchor != self._anchor:
            self._anchor = resolution.anchor
            self._save()
        return resolution.reading

    def peek(self, vehicle_id: str | None) -> SocReading | None:
        """`read` without touching the anchor: for a car only shown (another car at the charger in the card's
        list, a site's overview), never the one planned for. The anchor's car gets its estimate; any other car
        its reading as it is.

        One anchor is kept per charger. Before, a dashboard that listed every car at the charger read each of
        them through `read`: the second car took the anchor, and the planned car's next read anchored its stale
        reading again at the register's present value, so nothing it had been given counted any more.
        """
        anchor = self._anchor if self._anchor is not None and self._anchor.vehicle_id == vehicle_id else None
        return self._resolve(vehicle_id, anchor).reading

    def _resolve(self, vehicle_id: str | None, anchor: SocAnchor | None) -> SocResolution:
        register_entity_id = self._register_entity_id()
        reading, throughout = self._with_comeback(self._raw_reader(vehicle_id))
        return resolve_soc(
            reading=reading,
            anchor=anchor,
            register_kwh=read_energy_register_kwh(self._hass, register_entity_id),
            capacity_kwh=self.capacity_kwh(vehicle_id),
            now=self._now(),
            vehicle_id=vehicle_id,
            register_entity_id=register_entity_id,
            plugged_in_throughout=throughout,
            ceiling_percent=self.vehicle_max_percent(vehicle_id),
        )

    def _with_comeback(self, reading: SocReading | None) -> tuple[SocReading | None, bool]:
        """`reading` flagged `restored` when its state is its entity coming back, and whether the charger saw
        the car plugged in since before the entity went away (`resolve_soc`'s `plugged_in_throughout`).

        The first state seen after SpotNav set up (a restart, a reload) has no gap the charger watched across,
        so it is never shown continuous: a new reading, as an unplug or an unknown connection makes it."""
        if reading is None or reading.entity_id is None:
            return reading, False
        comeback = self._comebacks.get(reading.entity_id)
        state = self._hass.states.get(reading.entity_id)
        if comeback is None or state is None or state.last_updated != comeback[0]:
            return reading, False
        gone_since = comeback[1]
        since = None if self._plugged_in_since is None else self._plugged_in_since()
        throughout = since is not None and gone_since is not None and since <= gone_since
        return replace(reading, restored=True), throughout

    @callback
    def _note_source_state(self, event: Event[EventStateChangedData]) -> None:
        """Remember when a source entity went away and the state it came back with."""
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        gone = (STATE_UNAVAILABLE, STATE_UNKNOWN)
        if new is None or new.state in gone:
            if old is not None and old.state not in gone:
                self._gone_at[entity_id] = new.last_updated if new is not None else self._now()
            self._comebacks.pop(entity_id, None)
            return
        if old is None or old.state in gone:
            gone_since = self._gone_at.pop(entity_id, None)
            if old is not None and gone_since is None:
                gone_since = old.last_updated
            self._comebacks[entity_id] = (new.last_updated, gone_since)
        else:
            # An update of a value it had (the same value with a new poll included): a reading.
            self._comebacks.pop(entity_id, None)

    def _save(self) -> None:
        if self._closed:
            return
        self._store.async_delay_save(
            lambda: {"anchor": None if self._anchor is None else self._anchor.as_dict()}, 1.0
        )

    def ensure_watch(self, vehicle_id: str | None) -> None:
        """Watch the source entities so a fresh reading is anchored the moment it arrives."""
        if self._closed:
            return
        entity_ids = tuple(
            entity_id
            for entity_id in (
                charger_soc_entity_id(self._hass, self._charge_control()),
                vehicle_soc_entity_id(self._hass, vehicle_id),
            )
            if entity_id
        )
        self._watch_vehicle = vehicle_id
        if entity_ids == self._watched:
            return
        self._drop_watch()
        self._watched = entity_ids
        if entity_ids:
            self._watch_cancel = async_track_state_change_event(
                self._hass, list(entity_ids), self._on_source_changed
            )

    def _drop_watch(self) -> None:
        if self._watch_cancel is not None:
            self._watch_cancel()
        self._watch_cancel = None
        self._watched = ()

    @callback
    def _on_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self._note_source_state(event)
        reading = self.read(self._watch_vehicle)
        if reading is None or reading.soc_percent is None or reading.estimated:
            return
        self._tell(reading)

    def _tell(self, reading: SocReading) -> None:
        """Ask for a new calculation on `reading`: always while one lacked a level, else when it moved."""
        assert reading.soc_percent is not None
        last = self._last_notified
        if (
            not self._notify_next
            and last is not None
            and abs(reading.soc_percent - last) < RECALCULATE_DELTA_PERCENT
        ):
            return
        self._notify_next = False
        self._drop_arrival()
        self._last_notified = reading.soc_percent
        if self._on_reading is not None:
            self._on_reading()


@callback
def _battery_state(event_data: EventStateChangedData) -> bool:
    """A sensor state that could be a state of charge (`await_reading`'s filter, run for every state)."""
    new = event_data["new_state"]
    return (
        new is not None
        and new.domain == "sensor"
        and new.attributes.get("device_class") == "battery"
    )
