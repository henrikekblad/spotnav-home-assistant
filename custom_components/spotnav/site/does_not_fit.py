"""A car's charge that does not fit under the ceiling by its departure, and the person's choices (plan
`plans/v3_flexible_loads_design.md`, step 3a): one block per car, in the charger's dashboard (`does_not_fit`) and in
the Plan tab's car lane (`spotnav/get_plan`, the load's `does_not_fit`), and where the plan asks, on the phones
(`ChargerNotifier.does_not_fit`, step 3b: once per departure, in the daytime). Words are the card's and the
notice's; these are figures.

    {departure, short_kwh, deliverable_kwh, ceiling, ceiling_kw, set_kw, chosen,
     choices: [{code, kwh, peak_kw, cost, departure}]}

* **Only a ceiling that acts now.** The import target's hold, or the price's power level where the hold keeps it
  without a target (`ceiling`: `import_target` / `power_level`): what `SiteCapacityController.import_target_status`
  says this hour; `ceiling_kw` the level that holds this hour, `set_kw` the target (or power level) as set, below
  `ceiling_kw` where the month's peak lifts the running hour. The demand charge's soft ceiling (`site/demand_ceiling.py`) and the car's plan under it
  (`planning/car_demand.py`) are shadow only: they hold nothing back, so they show nothing here. The main fuse is not
  a "does not fit" either: the plan is made at what the fuse allows (load balancing then shares it out).
* **Short** (`TargetRisk`): `short_kwh` the energy the hold leaves missing by the `departure` (an instant),
  `deliverable_kwh` what it still delivers of what the plan needs (`None` where the need is not known).
* **`chosen`**: `takes_what_fits` where this departure is held as `if_not_ready` `hold` holds it: a person's "Take
  what fits" for this departure (`SiteCapacityController.async_take_what_fits`), or the lasting `hold`; `accept_peak`
  while the target is set aside for this charge (a person's "Release the import target for this charge", or the
  plan's own `release`): then nothing is short and `short_kwh` is `None`; else `None` (the plan asks).
* **`choices`** (the codes of `planning/flexible_load.py`), the ones not chosen. Each is for this departure only
  (the lasting `if_not_ready` is the person's default, never changed by a choice). Once the peak is accepted by a
  person there is none (a set-aside has no undo); where the plan's own `release` accepted it, `takes_what_fits` is
  still offered for this departure.
  `takes_what_fits` (`kwh` what it delivers, `spotnav/take_what_fits`); `later` (the departure moved: `departure`
  the earliest whole hour after it by which it all fits under the same ceiling within the prices held,
  `TargetRisk.later_departure`, which `spotnav/later_departure` moves this departure alone to; `None` where none
  does: the person moves it in the departure's editor); `accept_peak` (`spotnav/release_import_target` for this charge alone: `peak_kw`
  the hour's import it may reach, `cost` what that peak adds to the month's demand charge in the area's minor unit
  where the price has one and it is known, else `None`).
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.util import dt as dt_util

from ..planning.auto_settings import IF_NOT_READY_HOLD
from ..planning.flexible_load import CHOICE_ACCEPT_PEAK, CHOICE_LATER, CHOICE_TAKES_WHAT_FITS

BLOCK_KEYS: Final = (
    "departure", "short_kwh", "deliverable_kwh", "ceiling", "ceiling_kw", "set_kw", "chosen", "choices"
)
CHOICE_KEYS: Final = ("code", "kwh", "peak_kw", "cost", "departure")

CEILING_IMPORT_TARGET: Final = "import_target"
CEILING_POWER_LEVEL: Final = "power_level"


def _instant(value: str | None) -> str | None:
    """An instant as ISO (UTC), or `None` (no departure, or a set-aside kept until the unplug)."""
    moment = None if value is None else dt_util.parse_datetime(value)
    if moment is None or moment.tzinfo is None:
        return None
    return dt_util.as_utc(moment).isoformat()


def _choice(
    code: str,
    *,
    kwh: float | None = None,
    peak_kw: float | None = None,
    cost: float | None = None,
    departure: str | None = None,
) -> dict[str, Any]:
    return {"code": code, "kwh": kwh, "peak_kw": peak_kw, "cost": cost, "departure": departure}


def does_not_fit(status: Any, *, ceiling: str, peak_cost: float | None = None) -> dict[str, Any] | None:
    """The block for one charger from what the import target does to it now (`ImportTargetStatus`, `None`: it does
    nothing this hour), or `None` when its charge fits (see the module). `ceiling` which hold it is; `peak_cost` what
    accepting the peak adds to the month's demand charge (minor unit, `None`: no demand charge, or not known)."""
    if status is None:
        return None
    risk = status.at_risk
    if risk is None and status.released_for is None:
        return None
    peak = None if status.release_peak_kw is None else float(status.release_peak_kw)
    accept = _choice(CHOICE_ACCEPT_PEAK, peak_kw=peak, cost=None if peak is None else peak_cost)
    if risk is None:
        # Set aside for this charge (by a person, or the plan's `release`): the peak is accepted until the departure.
        return {
            "departure": _instant(status.released_for),
            "short_kwh": None,
            "deliverable_kwh": None,
            "ceiling": ceiling,
            "ceiling_kw": float(status.target_kw),
            "set_kw": float(status.set_kw),
            "chosen": CHOICE_ACCEPT_PEAK,
            "choices": [_choice(CHOICE_TAKES_WHAT_FITS)] if status.released_automatically else [],
        }
    need = getattr(risk, "need_kwh", None)
    deliverable = None if need is None else round(max(0.0, need - risk.missing_kwh), 1)
    held = risk.choice == IF_NOT_READY_HOLD
    choices = [] if held else [_choice(CHOICE_TAKES_WHAT_FITS, kwh=deliverable)]
    choices += [_choice(CHOICE_LATER, departure=_instant(getattr(risk, "later_departure", None))), accept]
    return {
        "departure": _instant(risk.departure),
        "short_kwh": round(float(risk.missing_kwh), 1),
        "deliverable_kwh": deliverable,
        "ceiling": ceiling,
        "ceiling_kw": float(risk.target_kw),
        "set_kw": float(risk.target_kw if risk.set_kw is None else risk.set_kw),
        "chosen": CHOICE_TAKES_WHAT_FITS if held else None,
        "choices": choices,
    }


def peak_cost_minor(site: Any, peak_kw: float | None) -> float | None:
    """What raising the running hour's day peak to `peak_kw` adds to the month's demand charge (the site's soft
    ceiling's price above its threshold, `demand_cost.raise_cost`), in the area's minor unit; `None` without a demand
    charge or a known peak."""
    from ..planning.demand_charge import hour_start  # noqa: PLC0415 - the planner imports the site
    from ..planning.demand_cost import raise_cost  # noqa: PLC0415

    ceiling = getattr(site, "demand_ceiling", None)
    state = None if ceiling is None else ceiling.state
    if state is None or peak_kw is None:
        return None
    hour = state.hours.get(hour_start(site.demand.now(), site.demand.tracker.tz))
    if hour is None:
        return None
    return round(raise_cost(hour, peak_kw) * 100.0)


def site_does_not_fit(site: Any, charger_entry_id: str) -> dict[str, Any] | None:
    """The block for one of the site's chargers (see the module), from the running site controller."""
    from .import_target import stored_import_target_kw  # noqa: PLC0415

    status = site.import_target_status(charger_entry_id)
    if status is None:
        return None
    # The target as stored in the entry (written without a reload), as the hold reads it.
    read = getattr(site, "_import_target_kw", None)
    target = read() if callable(read) else stored_import_target_kw(site.config)
    ceiling = CEILING_IMPORT_TARGET if target is not None else CEILING_POWER_LEVEL
    try:
        cost = peak_cost_minor(site, status.release_peak_kw)
    except Exception:  # noqa: BLE001 - the block stands without its price
        cost = None
    return does_not_fit(status, ceiling=ceiling, peak_cost=cost)
