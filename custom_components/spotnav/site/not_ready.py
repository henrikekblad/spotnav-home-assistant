"""What will not be ready in time, reckoned across every deadline of the site (spec `plans/v3_pool_spa_spec.md` section 8): the
cars' departures under the ceiling (`site/does_not_fit.py`) and the spa's baths and the sauna's sessions that will not
be ready (`spa/plan.py`, `NotReady`), with each choice ranked by how much it frees. One block per site, `get_plan`'s
top-level `not_ready`, the card's choice sheet and the phones' combined notice (`notifications/site_notice.py`) read it.
Pure, but for `site_not_ready`, which reads the running site.

    {items: [{load_id, kind, name, deadline, short_kwh, short_c, reach_c, cause, chosen,
              choices: [{code, frees_kwh, kwh, reach_c, later, peak_kw, cost}]}],
     ranked: [{load_id, code, frees_kwh}]} | null

* **Items**, one per deadline that will not be ready, in deadline order. `load_id` is the load's id in `get_plan`
  (a car's is its charger entry id, the spa's `spa`, the sauna's `sauna`), `kind` `car`, `spa` or `sauna`, `name` the
  charger's or the device's name. A car's is its "does not fit" block (`short_kwh` the energy missing by the
  `deadline`, `cause` the ceiling that holds it: `import_target` or `power_level`; `short_c`/`reach_c` null); a car's
  charge whose peak is accepted is not one (nothing is short). A bath's or a session's: `short_c` the degrees missing
  at the `deadline`, `reach_c` the temperature it reaches, `short_kwh` what that is in energy at the heater's power
  and the learnt rate, `cause` `rate` (the time left is too short), `import_target`, `power_level` or `fuse`.
  `chosen` is the car's choice for its departure (`takes_what_fits`, `None`: it asks); a bath or a session that was
  answered is planned so and is no item.
* **Choices** (the codes of `planning/flexible_load.py`), for that deadline alone, each with what it frees
  (`frees_kwh`): `later` (`later` the earliest whole local hour where it all fits: the car's later departure, the
  later bath or session; left out where none fits) and `takes_what_fits` (the car's `kwh` it still delivers, the
  bath's `reach_c`) free the item's own shortfall; `accept_peak` (only under the import target or the power level,
  never for the fuse or the rate) frees every item under that ceiling at once: one press sets the ceiling aside for
  all of them (`peak_kw`, `cost` the car's figures where one car alone is under it).
* **Joint view** (first version, as the spec infers): the shortfalls are each item's own against the room the hold
  leaves; no joint optimiser.
* **`ranked`**: every choice of the items that ask (`chosen` null), the most freed first; at a tie the one that costs
  nothing first (`later`, then `takes_what_fits`, then `accept_peak`), then the load lower in the site's priority list
  (`site/priority.py`: by default the booked bath or session gives way before the car). `accept_peak` is ranked once
  for the site (`load_id` null). Unanswered, the shed order trims by the same list and each car's lasting
  `if_not_ready` stands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from ..planning.flexible_load import CHOICE_ACCEPT_PEAK, CHOICE_LATER, CHOICE_TAKES_WHAT_FITS

ITEM_KEYS: Final = (
    "load_id", "kind", "name", "deadline", "short_kwh", "short_c", "reach_c", "cause", "chosen", "choices"
)
CHOICE_KEYS: Final = ("code", "frees_kwh", "kwh", "reach_c", "later", "peak_kw", "cost")
RANKED_KEYS: Final = ("load_id", "code", "frees_kwh")

KIND_CAR: Final = "car"
KIND_SPA: Final = "spa"
KIND_SAUNA: Final = "sauna"

#: The ceilings an accepted peak lifts (never the fuse, never the time left).
PEAK_CAUSES: Final = frozenset({"import_target", "power_level"})
#: At a tie, what costs nothing first.
_COST_RANK: Final = {CHOICE_LATER: 0, CHOICE_TAKES_WHAT_FITS: 1, CHOICE_ACCEPT_PEAK: 2}


def _utc(value: Any) -> str | None:
    """An instant as ISO in UTC (so items sort and compare as text), `None` for one that cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(UTC).isoformat()


def _choice(
    code: str,
    frees: float,
    *,
    kwh: float | None = None,
    reach_c: float | None = None,
    later: str | None = None,
    peak_kw: float | None = None,
    cost: float | None = None,
) -> dict[str, Any]:
    return {
        "code": code, "frees_kwh": round(frees, 1), "kwh": kwh, "reach_c": reach_c, "later": later,
        "peak_kw": peak_kw, "cost": cost,
    }


def car_item(load_id: str, name: str | None, block: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A car's item from its "does not fit" block (`site/does_not_fit.py`), `None` where nothing is short. Its
    `accept_peak` frees only its own shortfall here; `site_block` widens it to every item under the ceiling."""
    if block is None or block.get("short_kwh") is None:
        return None
    short = float(block["short_kwh"])
    choices: list[dict[str, Any]] = []
    for choice in block.get("choices") or ():
        code = choice.get("code")
        if code == CHOICE_TAKES_WHAT_FITS:
            choices.append(_choice(code, short, kwh=choice.get("kwh")))
        elif code == CHOICE_LATER and choice.get("departure") is not None:
            choices.append(_choice(code, short, later=_utc(choice.get("departure"))))
        elif code == CHOICE_ACCEPT_PEAK:
            choices.append(_choice(code, short, peak_kw=choice.get("peak_kw"), cost=choice.get("cost")))
    return {
        "load_id": load_id,
        "kind": KIND_CAR,
        "name": name,
        "deadline": _utc(block.get("departure")),
        "short_kwh": round(short, 1),
        "short_c": None,
        "reach_c": None,
        "cause": block.get("ceiling"),
        "chosen": block.get("chosen"),
        "choices": choices,
    }


def deadline_item(kind: str, name: str | None, not_ready: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A bath's or a session's item from its plan's `not_ready` (`spa/plan.NotReady.as_dict`), `None` without one."""
    if not_ready is None:
        return None
    short = float(not_ready.get("short_kwh") or 0.0)
    reach = not_ready.get("reach_c")
    choices = [_choice(CHOICE_TAKES_WHAT_FITS, short, reach_c=reach)]
    if not_ready.get("later") is not None:
        choices.insert(0, _choice(CHOICE_LATER, short, later=_utc(not_ready["later"])))
    if not_ready.get("cause") in PEAK_CAUSES:
        choices.append(_choice(CHOICE_ACCEPT_PEAK, short))
    return {
        "load_id": kind,
        "kind": kind,
        "name": name,
        "deadline": _utc(not_ready.get("at")),
        "short_kwh": round(short, 1),
        "short_c": not_ready.get("short_c"),
        "reach_c": reach,
        "cause": not_ready.get("cause"),
        "chosen": None,
        "choices": choices,
    }


def _priority_id(item: Mapping[str, Any]) -> str:
    return f"charger:{item['load_id']}" if item["kind"] == KIND_CAR else str(item["load_id"])


def site_block(items: Sequence[dict[str, Any] | None], priority: Sequence[str] = ()) -> dict[str, Any] | None:
    """The site's block from its items (see the module), `None` without any; `priority` the site's list, top first."""
    found = sorted((item for item in items if item is not None), key=lambda item: (item["deadline"] or "", item["load_id"]))
    if not found:
        return None
    asking = [item for item in found if item["chosen"] is None]
    # The peak lifts the ceiling for every item under it: what it frees is all of theirs.
    under = [item for item in asking if item["cause"] in PEAK_CAUSES]
    freed_by_peak = round(sum(item["short_kwh"] or 0.0 for item in under), 1)
    peaks = [choice for item in under for choice in item["choices"] if choice["code"] == CHOICE_ACCEPT_PEAK]
    for item in under:
        for choice in item["choices"]:
            if choice["code"] == CHOICE_ACCEPT_PEAK:
                choice["frees_kwh"] = freed_by_peak
    place = {load: index for index, load in enumerate(priority)}
    lowest = len(priority)
    ranked: list[tuple[tuple[float, int, int], dict[str, Any]]] = []
    for item in asking:
        rank = place.get(_priority_id(item), lowest)
        for choice in item["choices"]:
            if choice["code"] == CHOICE_ACCEPT_PEAK:
                continue
            entry = {"load_id": item["load_id"], "code": choice["code"], "frees_kwh": choice["frees_kwh"]}
            ranked.append(((-choice["frees_kwh"], _COST_RANK[choice["code"]], -rank), entry))
    if peaks:
        entry = {"load_id": None, "code": CHOICE_ACCEPT_PEAK, "frees_kwh": freed_by_peak}
        ranked.append(((-freed_by_peak, _COST_RANK[CHOICE_ACCEPT_PEAK], 0), entry))
    ranked.sort(key=lambda pair: pair[0])
    return {"items": found, "ranked": [entry for _key, entry in ranked]}


def deadline_items(site: Any) -> list[dict[str, Any]]:
    """The spa's and the sauna's items from the site's pool and spa shadow (`pool/runtime.py`)."""
    shadow = getattr(site, "pool_spa", None)
    if shadow is None:
        return []
    config = shadow.config()
    out: list[dict[str, Any]] = []
    for kind, part, block in ((KIND_SPA, shadow.spa, config.spa), (KIND_SAUNA, shadow.sauna, config.sauna)):
        plan = None if block is None else part.plan
        if plan is None or plan.not_ready is None:
            continue
        state = site.hass.states.get(block.entity_id)
        item = deadline_item(kind, None if state is None else state.name, plan.not_ready.as_dict())
        if item is not None:
            out.append(item)
    return out


def site_not_ready(site: Any, car_blocks: Mapping[str, dict[str, Any] | None] | None = None) -> dict[str, Any] | None:
    """The block for the running site (see the module). `car_blocks`: each member charger's "does not fit" block where
    the caller has it already; else read (`site_does_not_fit`)."""
    from ..const import CONF_CHARGER_ENTRY_IDS  # noqa: PLC0415
    from .does_not_fit import site_does_not_fit  # noqa: PLC0415
    from .priority import effective_order  # noqa: PLC0415

    items: list[dict[str, Any] | None] = []
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        if car_blocks is not None and charger_entry_id in car_blocks:
            block = car_blocks[charger_entry_id]
        else:
            try:
                block = site_does_not_fit(site, charger_entry_id)
            except Exception:  # noqa: BLE001 - the other items stand without this one
                block = None
        entry = site.hass.config_entries.async_get_entry(charger_entry_id)
        items.append(car_item(charger_entry_id, None if entry is None else entry.title, block))
    items.extend(deadline_items(site))
    try:
        priority = effective_order(site.config)
    except Exception:  # noqa: BLE001 - ranked without the list: the deadline's own order
        priority = []
    return site_block(items, priority)
