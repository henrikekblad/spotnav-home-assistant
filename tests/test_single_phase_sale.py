"""A sale (a discharge to the grid) on a single-phase site: everything is on L1 at one fuse. The export cap is L1's
room (`Caps._export_cap_kw`), the grid's power is L1's (`runtime.site_signed_w`), the sale's start, step and hand-back
rules judge L1 (`ExecInputs.site_phases`), and the plan sells within one phase's fuse (`plan_runtime.site_export_cap_kw`).
A three-phase site reads and plans as before."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.battery import plan_runtime
from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL
from custom_components.spotnav.battery.control.caps import Caps
from custom_components.spotnav.battery.control.exec_inputs import Inputs
from custom_components.spotnav.battery.executor import (
    HEADROOM_MARGIN_A,
    Executor,
    _import_headroom_a,
    hold_headroom_ok,
    uncovered_a,
)
from custom_components.spotnav.battery.plan_model import ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE, BatteryParams
from custom_components.spotnav.battery.plan_shadow import PriceSlot, build_slots
from custom_components.spotnav.battery.runtime import site_phase_total, site_signed_w
from custom_components.spotnav.const import (
    CONF_DIRECT_ENTITIES,
    CONF_MAIN_FUSE_A,
    CONF_MEASUREMENT_MODE,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
)
from custom_components.spotnav.core.electric import PHASE_V
from custom_components.spotnav.site.site_capacity import PHASES, SINGLE_PHASE
from tests.battery_control_fakes import SIGENERGY_STATES, FakeBattery, inputs

from .helpers import make_site_entry
from .world import controller_of

UTC = timezone.utc
VOLTS = 400.0
PHASE = VOLTS / math.sqrt(3.0)
T0 = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)


class _Shell(Caps, Inputs):
    """The cap's and the look's reads on a stand-in site (no executor, no brand: a plant on one phase)."""

    def __init__(self, site: Any, *, sale_kw: float | None = None) -> None:
        self.hass = None
        self.site = site
        self.facts = SimpleNamespace(control_brand=None, power_kw=None if sale_kw is None else -sale_kw)
        self.executor = None if sale_kw is None else SimpleNamespace(
            state=SimpleNamespace(phase="active", action=ACTION_EXPORT_DISCHARGE, rate_kw=sale_kw)
        )


def _site(l1_w: float | None, *, phases: tuple[str, ...] = SINGLE_PHASE, fuse: float = 100.0, others: float | None = None) -> Any:
    return SimpleNamespace(
        config={CONF_MAIN_FUSE_A: fuse},
        site_phases=phases,
        result=SimpleNamespace(phase_signed_active_power_w={"L1": l1_w, "L2": others, "L3": others}),
    )


# ---- the readings ------------------------------------------------------------------------------------------------


def test_the_grid_power_of_a_single_phase_site_is_l1_alone() -> None:
    site = _site(-4600.0)
    assert site_phase_total(site) == 1 and site_signed_w(site) == [-4600.0]
    assert _Shell(site)._grid_import_kw() == pytest.approx(-4.6)  # noqa: SLF001
    assert _Shell(_site(None))._grid_import_kw() is None  # noqa: SLF001
    # A three-phase site still needs every phase: one not known is no grid power.
    three = _site(-4600.0, phases=PHASES)
    assert site_phase_total(three) == 3 and site_signed_w(three) == [-4600.0, None, None]
    assert _Shell(three)._grid_import_kw() is None  # noqa: SLF001
    assert _Shell(_site(-4600.0, phases=PHASES, others=100.0))._grid_import_kw() == pytest.approx(-4.4)  # noqa: SLF001
    # A site that does not say its phases is three-phase.
    assert site_phase_total(SimpleNamespace()) == 3


# ---- the export cap ----------------------------------------------------------------------------------------------


def test_a_single_phase_site_may_sell_up_to_l1_s_room_and_never_more() -> None:
    # L1 imports: nothing exported yet, the cap is the one phase's fuse less the margin, never three times it.
    cap = _Shell(_site(1500.0))._export_cap_kw()  # noqa: SLF001
    assert cap == pytest.approx((100.0 - HEADROOM_MARGIN_A) * PHASE / 1000.0)
    assert cap < 100.0 * PHASE / 1000.0
    # A small fuse: 25 A is 5.3 kW of sale at most.
    assert _Shell(_site(0.0, fuse=25.0))._export_cap_kw() == pytest.approx(23.0 * PHASE / 1000.0)  # noqa: SLF001


def test_what_l1_already_exports_comes_off_the_sale_s_room() -> None:
    # The sun exports 20 A on L1 with no sale running: 78 A left for the sale.
    shell = _Shell(_site(-20.0 * PHASE))
    assert shell._phase_export_a(VOLTS) == [pytest.approx(20.0)]  # noqa: SLF001
    assert shell._export_cap_kw() == pytest.approx(78.0 * PHASE / 1000.0)  # noqa: SLF001
    # A running sale of 4.6 kW (20 A) is that export: its own share is not counted against it again.
    assert _Shell(_site(-20.0 * PHASE), sale_kw=20.0 * PHASE / 1000.0)._export_cap_kw() == pytest.approx(  # noqa: SLF001
        98.0 * PHASE / 1000.0
    )
    # The sale's 20 A and 30 A of sun beside it: the sun's 30 A come off.
    assert _Shell(_site(-50.0 * PHASE), sale_kw=20.0 * PHASE / 1000.0)._export_cap_kw() == pytest.approx(  # noqa: SLF001
        68.0 * PHASE / 1000.0
    )
    # The sun alone at the fuse: no sale. L1 not known: none either.
    assert _Shell(_site(-99.0 * PHASE))._export_cap_kw() == 0.0  # noqa: SLF001
    assert _Shell(_site(None))._export_cap_kw() == 0.0  # noqa: SLF001


def test_a_three_phase_site_s_export_cap_is_unchanged() -> None:
    # A phase not known: no sale, as before.
    assert _Shell(_site(-4600.0, phases=PHASES))._export_cap_kw() == 0.0  # noqa: SLF001
    # The worst phase counts (the stand-in's plant is on one phase).
    shell = _Shell(_site(-15.0 * PHASE, phases=PHASES, others=-3.0 * PHASE, fuse=25.0))
    assert shell._phase_export_a(VOLTS) == [pytest.approx(15.0), pytest.approx(3.0), pytest.approx(3.0)]  # noqa: SLF001
    assert shell._export_cap_kw() == pytest.approx(8.0 * PHASE / 1000.0)  # noqa: SLF001


# ---- the executor's rules ----------------------------------------------------------------------------------------


def _look(action: str, rate_kw: float, **overrides: Any):
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    return inputs(
        battery, T0, action, rate_kw, **({"volts_between_phases": VOLTS, "charge_phases": 1, "site_phases": 1} | overrides)
    )


def test_a_sale_s_start_rule_counts_the_sale_on_l1_where_the_cover_is() -> None:
    # The battery covers 30 A of house on L1; the headroom is measured under that cover. A sale of at least that
    # keeps the house covered: nothing comes back. A smaller one gives back the difference.
    cover_kw = 30.0 * PHASE / 1000.0
    look = _look("export_discharge", 10.0, power_kw=-cover_kw, headroom_a=20.0)
    assert uncovered_a(look, 10.0) == 0.0
    assert uncovered_a(look, cover_kw / 2) == pytest.approx(15.0)
    assert uncovered_a(look) == pytest.approx(30.0)  # a hold: all of the cover comes back
    assert hold_headroom_ok(look, 10.0) and not hold_headroom_ok(look, 1.0) and not hold_headroom_ok(look)
    # A plant on one phase of a three-phase site: the phase is not known, the sale gets no share, as before.
    three = _look("export_discharge", 10.0, power_kw=-cover_kw, headroom_a=20.0, site_phases=3)
    assert uncovered_a(three, 10.0) == pytest.approx(30.0)


def test_a_sale_at_l1_s_cap_is_not_handed_back_for_the_headroom_its_own_export_takes() -> None:
    # The sale exports 97 A on L1: the headroom reads about nothing, on the export side. That is the export cap's.
    selling = _look("export_discharge", 22.0, power_kw=-22.0, headroom_a=0.5, grid_import_kw=-97.0 * PHASE / 1000.0)
    assert _import_headroom_a(selling) is None
    # The same headroom while L1 imports (or its sign is not known) is the fuse's: handed back.
    assert _import_headroom_a(_look("export_discharge", 22.0, headroom_a=0.5, grid_import_kw=22.0)) == 0.5
    assert _import_headroom_a(_look("export_discharge", 22.0, headroom_a=0.5, grid_import_kw=None)) == 0.5


def test_a_sale_stepped_down_on_l1_is_judged_by_l1_s_sign() -> None:
    # 10 kW sold, L1 exports 40 A beside 3.5 A of house: a step down to 4 kW takes 26 A back and L1 still exports.
    grid_kw = -(10.0 - 0.8)
    look = _look("export_discharge", 4.0, power_kw=-10.0, headroom_a=100.0 - 2.0 - 40.0, grid_import_kw=grid_kw)
    assert not Executor._sale_step_too_large(look, 4.0)  # noqa: SLF001
    # Near the fuse on the export side the headroom is small, and the step is still only less export.
    near = _look("export_discharge", 4.0, power_kw=-22.0, headroom_a=1.0, grid_import_kw=-21.5)
    assert not Executor._sale_step_too_large(near, 4.0)  # noqa: SLF001
    # The house takes more than the sale gives: L1 imports near the fuse, and the step would put it over.
    loaded = _look("export_discharge", 0.5, power_kw=-10.0, headroom_a=5.0, grid_import_kw=11.0)
    assert Executor._sale_step_too_large(loaded, 0.5)  # noqa: SLF001
    # The site's grid power not known: the least headroom less what is taken back, the safe side.
    assert Executor._sale_step_too_large(_look("export_discharge", 4.0, power_kw=-22.0, headroom_a=1.0, grid_import_kw=None), 4.0)  # noqa: SLF001


def test_every_single_phase_step_the_rule_allows_keeps_l1_under_the_fuse_less_the_margin() -> None:
    fuse = 25.0
    checked = 0
    for sale_kw, new_kw in ((5.0, 0.5), (5.0, 3.0), (4.0, 1.0), (3.0, 0.5), (5.5, 2.0)):
        for house_a in (0.0, 3.0, 10.0, 18.0, 22.0, 30.0, 40.0):
            before = house_a - sale_kw * 1000.0 / PHASE
            after = house_a - new_kw * 1000.0 / PHASE
            if abs(before) > fuse:
                continue
            look = _look(
                "export_discharge", new_kw, power_kw=-sale_kw, headroom_a=fuse - abs(before),
                grid_import_kw=before * PHASE / 1000.0,
            )
            if after - before >= HEADROOM_MARGIN_A and not Executor._sale_step_too_large(look, new_kw):  # noqa: SLF001
                checked += 1
                assert after <= fuse - HEADROOM_MARGIN_A + 1e-6, (sale_kw, new_kw, house_a)
    assert checked > 10


# ---- the plan ----------------------------------------------------------------------------------------------------

TZ = "Europe/London"
#: Midnight in London on a winter day (UTC).
MIDNIGHT = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)
#: A Flux-like day (pence per kWh): cheap 02-05, a peak 16-19, the day rate otherwise; export pays most in the peak.
FLUX_BUY = [27.0 if hour < 2 or hour >= 5 else 16.0 for hour in range(24)]
FLUX_SELL = [15.0 if hour < 2 or hour >= 5 else 4.0 for hour in range(24)]
for _hour in (16, 17, 18):
    FLUX_BUY[_hour], FLUX_SELL[_hour] = 38.0, 30.0
PEAK = (16, 17, 18)


def _flux_prices(days: int = 2) -> list[PriceSlot]:
    hour = timedelta(hours=1)
    return [
        PriceSlot(MIDNIGHT + hour * i, MIDNIGHT + hour * (i + 1), FLUX_BUY[i % 24], FLUX_SELL[i % 24])
        for i in range(24 * days)
    ]


def _flux_plan(battery: BatteryParams, export_cap_kw: float | None, mode: str = "max_profit"):
    house = {MIDNIGHT + timedelta(hours=i): 0.4 for i in range(48)}
    solved = plan_runtime._solve(  # noqa: SLF001
        MIDNIGHT, TZ, _flux_prices(), house, None, [], 23.0, battery, 20.0, mode, 2.0,
        {name: None for name in plan_runtime.REVIEWED}, export_cap_kw=export_cap_kw,
    )
    return solved.plans[mode].plan


def _sold(plan: Any) -> dict[int, float]:
    """The energy sold to the grid in each hour of the first day (kWh)."""
    out: dict[int, float] = {}
    for decision in plan.decisions:
        if decision.start < MIDNIGHT + timedelta(days=1) and decision.grid_kw < -1e-6:
            hours = (decision.end - decision.start).total_seconds() / 3600.0
            out[decision.start.hour] = out.get(decision.start.hour, 0.0) - decision.grid_kw * hours
    return out


def test_the_plan_on_a_single_phase_flux_like_day_sells_in_the_peak() -> None:
    """Cheap at night, export paid most 16-19: the plan charges in the cheap hours and sells in the peak, at the
    battery's rating where one phase's fuse (100 A) leaves more."""
    battery = BatteryParams(usable_kwh=15, max_charge_kw=4.4, max_discharge_kw=4.4, min_soc_pct=10, max_soc_pct=100)
    cap = (100.0 - 2.0) * PHASE_V / 1000.0
    plan = _flux_plan(battery, cap)
    sold = _sold(plan)
    assert sold and set(sold) <= set(PEAK)
    assert sum(sold.values()) > 8.0
    first_day = [d for d in plan.decisions if d.start < MIDNIGHT + timedelta(days=1)]
    assert any(d.action == ACTION_EXPORT_DISCHARGE and d.start.hour in PEAK for d in first_day)
    assert any(d.action == ACTION_GRID_CHARGE and 2 <= d.start.hour < 5 for d in first_day)
    assert all(d.battery_kw >= -battery.max_discharge_kw - 1e-6 and d.grid_kw >= -cap - 1e-6 for d in plan.decisions)
    # The fuse leaves more than the battery gives: the plan is the one without the bound.
    free = _flux_plan(battery, None)
    assert [(d.action, round(d.battery_kw, 3)) for d in plan.decisions] == [
        (d.action, round(d.battery_kw, 3)) for d in free.decisions
    ]


def test_the_plan_s_sale_is_capped_at_one_phase_s_fuse() -> None:
    """A battery rated above what a small single-phase fuse carries: the sale is planned at the fuse's room."""
    battery = BatteryParams(usable_kwh=15, max_charge_kw=8.0, max_discharge_kw=8.0, min_soc_pct=10, max_soc_pct=100)
    cap = (16.0 - 2.0) * PHASE_V / 1000.0
    plan = _flux_plan(battery, cap)
    sold = _sold(plan)
    assert sold and set(sold) <= set(PEAK)
    assert all(d.grid_kw >= -cap - 1e-6 for d in plan.decisions)
    assert min(d.grid_kw for d in plan.decisions) == pytest.approx(-cap, abs=0.01)
    # Without the bound (a three-phase site) the same battery sells faster than one phase carries.
    assert min(d.grid_kw for d in _flux_plan(battery, None).decisions) < -cap - 1.0


def test_only_a_single_phase_site_gives_the_plan_an_export_cap() -> None:
    config = {
        CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT, CONF_MAIN_FUSE_A: 100, "safety_margin_a": 1.0,
        CONF_DIRECT_ENTITIES: {"L1": "sensor.l1"},
    }
    # The margin is control's on the export side where the site's own is smaller.
    one = SimpleNamespace(config=config, site_phases=SINGLE_PHASE)
    assert plan_runtime.site_export_cap_kw(one) == pytest.approx(98.0 * PHASE_V / 1000.0)  # type: ignore[arg-type]
    assert plan_runtime.site_export_cap_kw(  # type: ignore[arg-type]
        SimpleNamespace(config={**config, "safety_margin_a": 5.0}, site_phases=SINGLE_PHASE)
    ) == pytest.approx(95.0 * PHASE_V / 1000.0)
    # A stand-in without the controller's phases reads them from the stored meter.
    assert plan_runtime.site_export_cap_kw(SimpleNamespace(config=config)) == pytest.approx(98.0 * PHASE_V / 1000.0)  # type: ignore[arg-type]
    assert plan_runtime.site_export_cap_kw(SimpleNamespace(config=config, site_phases=PHASES)) is None  # type: ignore[arg-type]


def test_a_slot_below_zero_still_exports_nothing_under_the_export_cap() -> None:
    hour = timedelta(hours=1)
    prices = [PriceSlot(MIDNIGHT, MIDNIGHT + hour, 30.0, 10.0), PriceSlot(MIDNIGHT + hour, MIDNIGHT + 2 * hour, 30.0, -5.0)]
    house = {MIDNIGHT: 0.5, MIDNIGHT + hour: 0.5}
    slots, _ = build_slots(MIDNIGHT, prices, house, None, [], None, TZ, export_cap_kw=3.2)
    assert [slot.export_cap_kw for slot in slots] == [3.2, 0.0]
    slots, _ = build_slots(MIDNIGHT, prices, house, None, [], None, TZ)
    assert [slot.export_cap_kw for slot in slots] == [None, 0.0]


# ---- a real site -------------------------------------------------------------------------------------------------


async def test_a_real_single_phase_site_reads_l1_for_the_grid_and_the_sale_s_room(hass: HomeAssistant) -> None:
    """A site measured on L1 alone, as a single-phase GX is: the battery's look has the grid's power and the export
    cap from L1, where L2 and L3 read nothing."""
    hass.states.async_set("sensor.gx_l1_power", str(-20.0 * 230.0), {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.gx_l1_voltage", "230", {"unit_of_measurement": "V"})
    entry = make_site_entry(
        hass, entry_id="gx_one", main_fuse_a=100.0, measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={"L1": {"power": "sensor.gx_l1_power", "voltage": "sensor.gx_l1_voltage"}},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, entry.entry_id)
    assert site.site_phases == SINGLE_PHASE and site.result.state == "observing"
    assert site.result.phase_signed_active_power_w["L2"] is None
    control = site.battery_control
    assert control._grid_import_kw() == pytest.approx(-4.6)  # noqa: SLF001
    assert control._phase_export_a(VOLTS) == [pytest.approx(20.0 * 230.0 / PHASE)]  # noqa: SLF001
    assert control._export_cap_kw() == pytest.approx(  # noqa: SLF001
        (100.0 - HEADROOM_MARGIN_A) * PHASE / 1000.0 - 4.6
    )
    # The house takes over L1: nothing exported, the whole of the one phase's room.
    hass.states.async_set("sensor.gx_l1_power", "2300", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert control._grid_import_kw() == pytest.approx(2.3)  # noqa: SLF001
    assert control._export_cap_kw() == pytest.approx((100.0 - HEADROOM_MARGIN_A) * PHASE / 1000.0)  # noqa: SLF001
