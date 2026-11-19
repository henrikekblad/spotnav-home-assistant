"""The shell of what SpotNav saved (`site/spotnav_saving.py`): reads the parts a site holds and the recorder's hours the
car's figure needs, for the house card (`api/house.py`, `api/house_history.py`) and the charger card's history
(`api/sessions.py`), which so read one computation.

What is read: the site's chargers' sessions (the session store, in memory); for the sessions with a plug-in time, the
Energy dashboard's meters and the chargers' registers by the hour from the recorder's long-term statistics over their
span (`api/house._hourly_periods`, in the recorder's executor) and the area's held day prices with the fees as set
now (`pricing/period_prices.period_price_slots`, in memory); the battery's day money (`battery/stored_value.py`) and the
heat shadow's days (`heat/runtime.py`), in memory. A session's figure is kept per site once it is settled (closed,
and its last hour two hours behind, so the statistics have it); an open or fresh one is worked out again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback

from .. import time_zones
from ..const import CONF_CHARGER_ENTRY_IDS, DOMAIN
from ..planning.demand_charge import hour_start
from ..pricing.period_prices import period_price_slots
from ..runtime import domain_data
from ..sessions.model import ChargeSession
from ..site.history_period import PERIOD_DAY, Period
from ..site.spotnav_saving import (
    DaySaving,
    GridShares,
    Prices,
    car_days,
    car_hours,
    day_savings,
    default_share,
    session_savings,
)

_LOGGER = logging.getLogger(__name__)

_CACHE: Final = f"{DOMAIN}_session_savings"
#: A closed session's figure is kept once its last hour is this far behind (the statistics have it).
SETTLED_AFTER: Final = timedelta(hours=2)


def site_sessions(hass: HomeAssistant, controller: Any) -> list[ChargeSession]:
    """The site's chargers' sessions, closed and open (unpriced; only the energy and times are read)."""
    store = getattr(domain_data(hass), "session_store", None)
    if store is None:
        return []
    out: list[ChargeSession] = []
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        out.extend(store.closed_raw(charger_entry_id))
        if (open_session := store.open_raw(charger_entry_id)) is not None:
            out.append(open_session)
    return out


def _settled(session: ChargeSession, now: datetime) -> bool:
    return session.end is not None and session.end + SETTLED_AFTER <= now


async def async_session_savings(
    hass: HomeAssistant, site_entry_id: str, controller: Any, sessions: Iterable[ChargeSession]
) -> dict[str, float | None]:
    """Each session's car saving (minor units, `site/spotnav_saving.session_savings`), `None` where it cannot be
    known; the plug-ins of the sessions given are completed from the site's other sessions."""
    from .house import _car_register_ids, _hourly_periods  # noqa: PLC0415 - the house API imports this module
    from ..site.energy_statistics import async_energy_prefs, energy_flow_ids  # noqa: PLC0415

    now = controller.demand.now()
    tz = controller.demand.tracker.tz
    sessions = list(sessions)
    out: dict[str, float | None] = {session.id: None for session in sessions}
    cache: dict[str, dict[str, float | None]] = hass.data.setdefault(_CACHE, {}).setdefault(site_entry_id, {})
    wanted = [session for session in sessions if session.plugged_in_at is not None]
    for session in wanted:
        if session.id in cache:
            out[session.id] = cache[session.id]
    todo = [session for session in wanted if session.id not in cache]
    if not todo:
        return out
    plugged = {(session.charger_id, session.plugged_in_at) for session in todo}
    group = [
        session for session in site_sessions(hass, controller) if (session.charger_id, session.plugged_in_at) in plugged
    ]
    group_ids = {session.id for session in group}
    group += [session for session in todo if session.id not in group_ids]
    first = min(session.plugged_in_at for session in group if session.plugged_in_at is not None)
    last = max(session.end or session.last_sample_at or now for session in group)
    zone = time_zones.require_zone(tz)
    period = Period(PERIOD_DAY, first.astimezone(zone).date(), last.astimezone(zone).date() + timedelta(days=1))
    prices = Prices(period_price_slots(hass, site_entry_id, period))
    ids = energy_flow_ids(await async_energy_prefs(hass))
    periods: Sequence[Any] = ()
    end = min(hour_start(now, tz), hour_start(last, tz) + timedelta(hours=1))
    start = hour_start(min(session.start for session in group), tz)
    if ids.grid_in and end > start:
        hours = []
        moment = start
        while moment < end:
            hours.append(moment)
            moment += timedelta(hours=1)
        heat = controller.house_forecast.heat
        try:
            periods = await _hourly_periods(
                hass,
                controller,
                hours,
                end,
                ids,
                _car_register_ids(hass, controller),
                heat.meter,
                await heat.async_current_zones(),
            )
        except Exception:  # noqa: BLE001 - without the hours the default share stands
            _LOGGER.debug("SpotNav saving: the car's hours could not be read", exc_info=True)
    has_battery = bool(ids.battery_in) or controller.battery_shadow.facts.battery_entity_id is not None
    shares = GridShares(periods, default_share(has_battery, bool(ids.solar)))
    figures = session_savings(group, prices, shares, now)
    for session in group:
        value = figures.get(session.id)
        if session.id in out:
            out[session.id] = value
        if _settled(session, now) and value is not None:
            cache[session.id] = value
    return out


async def async_day_savings(
    hass: HomeAssistant, site_entry_id: str, controller: Any, days: Sequence[date]
) -> tuple[dict[date, DaySaving], date | None]:
    """Each day's parts of what SpotNav saved (`site/spotnav_saving.day_savings`) and the first day the car's figure
    is known from."""
    savings, since, _hours = await async_day_savings_by_hour(hass, site_entry_id, controller, days)
    return savings, since


async def async_day_savings_by_hour(
    hass: HomeAssistant, site_entry_id: str, controller: Any, days: Sequence[date]
) -> tuple[dict[date, DaySaving], date | None, dict[datetime, float]]:
    """`async_day_savings` and, from the same computation, the car's saving per UTC hour its charges started in
    (`site/spotnav_saving.car_hours`), for a day's hourly bars."""
    if not days:
        return {}, None, {}
    now = controller.demand.now()
    tz = controller.demand.tracker.tz
    zone = time_zones.require_zone(tz)
    today = now.astimezone(zone).date()
    first, last = min(days), max(days)
    all_sessions = site_sessions(hass, controller)
    shown = [session for session in all_sessions if first <= session.start.astimezone(zone).date() <= last]
    savings = await async_session_savings(hass, site_entry_id, controller, shown)
    car, _since = car_days(shown, savings, zone)
    _all_days, since = car_days(all_sessions, {}, zone)
    battery_money: dict[date, Any] = {}
    if controller.battery_shadow.facts.battery_entity_id is not None:
        stored = controller.battery_shadow.plan.stored
        battery_money = dict(stored.days) if stored is not None else {}
    heat_days: dict[date, Any] = {}
    shadow = getattr(controller, "heat_shadow", None)
    for key, record in (getattr(shadow, "days", None) or {}).items():
        try:
            heat_days[date.fromisoformat(key)] = record
        except (TypeError, ValueError):
            continue
    return (
        day_savings(days, battery_money=battery_money, heat_days=heat_days, car=car, car_since=since, today=today),
        since,
        car_hours(shown, savings),
    )


@callback
def forget_cached_savings(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop a site's kept session figures (its unload)."""
    cache: dict[str, Any] | None = hass.data.get(_CACHE)
    if cache:
        cache.pop(site_entry_id, None)
