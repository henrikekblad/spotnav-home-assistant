"""The one amps-to-kW rule (`core/electric.py`) and the readers of the site's electric settings: a TN 3 x 20 A site
keeps the planner's and the battery gate's figure (13.86 kW; the heat cap, the house card's fuse and the import
target's ceiling, which counted 3 x 230 V, 13.80 kW, now agree with it), an IT site (230 V between phases) no longer
counts 690 W per ampere, a single-phase load is 230 V on either network, and a missing safety margin is the form's
1.0 A."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from types import SimpleNamespace

import pytest

from custom_components.spotnav.const import (
    CONF_MAIN_FUSE_A,
    CONF_SAFETY_MARGIN_A,
    CONF_VOLTAGE_BETWEEN_PHASES_V,
)
from custom_components.spotnav.core.electric import (
    DEFAULT_SAFETY_MARGIN_A,
    PHASE_V,
    TN_VOLTS_BETWEEN_PHASES,
    fuse_kw,
    phase_volts,
    power_kw,
    site_fuse_kw,
    stored_safety_margin_a,
    stored_voltage_between_phases_v,
    three_phase_kw,
)

TN = {CONF_MAIN_FUSE_A: 20, CONF_VOLTAGE_BETWEEN_PHASES_V: 400}
IT = {CONF_MAIN_FUSE_A: 20, CONF_VOLTAGE_BETWEEN_PHASES_V: 230}


def test_tn_three_by_twenty_amps_keeps_its_figures() -> None:
    # sqrt(3) x 400 V x 20 A, what the planner, the battery gate and the plan's import cap already counted.
    assert three_phase_kw(20, 400) == pytest.approx(13.8564, abs=1e-4)
    assert power_kw(20, 3) == pytest.approx(13.8564, abs=1e-4)
    assert power_kw(16, 3, 400.0) == pytest.approx(11.0851, abs=1e-4)
    assert fuse_kw(20.0, 400.0, 2.0) == pytest.approx(12.4708, abs=1e-4)
    assert site_fuse_kw(TN) == pytest.approx(13.8564, abs=1e-4)
    assert phase_volts(400.0) == pytest.approx(230.94, abs=1e-2)
    assert TN_VOLTS_BETWEEN_PHASES == 400.0


def test_it_network_counts_398_watts_per_ampere_not_690() -> None:
    assert three_phase_kw(1, 230) * 1000 == pytest.approx(398.37, abs=1e-2)
    assert power_kw(20, 3, 230.0) == pytest.approx(7.9674, abs=1e-4)
    assert site_fuse_kw(IT) == pytest.approx(7.9674, abs=1e-4)
    # The old shortcut (20 x 3 x 230 = 13.8 kW) is 73 % above it.
    assert 13.8 / site_fuse_kw(IT) == pytest.approx(math.sqrt(3), rel=1e-6)  # type: ignore[operator]
    assert phase_volts(230.0) == pytest.approx(132.79, abs=1e-2)


@pytest.mark.parametrize("volts", [400.0, 230.0])
def test_single_phase_is_230_volts_on_either_network(volts: float) -> None:
    assert power_kw(16, 1, volts) == pytest.approx(16 * PHASE_V / 1000)
    assert PHASE_V == 230.0


@pytest.mark.parametrize("fuse", [None, 0, -5, True, "20", math.nan, math.inf])
def test_no_fuse_is_no_fuse_power(fuse: object) -> None:
    assert fuse_kw(fuse, 400.0) is None  # type: ignore[arg-type]
    assert site_fuse_kw({CONF_MAIN_FUSE_A: fuse}) is None
    assert site_fuse_kw(None) is None


def test_margin_above_the_fuse_is_no_power() -> None:
    assert fuse_kw(10.0, 400.0, 12.0) == 0.0


def test_unknown_voltage_is_tn() -> None:
    assert stored_voltage_between_phases_v(None) == 400.0
    assert stored_voltage_between_phases_v({CONF_VOLTAGE_BETWEEN_PHASES_V: 380}) == 400.0
    assert stored_voltage_between_phases_v({CONF_VOLTAGE_BETWEEN_PHASES_V: True}) == 400.0
    assert stored_voltage_between_phases_v({CONF_VOLTAGE_BETWEEN_PHASES_V: 230}) == 230.0


def test_missing_safety_margin_is_the_forms_default() -> None:
    assert DEFAULT_SAFETY_MARGIN_A == 1.0
    assert stored_safety_margin_a(None) == 1.0
    assert stored_safety_margin_a({}) == 1.0
    for broken in (None, True, "2", -1.0, math.nan, math.inf):
        assert stored_safety_margin_a({CONF_SAFETY_MARGIN_A: broken}) == 1.0
    assert stored_safety_margin_a({CONF_SAFETY_MARGIN_A: 0}) == 0.0
    assert stored_safety_margin_a({CONF_SAFETY_MARGIN_A: 2.5}) == 2.5


# --- the sites that used a "fuse x 3 x 230" shortcut ----------------------------------------------------------------


def _site(data: dict) -> SimpleNamespace:
    return SimpleNamespace(config=data)


@pytest.mark.parametrize(
    ("data", "kw"),
    [(TN, 13.8564), (IT, 7.9674), ({CONF_MAIN_FUSE_A: 20}, 13.8564), ({}, None)],
    ids=["tn", "it", "no_voltage_is_tn", "no_fuse"],
)
def test_heat_cap_and_house_card_fuse_follow_the_network(data: dict, kw: float | None) -> None:
    from custom_components.spotnav.api.house import fuse_kw as house_fuse_kw
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    for found in (SiteCapacityController._main_fuse_kw(_site(data)), house_fuse_kw(_site(data))):  # type: ignore[arg-type]
        assert found == (None if kw is None else pytest.approx(kw, abs=1e-4))


@pytest.mark.parametrize(
    ("data", "watts"), [(TN, 13856.4), (IT, 7967.4), ({}, math.inf)], ids=["tn", "it", "no_fuse"]
)
def test_import_target_ceiling_follows_the_network(data: dict, watts: float) -> None:
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    assert SiteCapacityController._import_ceiling_w(_site(data)) == pytest.approx(watts, abs=0.1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("data", "phases", "kw"),
    [(TN, 3, 13.8564), (IT, 3, 7.9674), (TN, 1, 4.6), (IT, 1, 4.6)],
    ids=["tn_three_phase", "it_three_phase", "tn_single_phase", "it_single_phase"],
)
def test_battery_plan_car_windows_follow_the_network(
    monkeypatch: pytest.MonkeyPatch, data: dict, phases: int, kw: float
) -> None:
    from custom_components.spotnav import runtime
    from custom_components.spotnav.battery.plan_runtime import site_car_windows
    from custom_components.spotnav.const import CONF_CHARGER_ENTRY_IDS

    start = datetime(2026, 11, 19, 1, tzinfo=UTC)
    windows = [(start, start + timedelta(hours=2))]
    plans = {
        "unsized": SimpleNamespace(power_kw=None, amps=20, phases=phases, windows=windows),
        "sized": SimpleNamespace(power_kw=5.5, amps=20, phases=phases, windows=windows),
    }
    monkeypatch.setattr(runtime, "controller_for", lambda _hass, entry_id: SimpleNamespace(plan=plans[entry_id]))
    site = _site({**data, CONF_CHARGER_ENTRY_IDS: ["unsized", "sized"]})
    found = site_car_windows(None, site)  # type: ignore[arg-type]
    # A plan that knows its power keeps it; one that does not is counted at its current on the site's network.
    assert [round(k, 4) for _, _, k in found] == [pytest.approx(kw, abs=1e-4), 5.5]
