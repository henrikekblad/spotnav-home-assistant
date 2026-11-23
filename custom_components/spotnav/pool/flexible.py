"""The pool's and the spa's loads in the common shape (`planning/flexible_load.py`, spec section 4): the pool's
circulation (a run time per day, moved never dropped), the pool's heat pump (a temperature, inside the circulation's
hours), the spa (a bath temperature by the next bath, else held at its idle temperature) and the sauna (a temperature
by the next session it was booked for, else off). Descriptions only: the
numbers are the rules' own constants (`pool/rules.py`), the need and the next bath what the shadow holds. Nothing is
written.

Each load's `id` is its stable load id (`pool_heat`, `pool_circulation`, `spa`, `sauna`), the name the site's priority list
refers to; the entity it would steer is in its handle's values. A pool whose season is closed is not described (there is
nothing to move: SpotNav touches nothing then).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..planning.flexible_load import (
    CHOICE_ACCEPT_PEAK,
    CHOICE_LATER,
    CHOICE_TAKES_WHAT_FITS,
    GIVE_COMFORT_DEG_H,
    GIVE_HOURS_PAUSED,
    GIVE_MINUTES_LATE,
    GOAL_RUNTIME_PER_DAY,
    GOAL_TEMPERATURE_BY,
    HANDLE_ON_OFF,
    HANDLE_SETPOINT_OFFSET,
    KIND_POOL_CIRCULATION,
    KIND_POOL_HEAT,
    KIND_SAUNA,
    KIND_SPA,
    Choice,
    FlexibleLoad,
    Give,
    Goal,
    Handle,
    rule,
    values,
)
from ..spa.config import (
    FAMILY_BALBOA,
    FAMILY_BESTWAY,
    FAMILY_GECKO,
    FAMILY_INTEX,
    FAMILY_MSPA,
    FAMILY_SMARTTUB,
    FAMILY_TUYA_INTEX,
    SpaConfig,
)
from ..sauna.config import SaunaConfig
from .config import PoolConfig
from .rules import (
    CIRCULATION_MIN_OFF_MIN,
    CIRCULATION_MIN_ON_MIN,
    CIRCULATION_STARTS_DAY,
    CIRCULATION_STARTS_HOUR,
    FOREIGN_CONFIRM_S,
    HEAT_PUMP_MIN_OFF_MIN,
    HEAT_PUMP_MIN_ON_MIN,
    HEAT_PUMP_STARTS_DAY,
    LOAD_POOL_CIRCULATION,
    LOAD_POOL_HEAT,
    LOAD_SAUNA,
    LOAD_SPA,
    ON_OFF_WRITES_DAY,
    RUN_ON_MIN,
    SETPOINT_WRITES_DAY,
)

#: How each spa family would be steered (spec section 6.2): heat on and held off, or a setpoint between its idle and
#: bath temperatures. A family missing here is watched only and has no handle.
SPA_ON_OFF = frozenset({FAMILY_BALBOA, FAMILY_MSPA, FAMILY_BESTWAY, FAMILY_INTEX, FAMILY_TUYA_INTEX})
SPA_SETPOINT = frozenset({FAMILY_SMARTTUB, FAMILY_GECKO})

#: Where the day's filtering need comes from.
SOURCE_WATER = "water"
SOURCE_OVERRIDE = "override"


def circulation_load(
    site_id: str, pool: PoolConfig, *, need_h: float | None, extra_h: float = 0.0, by: datetime | None = None,
    kw: float | None = None,
) -> FlexibleLoad | None:
    """The circulation pump, `None` without one or with the season closed. `need_h` the day's filtering need (`None`
    until the water has been read), `by` the rolling deadline it is due by, `kw` its power where known."""
    if pool.circulation_id is None or not pool.season_open:
        return None
    return FlexibleLoad(
        id=LOAD_POOL_CIRCULATION,
        kind=KIND_POOL_CIRCULATION,
        site=site_id,
        controlled=pool.control_circulation,
        goal=Goal(
            GOAL_RUNTIME_PER_DAY,
            by=by,
            values=values(
                hours=need_h,
                source=SOURCE_OVERRIDE if pool.filter_hours is not None else SOURCE_WATER,
                extra_h=extra_h,
            ),
        ),
        handles=(Handle(HANDLE_ON_OFF, unit="kW", high=kw if kw is not None else pool.circulation_kw, values=values(entity_id=pool.circulation_id)),),
        hard_rules=(
            rule("season_closed"),
            rule("heat_pump_needs_circulation"),
            rule("run_on", minutes=RUN_ON_MIN),
            rule("min_on", minutes=CIRCULATION_MIN_ON_MIN),
            rule("min_off", minutes=CIRCULATION_MIN_OFF_MIN),
            rule("starts_cap", per_day=CIRCULATION_STARTS_DAY, per_hour=CIRCULATION_STARTS_HOUR),
        ),
        give=Give(GIVE_HOURS_PAUSED),
    )


def heat_pump_load(site_id: str, pool: PoolConfig, *, kw: float | None = None) -> FlexibleLoad | None:
    """The pool's heat pump, `None` without one or with the season closed."""
    pump = pool.heat_pump
    if pump is None or not pool.season_open:
        return None
    return FlexibleLoad(
        id=LOAD_POOL_HEAT,
        kind=KIND_POOL_HEAT,
        site=site_id,
        # The interlock: never controlled without the circulation (`pool/config.py` refuses it too).
        controlled=pool.control_heat_pump and pool.circulation_id is not None,
        goal=Goal(GOAL_TEMPERATURE_BY, values=values(target_c=pool.target_c)),
        handles=(
            Handle(
                HANDLE_ON_OFF,
                unit="kW",
                high=kw if kw is not None else pool.heat_pump_kw,
                values=values(entity_id=pump.entity_id, shape=pump.shape, setpoint_c=pool.target_c),
            ),
        ),
        hard_rules=(
            rule("season_closed"),
            rule("inside_circulation", circulation=pool.circulation_id),
            rule("min_on", minutes=HEAT_PUMP_MIN_ON_MIN),
            rule("min_off", minutes=HEAT_PUMP_MIN_OFF_MIN),
            rule("starts_cap", per_day=HEAT_PUMP_STARTS_DAY),
            rule("write_cap", per_day=SETPOINT_WRITES_DAY),
        ),
        give=Give(GIVE_COMFORT_DEG_H),
    )


def spa_load(
    site_id: str, spa: SpaConfig, *, next_bath: datetime | None = None, ceiling_choice: bool = False,
    kw: float | None = None,
) -> FlexibleLoad:
    """The spa: its bath temperature by `next_bath` (`None`: held at its idle temperature only). `ceiling_choice`: an
    import target or a power level holds the site, so "allow the peak" is a choice."""
    if spa.family in SPA_ON_OFF:
        handles: tuple[Handle, ...] = (
            Handle(HANDLE_ON_OFF, unit="kW", high=kw if kw is not None else spa.heater_kw, values=values(entity_id=spa.entity_id, family=spa.family)),
        )
        writes = ON_OFF_WRITES_DAY
    elif spa.family in SPA_SETPOINT:
        handles = (
            Handle(
                HANDLE_SETPOINT_OFFSET,
                unit="°C",
                low=0.0,
                high=round(spa.bath_c - spa.hold_c, 2),
                values=values(entity_id=spa.entity_id, family=spa.family),
            ),
        )
        writes = SETPOINT_WRITES_DAY
    else:
        handles, writes = (), SETPOINT_WRITES_DAY
    choices = [
        Choice(CHOICE_TAKES_WHAT_FITS, "spa_take_what_fits", sets="spa_take_what_fits"),
        Choice(CHOICE_LATER, "spa_later_bath", sets="spa_later_bath"),
    ]
    if ceiling_choice:
        choices.append(Choice(CHOICE_ACCEPT_PEAK, "spa_accept_peak", sets="spa_accept_peak"))
    return FlexibleLoad(
        id=LOAD_SPA,
        kind=KIND_SPA,
        site=site_id,
        controlled=spa.control and spa.controllable,
        goal=Goal(
            GOAL_TEMPERATURE_BY,
            by=next_bath,
            values=values(bath_c=spa.bath_c, idle_c=spa.idle_c, floor_c=spa.floor_c),
        ),
        handles=handles,
        hard_rules=(
            rule("floor", floor_c=spa.floor_c),
            rule("write_cap", per_day=writes),
            rule("hand_back", confirm_s=FOREIGN_CONFIRM_S),
        ),
        give=Give(GIVE_MINUTES_LATE),
        choices=tuple(choices),
    )


def sauna_load(
    site_id: str, sauna: SaunaConfig, *, next_session: datetime | None = None, ceiling_choice: bool = False,
    kw: float | None = None, max_on_min: float | None = None,
) -> FlexibleLoad:
    """The sauna: its temperature by `next_session` (`None`: off, nothing booked). Never started but for a booked
    session; a readable lock is respected; a relay without its declared safety measure is never controlled."""
    choices = [
        Choice(CHOICE_TAKES_WHAT_FITS, "sauna_take_what_fits", sets="sauna_take_what_fits"),
        Choice(CHOICE_LATER, "sauna_later_session", sets="sauna_later_session"),
    ]
    if ceiling_choice:
        choices.append(Choice(CHOICE_ACCEPT_PEAK, "sauna_accept_peak", sets="sauna_accept_peak"))
    return FlexibleLoad(
        id=LOAD_SAUNA,
        kind=KIND_SAUNA,
        site=site_id,
        controlled=sauna.control and sauna.controllable,
        goal=Goal(GOAL_TEMPERATURE_BY, by=next_session, values=values(target_c=sauna.target_c)),
        handles=(
            Handle(
                HANDLE_ON_OFF,
                unit="kW",
                high=kw if kw is not None else sauna.heater_kw,
                values=values(entity_id=sauna.entity_id, family=sauna.family),
            ),
        ),
        hard_rules=(
            rule("booked_session_only"),
            rule("remote_start_lock", lock=sauna.lock_id, declared=sauna.safety_declared),
            rule("max_on", minutes=max_on_min),
            rule("write_cap", per_day=ON_OFF_WRITES_DAY),
            rule("hand_back", confirm_s=FOREIGN_CONFIRM_S),
        ),
        give=Give(GIVE_MINUTES_LATE),
        choices=tuple(choices),
    )


def pool_spa_loads(site: Any, *, ceiling_choice: bool) -> list[FlexibleLoad]:
    """The site's pool and spa loads from its heat settings and what its pool and spa shadow holds (`pool/runtime.py`,
    absent: no need read yet)."""
    config = site.heat_shadow.config()
    shadow = getattr(site, "pool_spa", None)
    found: list[FlexibleLoad] = []
    pool = config.pool
    if pool is not None:
        facts: dict[str, Any] = {} if shadow is None else shadow.load_facts()
        heat = heat_pump_load(site.entry_id, pool, kw=facts.get("heat_pump_kw"))
        circulation = circulation_load(
            site.entry_id,
            pool,
            need_h=facts.get("need_h", pool.filter_hours),
            extra_h=facts.get("extra_h", 0.0),
            by=facts.get("by"),
            kw=facts.get("circulation_kw"),
        )
        found.extend(load for load in (heat, circulation) if load is not None)
    if config.spa is not None:
        facts = {} if shadow is None else shadow.spa_facts()
        found.append(
            spa_load(site.entry_id, config.spa, next_bath=facts.get("next_bath"), ceiling_choice=ceiling_choice, kw=facts.get("kw"))
        )
    if config.sauna is not None:
        facts = {} if shadow is None else shadow.sauna_facts()
        found.append(
            sauna_load(
                site.entry_id, config.sauna, next_session=facts.get("next_session"), ceiling_choice=ceiling_choice,
                kw=facts.get("kw"), max_on_min=facts.get("max_on_min"),
            )
        )
    return found
