"""`execution/hybrid_execution.py`: the glue between the pure `planning/hybrid_plan.py` core and Home Assistant.

Two things are pinned here:

* `hybrid_needs_replan` -- the design's own three replan triggers (delivered energy moving the
  remaining need by `replan_step_kwh`, the forecast changing, and the result's state entering
  `last_call`), as a pure function directly against `ReplanMemory`, each trigger isolated from
  the other two so a change that breaks exactly one of them fails exactly one test here.
* `async_plan_hybrid` plans `grid_kwh` (not the full remaining need) over the *same* price
  slots `planner.calculate_plan` would use for identical inputs -- proved by calling both
  against the same `documents`/`now`/`departure` and checking hybrid's own full-need-equivalent
  plan (no forecast configured) reproduces cheapest's own selected slots exactly, while a
  configured forecast credit measurably lowers `grid_kwh` below the full need.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution import hybrid_execution
from custom_components.spotnav.planning.auto_settings import AutoSettings
from custom_components.spotnav.execution.hybrid_execution import (
    ReplanMemory,
    async_plan_hybrid,
    clear_hybrid_state,
    hybrid_needs_replan,
    hybrid_state,
)
from custom_components.spotnav.planning.hybrid_forecast import ForecastReadResult
from custom_components.spotnav.planning.hybrid_plan import HybridConfig
from custom_components.spotnav.planning.planner import (
    CONTRACT_VERSION,
    FiscalChoice,
    PlanRequest,
    calculate_plan,
)
from custom_components.spotnav.pricing.relay_contract import parse_day

UTC = timezone.utc
CONFIG = HybridConfig(solar_start_w=4100.0)


def _memory(kwh: float, forecast: dict | None = None, state: str = "waiting_for_sun") -> ReplanMemory:
    return ReplanMemory(remaining_need_kwh=kwh, forecast_wh=forecast, state=state)  # type: ignore[arg-type]


# ------------------------------------------------------------------- hybrid_needs_replan


def test_first_call_always_replans() -> None:
    assert hybrid_needs_replan(None, _memory(10.0), CONFIG) is True


def test_delivered_energy_below_the_step_does_not_replan() -> None:
    previous = _memory(10.0)
    current = _memory(10.0 - (CONFIG.replan_step_kwh - 0.01))
    assert hybrid_needs_replan(previous, current, CONFIG) is False


def test_delivered_energy_at_or_above_the_step_replans() -> None:
    previous = _memory(10.0)
    current = _memory(10.0 - CONFIG.replan_step_kwh)
    assert hybrid_needs_replan(previous, current, CONFIG) is True


def test_an_unchanged_forecast_does_not_replan() -> None:
    forecast = {datetime(2026, 9, 28, 10, tzinfo=UTC): 1000.0}
    previous = _memory(10.0, forecast=forecast)
    current = _memory(10.0, forecast=dict(forecast))
    assert hybrid_needs_replan(previous, current, CONFIG) is False


def test_a_changed_forecast_replans_even_with_the_need_unchanged() -> None:
    previous = _memory(10.0, forecast={datetime(2026, 9, 28, 10, tzinfo=UTC): 1000.0})
    current = _memory(10.0, forecast={datetime(2026, 9, 28, 10, tzinfo=UTC): 2000.0})
    assert hybrid_needs_replan(previous, current, CONFIG) is True


def test_a_forecast_source_appearing_replans() -> None:
    previous = _memory(10.0, forecast=None)
    current = _memory(10.0, forecast={datetime(2026, 9, 28, 10, tzinfo=UTC): 1000.0})
    assert hybrid_needs_replan(previous, current, CONFIG) is True


def test_entering_last_call_replans_even_with_the_need_and_forecast_unchanged() -> None:
    forecast = {datetime(2026, 9, 28, 10, tzinfo=UTC): 1000.0}
    previous = _memory(10.0, forecast=forecast, state="waiting_for_sun")
    current = _memory(10.0, forecast=dict(forecast), state="last_call")
    assert hybrid_needs_replan(previous, current, CONFIG) is True


def test_leaving_last_call_alone_is_not_itself_a_trigger() -> None:
    """Only *entering* `last_call` is named; leaving it again is already covered by the need
    or the forecast having moved (nothing else can raise `slack_kwh` back off zero) -- so this
    state transition by itself, with neither of the other two facts having moved, must not
    double-count as a fourth trigger."""
    forecast = {datetime(2026, 9, 28, 10, tzinfo=UTC): 1000.0}
    previous = _memory(10.0, forecast=forecast, state="last_call")
    current = _memory(10.0, forecast=dict(forecast), state="waiting_for_sun")
    assert hybrid_needs_replan(previous, current, CONFIG) is False


# ------------------------------------------------------------- async_plan_hybrid wiring


def _day_document(day: date, prices: list[float]) -> Any:
    """One 15-minute EUR document -- `tests/test_planner.py`'s own `day_body` shape, parsed
    through the real `relay_contract.parse_day`. EUR needs no fx table at all (`_rate_for`'s
    own EUR shortcut), which keeps this fixture down to exactly the fields that matter."""
    body = {
        "v": CONTRACT_VERSION,
        "area": "SE4",
        "date": day.isoformat(),
        "tz": "Europe/Stockholm",
        "res": 15,
        "start": f"{day.isoformat()}T00:00:00+02:00",
        "unit": "EUR/kWh",
        "prices": prices,
    }
    return parse_day(body, area_id="SE4", day=day)


async def test_hybrid_with_no_forecast_plans_the_same_slots_as_cheapest(hass: HomeAssistant) -> None:
    """No `solar_forecast_entries` configured for this (nonexistent) charger's site --
    `async_plan_hybrid` reads no forecast, `plan_hybrid` returns `no_forecast`/the full need,
    and feeding that `grid_kwh` back into `calculate_plan` reproduces cheapest's own plan over
    the identical documents, `now` and settings -- the same slots, not merely the same energy."""
    now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
    documents = (
        _day_document(date(2026, 9, 28), [0.10] * 96),
        _day_document(date(2026, 9, 29), [0.10] * 96),
    )
    settings = AutoSettings(area_id="SE4", amps=10, phases=3, departure_enabled=False)
    fiscal = FiscalChoice()

    def plan_for(requested_kwh: float) -> Any:
        return calculate_plan(
            PlanRequest(
                area_id="SE4", timezone="Europe/Stockholm", currency="EUR", major_unit="EUR",
                minor_unit="cent", documents=documents, now=now, phases=3, amps=10,
                requested_kwh=requested_kwh, consumption_kwh_per_10km=2.0, fiscal=fiscal,
            )
        )

    cheapest = plan_for(5.0)
    assert cheapest.has_plan

    outcome = await async_plan_hybrid(
        hass, "no-such-charger", settings, documents=documents, tz="Europe/Stockholm",
        currency="EUR", fiscal=fiscal, now=now, remaining_need_kwh=5.0,
    )

    assert outcome.result.state == "no_forecast"
    assert outcome.result.grid_kwh == pytest.approx(5.0)

    hybrid_plan = plan_for(outcome.result.grid_kwh)
    assert hybrid_plan.has_plan
    assert hybrid_plan.periods == cheapest.periods, "the same slots cheapest itself would choose"


async def test_a_configured_forecast_lowers_grid_kwh_below_the_full_need(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same wiring: once a forecast is readable, `grid_kwh` drops below
    `remaining_need_kwh` -- `async_read_forecast_wh` is monkeypatched directly here (its own
    correctness, and the site-config lookup that names which entries to read, are
    `tests/test_hybrid_forecast.py`'s and `tests/test_config_flow.py`'s jobs respectively);
    this test is only about `async_plan_hybrid` actually *using* what it is handed."""
    now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
    documents = (
        _day_document(date(2026, 9, 28), [0.10] * 96),
        _day_document(date(2026, 9, 29), [0.10] * 96),
    )
    settings = AutoSettings(
        area_id="SE4", amps=10, phases=3, departure_enabled=True, departure=time(20, 0),
    )

    sunny_hour = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)

    async def fake_read(_hass: HomeAssistant, _entry_ids: list[str]) -> ForecastReadResult:
        return ForecastReadResult(wh_hours={sunny_hour: 6000.0}, sources_read=("fake_entry",))

    monkeypatch.setattr(hybrid_execution, "async_read_forecast_wh", fake_read)

    outcome = await async_plan_hybrid(
        hass, "no-such-charger", settings, documents=documents, tz="Europe/Stockholm",
        currency="EUR", fiscal=FiscalChoice(), now=now, remaining_need_kwh=5.0,
    )

    assert outcome.result.grid_kwh < 5.0
    assert outcome.result.credit_kwh > 0.0
    assert outcome.sources_read == ("fake_entry",)


# ------------------------------------------------------------------- hybrid_state store


def test_hybrid_state_defaults_to_none_and_clears_idempotently(hass: HomeAssistant) -> None:
    assert hybrid_state(hass, "no-such-charger") is None
    clear_hybrid_state(hass, "no-such-charger")  # never raises with nothing to clear
    assert hybrid_state(hass, "no-such-charger") is None
