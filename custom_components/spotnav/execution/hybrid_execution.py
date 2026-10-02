"""Hybrid mode's execution glue.

`planning/hybrid_plan.py` is the pure core (`plan_hybrid`, `needs_replan`). This module supplies
what it needs from Home Assistant for one charger, once per Auto calculation: the forecast, the
same price slots and departure the cheapest planner resolves, `max_charge_kw` and `solar_start_w`
from the charger's configuration, and the diagnostics the site sensor reads back.

Replanning: `auto_controller._compute` already recalculates on the existing triggers.
`hybrid_needs_replan` only decides when to log a "plan changed" line (`HYBRID_LOG_TOKEN`); it does
not gate the calculation, because `grid_kwh` is cheap and pure and the executor skips installs that
change nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import CONF_PHASE_WIRING, CONF_SOLAR_FORECAST_ENTRIES, DEFAULT_MIN_CURRENT_A
from ..planning.auto_settings import AutoSettings
from ..planning.hybrid_forecast import async_read_forecast_wh, ForecastReadResult
from ..planning.hybrid_plan import (
    HybridConfig,
    HybridResult,
    HybridState,
    needs_replan,
    plan_hybrid,
    PriceSlot,
)
from ..planning.planner import (
    departure_for,
    effective_minor_per_kwh,
    first_start_for,
    FiscalChoice,
    HORIZON_HOURS,
    planning_slots,
    power_kw,
    STEP_MINUTES,
)
from ..pricing.relay_contract import PriceDocument
from ..runtime import charger_data
from .solar_execution import site_controller_for_charger


_LOGGER = logging.getLogger(__name__)

#: Greppable token carried by every hybrid log line (cf. `solar_execution.SOLAR_SURPLUS_LOG_TOKEN`).
HYBRID_LOG_TOKEN: Final = "HYBRID"


@dataclass(frozen=True, slots=True)
class HybridChargerState:
    """One charger's latest hybrid diagnostics, for the site sensor's `hybrid` attribute
    (the counterpart of `solar_execution.SolarExecutionState`).
    """

    enabled: bool
    state: HybridState
    reason: str
    grid_kwh: float
    credit_kwh: float
    forecast_credit_kwh: float
    slack_kwh: float
    plan_window_active: bool
    forecast_sources: tuple[str, ...]


def hybrid_state(hass: HomeAssistant, charger_entry_id: str) -> HybridChargerState | None:
    """The latest `HybridChargerState`, or `None` when nothing was computed or the
    strategy is not `hybrid` (see `clear_hybrid_state`).
    """
    data = charger_data(hass, charger_entry_id)
    return None if data is None else data.hybrid_state


def clear_hybrid_state(hass: HomeAssistant, charger_entry_id: str) -> None:
    """Drop this charger's hybrid diagnostics and replan memory when its strategy is not
    `hybrid`, so it reads as "nothing observed". Idempotent.
    """
    data = charger_data(hass, charger_entry_id)
    if data is not None:
        data.hybrid_state = None
        data.hybrid_memory = None


@dataclass(frozen=True, slots=True)
class ReplanMemory:
    """The previous cycle's inputs and outcome, compared by `hybrid_needs_replan`.
    `forecast_wh` is compared by value; two reads that found nothing (`None == None`) are
    not a forecast change.
    """

    remaining_need_kwh: float
    forecast_wh: dict[datetime, float] | None
    state: HybridState


def hybrid_needs_replan(
    previous: ReplanMemory | None, current: ReplanMemory, config: HybridConfig
) -> bool:
    """Whether this cycle's hybrid facts differ enough from the last to count as a replan.

    The first evaluation always counts. Otherwise: delivered energy moved the need by at least
    `config.replan_step_kwh`; the forecast changed (new reading, source appearing or disappearing, other
    numbers); or the state changed *into* `last_call`.
    """
    if previous is None:
        return True
    if needs_replan(previous.remaining_need_kwh, current.remaining_need_kwh, config):
        return True
    if previous.forecast_wh != current.forecast_wh:
        return True
    return current.state == "last_call" and previous.state != "last_call"  # noqa: E501


def _phase_wiring(site: Any, charger_entry_id: str) -> dict[str, Any]:
    return (site.config.get(CONF_PHASE_WIRING) or {}).get(charger_entry_id) or {}


@dataclass(frozen=True, slots=True)
class HybridPlanOutcome:
    """One cycle's hybrid planning outcome, for `auto_controller._compute` to act on."""

    result: HybridResult
    departure: datetime
    sources_read: tuple[str, ...]
    replanned: bool


async def async_plan_hybrid(
    hass: HomeAssistant,
    charger_entry_id: str,
    settings: AutoSettings,
    *,
    documents: tuple[PriceDocument, ...],
    tz: str,
    currency: str,
    fiscal: FiscalChoice,
    now: datetime,
    remaining_need_kwh: float,
    delivered_energy_trustworthy: bool = True,
) -> HybridPlanOutcome:
    """Turn `remaining_need_kwh` into `grid_kwh` for this charger over the *same* price slots and
    departure the cheapest planner would use.

    `delivered_energy_trustworthy` is `False` when a `manual_kwh` charger has no reliable energy-register
    signal, so the need is not reduced for energy already delivered. Crediting forecast sun against it
    could hold back grid energy for a car nearer done than it looks, so the forecast is then not read at
    all (`no_forecast`).
    """
    site = site_controller_for_charger(hass, charger_entry_id)
    if not delivered_energy_trustworthy:
        forecast_read = ForecastReadResult(wh_hours=None, sources_read=())
    else:
        entries: list[str] = list(
            (site.config.get(CONF_SOLAR_FORECAST_ENTRIES) if site is not None else None) or []
        )
        forecast_read = await async_read_forecast_wh(hass, entries)

    amps = settings.amps if settings.amps is not None else 0
    phases = settings.phases if settings.phases is not None else 3
    max_charge_kw = power_kw(amps, phases)

    if site is not None:
        wiring = _phase_wiring(site, charger_entry_id)
        min_current_a = float(wiring.get("min_current_a", DEFAULT_MIN_CURRENT_A))
        car_phases = 3 if wiring.get("phases", 3) == 3 else 1
    else:
        # No site: hybrid still runs (no readable forecast makes it plain cheapest) but needs some
        # `solar_start_w` to build a valid `HybridConfig`.
        min_current_a = DEFAULT_MIN_CURRENT_A
        car_phases = 3
    # The lowest power a charge can start at: the charger's own start minimum counts (Easee: 7 A).
    data = charger_data(hass, charger_entry_id)
    if data is not None:
        min_current_a = max(min_current_a, data.controller.adapter.min_start_current_a)
    solar_start_w = power_kw(min_current_a, car_phases) * 1000.0

    if documents:
        if settings.departure_enabled:
            departure = departure_for(documents, now=now, tz=tz, departure=settings.departure)
        else:
            # No deadline: mirror `calculate_plan`'s no-deadline geometry (24-hour horizon from the
            # first usable slot) so there is a window to plan a safety margin against.
            departure = first_start_for(documents, now=now, tz=tz) + timedelta(hours=HORIZON_HOURS)
        slots = planning_slots(documents, currency=currency, timezone=tz)
        price_slots = tuple(
            PriceSlot(
                start=slot.start,
                duration_s=STEP_MINUTES * 60.0,
                price=effective_minor_per_kwh(slot.local_major_per_kwh, fiscal),
            )
            for slot in slots
        )
    else:
        # No price documents: `_compute` gates on this earlier, but this function stays total;
        # empty `price_slots` is `plan_hybrid`'s `no_price_data` state, not a crash.
        departure = now
        price_slots = ()

    config = HybridConfig(solar_start_w=solar_start_w)
    result = plan_hybrid(
        now=now,
        departure=departure,
        remaining_need_kwh=remaining_need_kwh,
        max_charge_kw=max_charge_kw,
        config=config,
        price_slots=price_slots,
        forecast_wh=forecast_read.wh_hours,
    )

    data = charger_data(hass, charger_entry_id)
    previous = None if data is None else data.hybrid_memory
    current = ReplanMemory(
        remaining_need_kwh=remaining_need_kwh, forecast_wh=forecast_read.wh_hours, state=result.state
    )
    replanned = hybrid_needs_replan(previous, current, config)
    if data is not None:
        data.hybrid_memory = current

    return HybridPlanOutcome(
        result=result, departure=departure, sources_read=forecast_read.sources_read, replanned=replanned
    )


def log_hybrid_decision(charger_entry_id: str, outcome: HybridPlanOutcome, *, plan_window_active: bool) -> None:
    """INFO, once per real change (`outcome.replanned`), under the token `HYBRID`."""
    if not outcome.replanned:
        return
    result = outcome.result
    _LOGGER.debug(
        "%s charger %s: state=%s reason=%s grid_kwh=%.3f credit_kwh=%.3f forecast_credit_kwh=%.3f "
        "slack_kwh=%.3f plan_window_active=%s sources=%s",
        HYBRID_LOG_TOKEN,
        charger_entry_id,
        result.state,
        result.reason,
        result.grid_kwh,
        result.credit_kwh,
        result.forecast_credit_kwh,
        result.slack_kwh,
        plan_window_active,
        outcome.sources_read,
    )


def record_hybrid_state(
    hass: HomeAssistant,
    charger_entry_id: str,
    outcome: HybridPlanOutcome,
    *,
    plan_window_active: bool,
) -> None:
    """Publish this cycle's `HybridChargerState` for the site sensor and log the decision
    when it changed. Fresh every cycle, regardless of `outcome.replanned`.
    """
    result = outcome.result
    data = charger_data(hass, charger_entry_id)
    if data is not None:
        data.hybrid_state = HybridChargerState(
            enabled=True,
            state=result.state,
            reason=result.reason,
            grid_kwh=result.grid_kwh,
            credit_kwh=result.credit_kwh,
            forecast_credit_kwh=result.forecast_credit_kwh,
            slack_kwh=result.slack_kwh,
            plan_window_active=plan_window_active,
            forecast_sources=outcome.sources_read,
        )
    log_hybrid_decision(charger_entry_id, outcome, plan_window_active=plan_window_active)


def record_satisfied_state(hass: HomeAssistant, charger_entry_id: str) -> None:
    """Record `plan_hybrid`'s `satisfied` result for a charger whose remaining need is already known
    to be `<= 0`, without calling `async_plan_hybrid`.

    Callers: the target-SoC "already at target" short-circuit, and `manual_kwh` once delivered energy
    covers the request.
    """
    result = HybridResult(
        grid_kwh=0.0,
        credit_kwh=0.0,
        forecast_credit_kwh=0.0,
        slack_kwh=0.0,
        state="satisfied",
        reason="satisfied_need_met",
    )
    outcome = HybridPlanOutcome(
        result=result, departure=dt_util.utcnow(), sources_read=(), replanned=True
    )
    record_hybrid_state(hass, charger_entry_id, outcome, plan_window_active=False)
