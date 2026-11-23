"""The site's one priority list ("Prioritet", owner decision 2026-11-23, `plans/v3_pool_spa_spec.md`). Pure: no
Home Assistant.

One ordered list of load ids per site, stored in the site entry's data (`const.CONF_PRIORITY`): the top gets energy
first and gives way last. Each charger is its own row (`charger:<entry id>`); the other rows are fixed ids, kept in
the list whether or not the site has that load (a load that appears later keeps its place). It is read in both
directions:

* **Solar surplus** (top down): the battery's place against the cars is the old `solar_priority`: above every car is
  `battery_first`, else `car_first` (`solar_priority`). With no charger on the site, the battery above the water
  heater is `battery_first`.
* **Load balancing between chargers**: a charger higher in the list is served first and cut last (`charger_rank`).
* **The shed order under the ceiling** (bottom up, `site/shed_order.py`): `shed_sequence`.

Hard rules and deadlines always come before the list (a car's minimum level, legionella, the spa's floor, the fuse).

Default (top to bottom): house heat, the cars (in their old `charger_priority` order: first, normal, last, each class
in the site's own charger order), the spa with a booked bath, the sauna, house cooling, the water heater, the home
battery, the spa idle, the pool's circulation beyond the day's need, the pool's heat pump. With `battery_first` the
battery stands directly above the cars.

**Migration**: while no list is stored, the old keys (`solar_priority` on the site, `charger_priority` on each
charger) are read exactly as before (`legacy_order`), so nothing changes on upgrade. The first write of the list
stores it and drops the old keys; from then on they are only derived from the list for older apps
(`derived_charger_priorities`, `solar_priority`). A stored list is completed on each read (`complete`): an id it does
not know is dropped, a charger no longer on the site is dropped, a new one (a charger joining, a load added in a
later version) is inserted at its default place.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from ..const import (
    CHARGER_PRIORITY_FIRST,
    CHARGER_PRIORITY_LAST,
    CHARGER_PRIORITY_NORMAL,
    CONF_CHARGER_ENTRY_IDS,
    CONF_PRIORITY,
    CONF_SOLAR_PRIORITY,
    DEFAULT_SOLAR_PRIORITY,
    SOLAR_PRIORITY_BATTERY_FIRST,
    SOLAR_PRIORITY_CAR_FIRST,
)
from ..pool.rules import LOAD_COOLING, LOAD_POOL_CIRCULATION, LOAD_POOL_HEAT, LOAD_SAUNA, LOAD_SPA

ID_HEAT: Final = "heat"
ID_BATTERY: Final = "battery"
ID_WATER_HEATER: Final = "water_heater"
ID_COOLING: Final = LOAD_COOLING
#: The spa heating towards a booked bath, and the spa holding its idle temperature between baths: two rows, as the
#: owner's order places them far apart (the bath gives way just before the car, the idle hold among the first).
ID_SPA: Final = LOAD_SPA
ID_SPA_IDLE: Final = "spa_idle"
ID_SAUNA: Final = LOAD_SAUNA
ID_POOL_CIRCULATION: Final = LOAD_POOL_CIRCULATION
ID_POOL_HEAT: Final = LOAD_POOL_HEAT
CHARGER_PREFIX: Final = "charger:"

#: The rows above the cars, and below them, in their default order.
ABOVE_CARS: Final = (ID_HEAT,)
BELOW_CARS: Final = (
    ID_SPA,
    ID_SAUNA,
    ID_COOLING,
    ID_WATER_HEATER,
    ID_BATTERY,
    ID_SPA_IDLE,
    ID_POOL_CIRCULATION,
    ID_POOL_HEAT,
)
FIXED_IDS: Final = frozenset((*ABOVE_CARS, *BELOW_CARS))

_LEGACY_RANK: Final = {CHARGER_PRIORITY_FIRST: 0, CHARGER_PRIORITY_NORMAL: 1, CHARGER_PRIORITY_LAST: 2}


def charger_id(entry_id: str) -> str:
    """A charger's row id."""
    return f"{CHARGER_PREFIX}{entry_id}"


def charger_entry_of(load_id: str) -> str | None:
    """The charger entry id a row names, `None` for any other row."""
    return load_id[len(CHARGER_PREFIX) :] if load_id.startswith(CHARGER_PREFIX) else None


def members(config: Mapping[str, Any]) -> list[str]:
    """The site's member chargers in its own order."""
    return [str(entry_id) for entry_id in config.get(CONF_CHARGER_ENTRY_IDS) or [] if isinstance(entry_id, str)]


def default_order(chargers: Sequence[str], *, battery_first: bool = False) -> list[str]:
    """The default list for these chargers (entry ids, already in their order)."""
    below = list(BELOW_CARS)
    cars = [charger_id(entry_id) for entry_id in chargers]
    if battery_first:
        below.remove(ID_BATTERY)
        cars = [ID_BATTERY, *cars]
    return [*ABOVE_CARS, *cars, *below]


def legacy_order(chargers: Sequence[str], ranks: Mapping[str, str], solar_priority: str) -> list[str]:
    """The list the old settings describe: the cars by their `charger_priority` (first, normal, last; an unknown one
    is normal), each class in the site's own order; the battery above the cars under `battery_first`."""
    ordered = sorted(
        enumerate(chargers), key=lambda item: (_LEGACY_RANK.get(ranks.get(item[1], ""), 1), item[0])
    )
    return default_order(
        [entry_id for _index, entry_id in ordered], battery_first=solar_priority == SOLAR_PRIORITY_BATTERY_FIRST
    )


def complete(order: Iterable[Any], chargers: Sequence[str]) -> list[str]:
    """`order` with what it does not know dropped (unknown ids, repeats, chargers not on the site) and every known
    row it lacks inserted at its default place: directly below the nearest row above it in the default order that
    the list has (a new charger: below the last car), else at the top."""
    known = FIXED_IDS | {charger_id(entry_id) for entry_id in chargers}
    out: list[str] = []
    for item in order:
        if isinstance(item, str) and item in known and item not in out:
            out.append(item)
    reference = default_order(chargers)
    for index, item in enumerate(reference):
        if item in out:
            continue
        above = next((row for row in reversed(reference[:index]) if row in out), None)
        out.insert(0 if above is None else out.index(above) + 1, item)
    return out


def stored(config: Mapping[str, Any]) -> list[str] | None:
    """The stored list, `None` while none is (the old keys still count)."""
    raw = config.get(CONF_PRIORITY)
    if not isinstance(raw, (list, tuple)):
        return None
    return [item for item in raw if isinstance(item, str)]


def effective_order(config: Mapping[str, Any], charger_ranks: Mapping[str, str] | None = None) -> list[str]:
    """The site's list now: the stored one completed, else the one the old settings describe (`charger_ranks`: each
    member charger's stored `charger_priority`)."""
    chargers = members(config)
    found = stored(config)
    if found is not None:
        return complete(found, chargers)
    return legacy_order(chargers, charger_ranks or {}, str(config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY)))


def battery_first_in(order: Sequence[str]) -> bool:
    """Whether the battery stands above every car (with no car: above the water heater)."""
    if ID_BATTERY not in order:
        return False
    battery = order.index(ID_BATTERY)
    cars = [index for index, item in enumerate(order) if item.startswith(CHARGER_PREFIX)]
    if cars:
        return battery < min(cars)
    return ID_WATER_HEATER in order and battery < order.index(ID_WATER_HEATER)


def solar_priority(config: Mapping[str, Any]) -> str:
    """`car_first` or `battery_first`, what the solar surplus and the battery read: from the stored list, else the
    old key as before."""
    found = stored(config)
    if found is None:
        return str(config.get(CONF_SOLAR_PRIORITY, DEFAULT_SOLAR_PRIORITY))
    return SOLAR_PRIORITY_BATTERY_FIRST if battery_first_in(complete(found, members(config))) else SOLAR_PRIORITY_CAR_FIRST


def charger_rank(config: Mapping[str, Any], entry_id: str) -> int | None:
    """A charger's place in the stored list (0 the top), `None` while no list is stored (its own `charger_priority`
    then counts, as before)."""
    found = stored(config)
    if found is None:
        return None
    order = complete(found, members(config))
    row = charger_id(entry_id)
    return order.index(row) if row in order else len(order)


def derived_charger_priorities(order: Sequence[str]) -> dict[str, str]:
    """The old `charger_priority` an older app is shown, from the list: the top car "first", the bottom one "last",
    the rest (and a car alone) "normal"."""
    cars = [entry_id for item in order if (entry_id := charger_entry_of(item)) is not None]
    out = {entry_id: CHARGER_PRIORITY_NORMAL for entry_id in cars}
    if len(cars) >= 2:
        out[cars[0]] = CHARGER_PRIORITY_FIRST
        out[cars[-1]] = CHARGER_PRIORITY_LAST
    return out


def _car_slots(order: Sequence[str]) -> list[int]:
    return [index for index, item in enumerate(order) if item.startswith(CHARGER_PREFIX)]


def with_charger_priority(order: Sequence[str], entry_id: str, value: str) -> list[str]:
    """An older app's `charger_priority` write as a list change: the cars sorted by their derived class with this one
    set to `value` (first, normal, last), each class keeping its order, back into the cars' own places."""
    slots = _car_slots(order)
    cars = [order[index] for index in slots]
    ranks = {charger_entry_of(item): _LEGACY_RANK[level] for item, level in _derived_rows(order).items()}
    ranks[entry_id] = _LEGACY_RANK.get(value, 1)
    cars.sort(key=lambda item: (ranks.get(charger_entry_of(item), 1), cars.index(item)))
    out = list(order)
    for index, item in zip(slots, cars, strict=True):
        out[index] = item
    return out


def _derived_rows(order: Sequence[str]) -> dict[str, str]:
    return {charger_id(entry_id): level for entry_id, level in derived_charger_priorities(order).items()}


def with_solar_priority(order: Sequence[str], value: str) -> list[str]:
    """An older app's `solar_priority` write as a list change: `battery_first` moves the battery directly above the
    top car (with no car: directly above the water heater); `car_first` moves it below the bottom car, to its default
    place where that is lower. Nothing moves where the list already says so."""
    if value not in (SOLAR_PRIORITY_CAR_FIRST, SOLAR_PRIORITY_BATTERY_FIRST) or ID_BATTERY not in order:
        return list(order)
    if battery_first_in(order) == (value == SOLAR_PRIORITY_BATTERY_FIRST):
        return list(order)
    out = [item for item in order if item != ID_BATTERY]
    slots = _car_slots(out)
    if value == SOLAR_PRIORITY_BATTERY_FIRST:
        anchor = slots[0] if slots else (out.index(ID_WATER_HEATER) if ID_WATER_HEATER in out else 0)
        out.insert(anchor, ID_BATTERY)
        return out
    # Its default place (directly below the water heater) where that is below every car, else directly below the cars.
    lowest = slots[-1] if slots else -1
    heater = out.index(ID_WATER_HEATER) if ID_WATER_HEATER in out else -1
    out.insert(max(lowest, heater) + 1, ID_BATTERY)
    return out


def merged(full: Sequence[str], shown: Sequence[str]) -> list[str]:
    """The whole list after a write of the rows a person was shown (`shown`, a reordering of those rows of `full`): the
    rows not shown (loads the site does not have) keep their places, the shown ones fill their own places in the new
    order."""
    wanted = set(shown)
    queue = list(shown)
    return [queue.pop(0) if item in wanted else item for item in full]


def shed_sequence(order: Sequence[str]) -> list[str]:
    """The list bottom up (the order loads give way in), every car as one `car` row at the place of the top car (a
    load between two cars gives way before the cars are touched); without any car, one at the cars' default place."""
    from .shed_order import LOAD_BATTERY, LOAD_CAR  # noqa: PLC0415 - the shed order reads this module

    rows = list(order)
    if not _car_slots(rows):
        rows = complete(rows, ["\0"])
    top_car = _car_slots(rows)[0]
    out: list[str] = []
    for index in range(len(rows) - 1, -1, -1):
        item = rows[index]
        if index == top_car:
            out.append(LOAD_CAR)
        elif not item.startswith(CHARGER_PREFIX):
            out.append(LOAD_BATTERY if item == ID_BATTERY else item)
    return out
