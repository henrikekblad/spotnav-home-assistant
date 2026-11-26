"""A single-phase site: its meter measures L1 alone, its fuse is one phase at the stated amps (never 3 x the fuse), and
every charger on it is a single-phase one on L1 (`core.electric.site_phase_count`, `site_capacity.on_site_phases`)."""

from __future__ import annotations

import math
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.control.caps import Caps
from custom_components.spotnav.const import (
    CONF_DERIVED_ENTITIES,
    CONF_DIRECT_ENTITIES,
    CONF_MAIN_FUSE_A,
    CONF_MEASUREMENT_MODE,
    CONF_SITE_CURRENT_SOURCE,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
    MEASUREMENT_MODE_DIRECT,
)
from custom_components.spotnav.core.electric import PHASE_V, site_fuse_kw, site_phase_count
from custom_components.spotnav.site.site_capacity import (
    PHASES,
    SINGLE_PHASE,
    ChargerRequest,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
    calculate_site_capacity,
)
from custom_components.spotnav.site.site_detection import DEAD_PHASE_HOLD_S, MeterCandidate, single_phase_candidate

from .helpers import make_site_entry, set_current_sensor
from .messages import field, get_message, register, update_entity_config_message
from .world import admin, controller_of, setup_charger_and_site, ws_call


def _direct(**l1: float) -> dict:
    return {CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT, CONF_MAIN_FUSE_A: 100, **l1}


def test_phase_count_is_read_from_the_meter_and_unknown_stays_three() -> None:
    assert site_phase_count({**_direct(), CONF_DIRECT_ENTITIES: {"L1": "sensor.l1"}}) == 1
    # An empty L2/L3 left by a cleared picker is no phase.
    assert site_phase_count({**_direct(), CONF_DIRECT_ENTITIES: {"L1": "sensor.l1", "L2": "", "L3": ""}}) == 1
    assert site_phase_count({**_direct(), CONF_DIRECT_ENTITIES: {"L1": "a", "L2": "b", "L3": "c"}}) == 3
    # Two phases is a three-phase meter missing one: still three, so the missing phase is reported.
    assert site_phase_count({**_direct(), CONF_DIRECT_ENTITIES: {"L1": "a", "L2": "b"}}) == 3
    source = {"kind": "attributes", "entity_id": "sensor.meter", "attributes": {"L1": "current_l1"}}
    assert site_phase_count({**_direct(), CONF_SITE_CURRENT_SOURCE: source}) == 1
    derived = {CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DERIVED, CONF_DERIVED_ENTITIES: {"L1": {"power": "sensor.p"}}}
    assert site_phase_count(derived) == 1
    # Phases unknown (no per-phase reading at all) keep today's three.
    assert site_phase_count({**_direct(), CONF_DIRECT_ENTITIES: {}}) == 3
    assert site_phase_count(None) == 3


def test_a_single_phase_fuse_is_one_phase_never_three() -> None:
    single = {**_direct(), CONF_DIRECT_ENTITIES: {"L1": "sensor.l1"}}
    assert math.isclose(site_fuse_kw(single), 100 * PHASE_V / 1000.0)
    three = {**_direct(), CONF_DIRECT_ENTITIES: {"L1": "a", "L2": "b", "L3": "c"}}
    assert math.isclose(site_fuse_kw(three), math.sqrt(3.0) * 400 * 100 / 1000.0)


def _value(amps: float | None) -> PhaseValue:
    return PhaseValue(amps, 1.0) if amps is not None else PhaseValue(None, None, problem="missing")


def test_load_balancing_on_one_phase_at_100_a() -> None:
    """L1 alone is read; L2 and L3 are no phase (not missing); a three-phase charger is reckoned on L1."""
    config = SiteCalculationConfig(
        main_fuse_a=100.0,
        safety_margin_a=2.0,
        measurement_mode="direct_phase_current",
        max_age_s=60.0,
        direct=DirectPhaseMeasurement(l1=_value(70.0), l2=_value(None), l3=_value(None)),
        phases=SINGLE_PHASE,
    )
    measured = DirectPhaseMeasurement(l1=_value(16.0), l2=_value(16.0), l3=_value(16.0))
    request = ChargerRequest("car", requested_current_a=32.0, phases=3, measured_current_a=measured)
    result = calculate_site_capacity(config, [request])
    assert result.state == "observing"
    assert result.measured_margin_a == {"L1": 28.0, "L2": None, "L3": None}
    assert result.limiting_phase == "L1"
    (allocation,) = result.allocations
    assert allocation.proposed_current_a == 28.0 and allocation.limiting_phase == "L1"
    assert result.site_phases == SINGLE_PHASE

    # The same readings on a three-phase site: L2 and L3 missing make the site unusable, as before.
    three = calculate_site_capacity(
        SiteCalculationConfig(
            main_fuse_a=100.0, safety_margin_a=2.0, measurement_mode="direct_phase_current", max_age_s=60.0,
            direct=config.direct,
        ),
        [request],
    )
    assert three.state == "missing_measurements" and three.site_phases == PHASES


def test_battery_grid_charge_cap_on_one_phase() -> None:
    """The HA-down cap on a single-phase site is the one phase's room: a three-phase plant is capped to the site."""
    one = gate.grid_charge_cap_kw(
        100.0, 400.0, margin_a=2.0, cars_a=10.0, house_peak_kw=2.0, plant_phases=1,
    )
    house_a = gate.HOUSE_PEAK_DEFAULT_KW * 1000.0 / (400.0 / math.sqrt(3.0))
    assert one is not None
    assert math.isclose(one, (100.0 - 2.0 - 10.0 - house_a) * (400.0 / math.sqrt(3.0)) / 1000.0)
    three = gate.grid_charge_cap_kw(
        100.0, 400.0, margin_a=2.0, cars_a=10.0, house_peak_kw=2.0, plant_phases=3,
    )
    assert three is not None and math.isclose(three, 3 * one)

    control = SimpleNamespace(output_type=None, output_phases=None, grid_charge_phases=3)
    site = SimpleNamespace(config={}, site_phases=SINGLE_PHASE)
    shell = SimpleNamespace(hass=None, site=site)
    assert Caps._plant_phases(shell, control) == 1  # type: ignore[arg-type]  # noqa: SLF001
    site.site_phases = PHASES
    assert Caps._plant_phases(shell, control) == 3  # type: ignore[arg-type]  # noqa: SLF001


async def test_controller_runs_a_single_phase_site_and_puts_every_charger_on_l1(hass: HomeAssistant) -> None:
    entry = make_site_entry(
        hass,
        entry_id="single",
        main_fuse_a=100.0,
        direct_entities={"L1": "sensor.single_l1"},
        charger_entry_ids=["car"],
        phase_wiring={"car": {"phases": 3, "phase": None}},
    )
    set_current_sensor(hass, "sensor.single_l1", 40)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, entry.entry_id)
    assert site.site_phases == SINGLE_PHASE
    assert site.result.state == "observing"
    assert site.result.measured_margin_a == {"L1": 59.0, "L2": None, "L3": None}
    assert site.reckoned_phases("car") == (1, "L1")
    assert math.isclose(site._main_fuse_kw(), 100 * PHASE_V / 1000.0)  # noqa: SLF001


async def test_config_flow_saves_a_single_phase_site_with_l1_only(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    set_current_sensor(hass, "sensor.grid_l1", 0)
    set_current_sensor(hass, "sensor.grid_l2", 0)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            CONF_MAIN_FUSE_A: 100,
            "safety_margin_a": 2,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "manual"})
    assert result["step_id"] == "site_details"

    # L2 without L3 is neither a single-phase nor a three-phase meter.
    refused = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"direct_L1": "sensor.grid_l1", "direct_L2": "sensor.grid_l2"}
    )
    assert refused["type"] is FlowResultType.FORM and refused["errors"] == {"direct_L3": "phases_incomplete"}

    result = await hass.config_entries.flow.async_configure(refused["flow_id"], {"direct_L1": "sensor.grid_l1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DIRECT_ENTITIES] == {"L1": "sensor.grid_l1"}
    assert site_phase_count(result["data"]) == 1


async def test_api_update_entity_config_accepts_a_single_phase_site(hass: HomeAssistant, hass_ws_client) -> None:
    charger, site = await setup_charger_and_site(hass)
    assert site is not None
    client = await admin(hass, hass_ws_client)

    got = (await ws_call(client, get_message(charger.entry_id)))["result"]
    assert field(got, "direct_L1")["required"] is True
    assert field(got, "direct_L2")["required"] is False and field(got, "direct_L3")["required"] is False

    half = (
        await ws_call(
            client, update_entity_config_message(charger.entry_id, scope="site", changes={"direct_L2": ""})
        )
    )["result"]
    assert half["ok"] is False
    assert {(item["field"], item["code"]) for item in half["field_errors"]} == {("direct_L2", "required")}

    l1 = register(hass, "sensor", "only_l1", "Only L1", device_class="current")
    result = (
        await ws_call(
            client,
            update_entity_config_message(
                charger.entry_id, scope="site", changes={"direct_L1": l1, "direct_L2": "", "direct_L3": ""}
            ),
        )
    )["result"]
    assert result["ok"] is True, result
    data = hass.config_entries.async_get_entry(site.entry_id).data
    assert data[CONF_DIRECT_ENTITIES] == {"L1": l1}
    assert site_phase_count(data) == 1


def _victron(hass: HomeAssistant, *, l2_v: str, l3_v: str, l2_p: str = "0", l3_p: str = "0") -> MeterCandidate:
    for phase, volts, watts in (("l1", "231", "1200"), ("l2", l2_v, l2_p), ("l3", l3_v, l3_p)):
        hass.states.async_set(f"sensor.grid_{phase}_power", watts, {"unit_of_measurement": "W"})
        hass.states.async_set(f"sensor.grid_{phase}_voltage", volts, {"unit_of_measurement": "V"})
    return MeterCandidate(
        candidate_id="victron:gx",
        integration="victron",
        title="GX",
        mode=MEASUREMENT_MODE_DERIVED,
        confidence="high",
        derived_entities={
            phase: {"power": f"sensor.grid_{phase.lower()}_power", "voltage": f"sensor.grid_{phase.lower()}_voltage"}
            for phase in PHASES
        },
    )


async def test_detection_reads_a_gx_without_l2_and_l3_supply_as_single_phase(hass: HomeAssistant) -> None:
    """hass-victron makes L2 and L3 entities on a single-phase GX: unknown, unavailable or 0 V is no phase."""
    for l2_v, l3_v in (("unknown", "unavailable"), ("0", "0.4"), ("unavailable", "0")):
        candidate = single_phase_candidate(hass, _victron(hass, l2_v=l2_v, l3_v=l3_v))
        # A phase that reads nothing at all, voltage and power alike, is no phase either.
        if l2_v == "unknown":
            hass.states.async_set("sensor.grid_l2_power", "unknown")
            hass.states.async_set("sensor.grid_l3_power", "unavailable")
            candidate = single_phase_candidate(hass, _victron(hass, l2_v=l2_v, l3_v=l3_v, l2_p="unknown", l3_p="unavailable"))
        assert candidate.derived_entities is not None and set(candidate.derived_entities) == {"L1"}, (l2_v, l3_v)

    # A three-phase house idling L2 and L3 at 0 W with their voltage live stays three-phase.
    idle = single_phase_candidate(hass, _victron(hass, l2_v="229", l3_v="230"))
    assert idle.derived_entities is not None and set(idle.derived_entities) == set(PHASES)
    # One phase without supply is a fault, not a single-phase meter.
    one = single_phase_candidate(hass, _victron(hass, l2_v="0", l3_v="230"))
    assert one.derived_entities is not None and set(one.derived_entities) == set(PHASES)


def _gx_states(hass: HomeAssistant, readings: dict[str, tuple[str, str]]) -> None:
    for phase, (volts, watts) in readings.items():
        hass.states.async_set(f"sensor.gx_{phase}_power", watts, {"unit_of_measurement": "W"})
        hass.states.async_set(f"sensor.gx_{phase}_voltage", volts, {"unit_of_measurement": "V"})


_GX_DERIVED = {
    phase: {"power": f"sensor.gx_{phase.lower()}_power", "voltage": f"sensor.gx_{phase.lower()}_voltage"}
    for phase in PHASES
}
_GX_DEAD = {"l1": ("231", "2300"), "l2": ("0", "0"), "l3": ("0", "0")}


async def _gx_site(hass: HomeAssistant, readings: dict[str, tuple[str, str]], **entry: object):
    _gx_states(hass, readings)
    site_entry = make_site_entry(
        hass, entry_id="gx", main_fuse_a=100.0, measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=_GX_DERIVED, **entry,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site = controller_of(hass, site_entry.entry_id)
    clock = {"now": 1000.0}
    site._yield_now = lambda: clock["now"]  # noqa: SLF001
    site._dead_phases_since = None  # noqa: SLF001 - the hold starts on the test's clock
    return site, clock


async def test_a_derived_site_whose_l2_and_l3_read_0_v_runs_on_l1_after_the_hold(hass: HomeAssistant) -> None:
    """Configured with all three phases, a GX whose L2 and L3 have no supply (0 V, nothing drawn) is a single-phase
    site once they have read so without a break for the hold; until then, and with an unknown L2, the site keeps its
    three phases and is unusable for the ones it cannot read."""
    site, clock = await _gx_site(hass, _GX_DEAD)
    assert site.site_phases == PHASES
    assert site.result.state != "observing"

    clock["now"] += DEAD_PHASE_HOLD_S - 1
    assert site.site_phases == PHASES
    clock["now"] += 2
    assert site.site_phases == SINGLE_PHASE
    _gx_states(hass, _GX_DEAD)
    await hass.async_block_till_done()
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.state == "observing"
    assert site.result.measured_margin_a["L2"] is None

    # Unknown is no evidence: three phases while it lasts, and L1 alone at once when the 0 V is read again.
    hass.states.async_set("sensor.gx_l2_voltage", "unknown", {"unit_of_measurement": "V"})
    assert site.site_phases == PHASES
    hass.states.async_set("sensor.gx_l2_voltage", "0", {"unit_of_measurement": "V"})
    assert site.site_phases == SINGLE_PHASE


async def test_a_three_phase_meter_reading_0_for_a_moment_stays_three_phase(hass: HomeAssistant) -> None:
    """A three-phase meter whose L2 and L3 read 0 V and 0 W for a while (a failed poll written as 0) never makes the
    site single-phase: a break in the evidence starts the hold again, and a phase once seen live stays a phase."""
    site, clock = await _gx_site(hass, _GX_DEAD)
    clock["now"] += DEAD_PHASE_HOLD_S - 1
    assert site.site_phases == PHASES
    # The meter comes back before the hold is over: the phases are live.
    _gx_states(hass, {"l2": ("229", "400"), "l3": ("230", "0")})
    assert site.site_phases == PHASES
    # It reads 0 again, for however long: the site had three phases and has them still.
    _gx_states(hass, _GX_DEAD)
    clock["now"] += 10 * DEAD_PHASE_HOLD_S
    assert site.site_phases == PHASES
    site._recompute(schedule_apply=False)  # noqa: SLF001
    assert site.result.state != "observing"


async def test_a_charger_drawing_on_several_phases_keeps_the_site_three_phase(hass: HomeAssistant) -> None:
    """A charger that reads a current on more than one phase shows the supply has them, whatever the meter reads."""
    source = {
        "kind": "separate_entities",
        "entity_ids": {phase: f"sensor.car_{phase.lower()}" for phase in PHASES},
    }
    site, clock = await _gx_site(
        hass, _GX_DEAD, charger_entry_ids=["car"],
        phase_wiring={"car": {"phases": 3, "phase": None, "measured_current_source": source}},
    )
    assert site.site_phases == PHASES  # the hold starts
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.car_{phase}", 10)
    clock["now"] += DEAD_PHASE_HOLD_S + 1
    assert site.site_phases == PHASES
    assert site._other_phase_seen_live is True  # noqa: SLF001
    for phase in ("l2", "l3"):
        set_current_sensor(hass, f"sensor.car_{phase}", 0)
    clock["now"] += DEAD_PHASE_HOLD_S + 1
    assert site.site_phases == PHASES
