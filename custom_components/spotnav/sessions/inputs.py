"""What a session recorder reads from a live charger: its facts and the prices around now.

The price book is built exactly as the dashboard's chart is: the repository's held day documents for the
charger's market, priced by `planner.chart_intervals` with the fiscal choice the plan itself uses
(`auto_controller.fiscal_choice_for`), so a session is costed with the same effective price per kWh as the
plan's estimated cost. Yesterday is included so a session that runs past midnight keeps pricing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..execution.controller import ChargingController
from ..execution.solar_execution import solar_execution_state
from ..planning.auto_controller import fiscal_choice_for
from ..planning.auto_settings import STRATEGY_HYBRID, STRATEGY_SOLAR
from ..planning.grid_voltage import voltage_between_phases_v
from ..planning.planner import chart_intervals, power_kw
from ..runtime import domain_data
from ..planning.phases import charger_wiring, charging_phases
from ..site.phase_detection import async_detect_phases
from ..vehicles.vehicle_discovery import resolve_target_vehicle
from .recorder import PriceBook, SessionFacts
from .summary import sessions_summary

#: Days around today whose prices a session may need.
DAYS_BEFORE: int = 1
DAYS_AFTER: int = 1


def solar_share_of(hass: HomeAssistant, charger_id: str, strategy: str | None) -> float | None:
    """The fraction of the car's power the surplus covers now, or `None` when the solar executor does not
    know it (another strategy, no verdict yet, a plan window holding the charger, no car power)."""
    if strategy not in (STRATEGY_SOLAR, STRATEGY_HYBRID):
        return None
    state = solar_execution_state(hass, charger_id)
    if state is None or state.held_by_plan or state.state not in ("on", "arming", "disarming"):
        return None
    if state.car_w is None or state.available_w is None or state.car_w <= 0:
        return None
    return min(1.0, max(0.0, state.available_w) / state.car_w)


def session_facts(hass: HomeAssistant, controller: ChargingController) -> SessionFacts:
    charger_id = controller.entry_id
    store = domain_data(hass).auto_store
    settings = None if store is None else store.settings(charger_id)
    adapter = controller.adapter
    amps = controller.requested_current_a
    if amps is None and controller.plan is not None:
        amps = controller.plan.amps
    if amps is None and settings is not None:
        amps = settings.amps
    # The charger's wiring, else what its entities suggest, held to the planned car's onboard charger.
    phases = charger_wiring(hass, charger_id)
    if phases is None:
        phases = async_detect_phases(hass, controller.charge_control).detected_phases
    car_phases = charging_phases(hass, charger_id).vehicle
    if phases is not None and car_phases is not None:
        phases = min(phases, car_phases)
    estimate_kw = (
        power_kw(amps, phases, voltage_between_phases_v(hass, charger_id))
        if amps and phases in (1, 3)
        else None
    )
    vehicle_id = vehicle_name = None
    if settings is not None:
        vehicle_id, candidates = resolve_target_vehicle(hass, settings.target.vehicle_id)
        vehicle_name = next((item.name for item in candidates if item.id == vehicle_id), None)
    strategy = None if settings is None else settings.strategy
    return SessionFacts(
        charging=controller.charging,
        connected=adapter.vehicle_connected(),
        register_kwh=adapter.energy_register_kwh(),
        register_integrated=controller.energy_from_power,
        estimate_kw=estimate_kw,
        strategy=strategy,
        vehicle_id=vehicle_id,
        vehicle_name=vehicle_name,
        solar_share=solar_share_of(hass, charger_id, strategy),
    )


def price_book_for(hass: HomeAssistant, charger_id: str, now: datetime) -> PriceBook | None:
    """The effective prices of the charger's market around `now`, or `None` with no market or settings."""
    data = domain_data(hass)
    store, repository = data.auto_store, data.price_repository
    if store is None or repository is None:
        return None
    settings = store.settings(charger_id)
    area_id = settings.area_id
    catalogue = repository.catalogue_snapshot()
    area = None if area_id is None else catalogue.area(area_id)
    if area is None:
        return None
    zone = dt_util.get_time_zone(area.tz)
    fiscal = fiscal_choice_for(settings, area)
    if zone is None or fiscal is None:
        return PriceBook((), area.currency, area.major_unit, area.minor_unit)
    today = now.astimezone(zone).date()
    documents = []
    for offset in range(-DAYS_BEFORE, DAYS_AFTER + 1):
        document = repository.day_snapshot(area_id, today + timedelta(days=offset)).document
        if document is not None:
            documents.append(document)
    intervals = (
        chart_intervals(tuple(documents), currency=area.currency, fiscal=fiscal) if documents else ()
    )
    return PriceBook(intervals, area.currency, area.major_unit, area.minor_unit)


def local_zone(hass: HomeAssistant):
    """Home Assistant's own time zone, the one days and months are counted in."""
    return dt_util.get_time_zone(hass.config.time_zone) or dt_util.UTC


def sessions_block(hass: HomeAssistant, charger_id: str, now: datetime) -> dict | None:
    """The dashboard's additive `sessions_summary`: this month and last month, or `None` with no record."""
    store = domain_data(hass).session_store
    if store is None:
        return None
    return sessions_summary(store.closed(charger_id), local_zone(hass), now)
