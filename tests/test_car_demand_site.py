"""The car's plan under the site's soft demand ceiling, in shadow, end to end: a charger in Auto in the pilot's site
plans as before, and once more under the ceiling the site gives it (`SiteDemandCeiling.car_inputs`), kept in the
diagnostics' plan watch and written to the site's change log only when it changes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.car_demand import CarCeiling
from custom_components.spotnav.planning.demand_charge import hour_start
from custom_components.spotnav.planning.demand_cost import HourCeiling
from custom_components.spotnav.runtime import preview_for, site_controller_for
from custom_components.spotnav.site.demand_ceiling import SiteDemandCeiling

from .test_demand_site import pilot_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _ceiling(kw: float) -> Any:
    def inputs(_self: Any, _charger: str, now: Any) -> CarCeiling:
        first = hour_start(now, "Europe/Stockholm")
        hours = [first + timedelta(hours=offset) for offset in range(48)]
        return CarCeiling(
            ceilings={start: HourCeiling(kw, 8.0) for start in hours},
            house_kw={start: 1.0 for start in hours},
            others_kw={},
            tz="Europe/Stockholm",
        )

    return inputs


def _logged(controller: Any) -> list[dict[str, Any]]:
    return [entry for entry in controller._history.target_log if entry["action"] == "demand_car"]


async def test_the_plan_in_force_is_unchanged_and_the_shadow_is_kept_and_logged_once(
    hass: HomeAssistant, freezer, monkeypatch
) -> None:
    site = await pilot_site(hass, freezer)
    preview = preview_for(hass, "entry_a")
    controller = site_controller_for(hass, site.entry_id)
    assert preview is not None and controller is not None
    before = preview.snapshot()
    assert before.proposal is not None and before.proposal.has_plan
    # The pilot has no house forecast yet: no ceiling for the car, nothing kept.
    assert preview.plan_watch_facts()["demand_shadow"] is None

    monkeypatch.setattr(SiteDemandCeiling, "car_inputs", _ceiling(9.0))
    await preview.async_recalculate()
    after = preview.snapshot()
    assert after.proposal is not None and after.proposal.periods == before.proposal.periods
    facts = preview.plan_watch_facts()["demand_shadow"]
    # 8 kW of room above the house: the car's own power fits, nothing raised, nothing over the ceiling.
    assert facts["fits"] and facts["raise_kw"] == 0.0 and facts["demand_cost"] == 0
    assert facts["periods"] == [[start.isoformat(), end.isoformat()] for start, end in before.proposal.periods]
    assert facts["in_force"]["demand_cost"] == 0
    assert len(_logged(controller)) == 1
    # The same plan again: not logged again.
    await preview.async_recalculate()
    assert len(_logged(controller)) == 1

    # 1 kW of room is below the charger's minimum: every hour paused. The pilot's car has no departure, so nothing is
    # raised and the plan under the ceiling waits; the plan in force is costed above the ceiling.
    monkeypatch.setattr(SiteDemandCeiling, "car_inputs", _ceiling(2.0))
    await preview.async_recalculate()
    assert preview.snapshot().proposal.periods == before.proposal.periods
    facts = preview.plan_watch_facts()["demand_shadow"]
    assert facts["reason"] == "insufficient_price_horizon" and facts["raise_kw"] == 0.0
    assert facts["in_force"]["demand_cost"] > 0
    assert len(_logged(controller)) == 2
