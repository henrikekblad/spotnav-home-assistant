"""Solar surplus between chargers on one site follows the charger priority.

The surplus is offered to the site's chargers in their order (First, Normal, Last; ties by the order
they joined): a later charger gets only what the earlier ones cannot use, because it is too little for
their minimum or more than they take. The pure split (`share_surplus`, `priority_adjust_w`) is tested
directly; the wiring with two real chargers on one derived-mode site, driven tick by tick with fake
clocks as `tests/test_solar_execution.py` does.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGER_PRIORITY,
    CONF_MEASURED_CURRENT_SOURCE,
    MEASUREMENT_MODE_DERIVED,
)
from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.site.measurement_source import PhaseMeasurementSource, source_to_dict
from custom_components.spotnav.site.site_capacity import PHASES
from custom_components.spotnav.site.solar_surplus import (
    SolarConfig,
    SolarController,
    SolarObservation,
    SolarShareMember,
    priority_adjust_w,
    share_surplus,
)

from .helpers import make_entry, make_site_entry
from .world import (
    SecondsClock,
    controller_of,
    set_charger_delivered_a,
    set_derived_site_entities,
    set_site_power_w,
    tick_site,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

V = 230.0
THREE_PHASE_W_PER_A = 3 * V  # 690 W per amp on three phases


def member(
    charger_entry_id: str,
    *,
    priority: str = "normal",
    order: int = 0,
    car_w: float = 0.0,
    running: bool = False,
    takes_less: bool = False,
    max_current_a: float = 16.0,
) -> SolarShareMember:
    return SolarShareMember(
        charger_entry_id=charger_entry_id,
        priority=priority,
        order=order,
        car_w=car_w,
        watts_per_a=THREE_PHASE_W_PER_A,
        running=running,
        start_a=6.0,
        stop_a=5.0,
        min_current_a=6.0,
        max_current_a=max_current_a,
        takes_less=takes_less,
    )


# ------------------------------------------------------------------------------- the pure split


def test_first_is_offered_everything_and_a_later_charger_only_what_is_left() -> None:
    """15 kW of surplus: the First charger can take 16 A (11 040 W); the Normal one is offered the rest."""
    offered = share_surplus(
        15_000.0,
        [member("normal", priority="normal", order=0), member("first", priority="first", order=1)],
    )
    assert offered["first"] == 15_000.0
    assert offered["normal"] == pytest.approx(15_000.0 - 16 * THREE_PHASE_W_PER_A)


def test_surplus_too_small_for_the_first_charger_goes_on_to_the_next() -> None:
    """Below the First charger's start minimum it cannot use the surplus, so the next one is offered it
    (a single-phase Last charger can start on 2 kW where a three-phase one cannot)."""
    single_phase_last = replace(member("last", priority="last", order=1), watts_per_a=V)
    offered = share_surplus(2_000.0, [member("first", priority="first", order=0), single_phase_last])
    assert offered["last"] == 2_000.0


def test_a_charger_whose_car_takes_less_passes_the_rest_on() -> None:
    """A running First charger whose car draws 3 kW of the 11 kW it is offered uses only that 3 kW."""
    offered = share_surplus(
        11_000.0,
        [
            member("first", priority="first", car_w=3_000.0, running=True, takes_less=True),
            member("last", priority="last", order=1),
        ],
    )
    assert offered["last"] == 8_000.0


def test_a_running_charger_below_its_stop_keeps_its_draw_until_it_stops() -> None:
    """A First charger on its way to a stop still draws its 6 A; the next one is not offered that."""
    offered = share_surplus(
        3_000.0,
        [
            member("first", priority="first", car_w=6 * THREE_PHASE_W_PER_A, running=True),
            member("last", priority="last", order=1),
        ],
    )
    assert offered["last"] == pytest.approx(3_000.0 - 6 * THREE_PHASE_W_PER_A)


def test_ties_go_by_the_order_the_chargers_joined() -> None:
    offered = share_surplus(5_000.0, [member("second", order=1), member("joined_first", order=0)])
    assert offered["joined_first"] == 5_000.0
    assert offered["second"] == 0.0


def test_priority_adjust_moves_a_lower_chargers_draw_to_the_first_one() -> None:
    """A Last charger draws 6 A (4 140 W) and 1 kW is exported. Alone, the First charger reckons it
    has 1 kW; the split offers it the Last charger's draw as well, and takes the same from the Last."""
    last_draw = 6 * THREE_PHASE_W_PER_A
    members = [
        member("first", priority="first", order=1),
        member("last", priority="last", order=0, car_w=last_draw, running=True),
    ]
    # Each reckons alone: its own draw plus the 1 kW export.
    assert priority_adjust_w("first", 1_000.0, members) == pytest.approx(last_draw)
    first_offered = 1_000.0 + last_draw
    assert priority_adjust_w("last", last_draw + 1_000.0, members) == pytest.approx(-first_offered)


def test_alone_or_left_out_nothing_moves() -> None:
    assert priority_adjust_w("only", 4_000.0, [member("only")]) == 0.0
    assert priority_adjust_w("absent", 4_000.0, [member("other")]) == 0.0


def test_the_controller_reckons_with_the_adjustment() -> None:
    """`share_adjust_w` is added to the charger's own surplus before it is turned into amps."""
    observation = SolarObservation(
        now=0.0,
        signed_grid_w={phase: -1_400.0 for phase in PHASES},
        voltage_v={phase: V for phase in PHASES},
        car_delivered_a={phase: 0.0 for phase in PHASES},
        battery_w=None,
        car_phases=PHASES,
        share_adjust_w=-4_200.0,
    )
    verdict = SolarController(SolarConfig()).observe(observation)
    assert verdict.available_w == pytest.approx(0.0)
    assert verdict.reason == "off_no_surplus"


# ----------------------------------------------------------------------- two chargers on a site


async def _two_charger_site(
    hass: HomeAssistant, *, priority_a: str | None, priority_b: str | None
) -> tuple[object, dict[str, SolarExecutionCoordinator], list[SecondsClock], list]:
    """Chargers `a` and `b` (joined in that order), both on `solar`, on one derived-mode site.
    Returns the site controller, the coordinators by charger, their clocks and the turn-on calls."""
    coordinators: dict[str, SolarExecutionCoordinator] = {}
    clocks: list[SecondsClock] = []
    entries = {}
    for name, priority in (("a", priority_a), ("b", priority_b)):
        prefix = f"{name}_charger"
        hass.states.async_set(f"switch.{prefix}", "off")
        entry = make_entry(
            hass,
            entry_id=prefix,
            charge_control=f"switch.{prefix}",
            current_limit=None,
            webhook_id=f"webhook-{name}",
            title=f"Charger {name}",
            extra={CONF_CHARGER_PRIORITY: priority} if priority else None,
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries[name] = entry

    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")

    store = domain_data(hass).auto_store
    assert store is not None
    for name, entry in entries.items():
        await store.async_update(entry.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_SOLAR))
        clock = SecondsClock(0.0)
        coordinator = hass.config_entries.async_get_entry(entry.entry_id).runtime_data.solar
        assert isinstance(coordinator, SolarExecutionCoordinator)
        coordinator._now = clock.now
        coordinators[name] = coordinator
        clocks.append(clock)
        set_charger_delivered_a(hass, f"{name}_charger", 0.0)

    derived_entities = set_derived_site_entities(hass, "pair_site", 0.0)
    site_entry = make_site_entry(
        hass,
        entry_id="pair_site",
        main_fuse_a=63.0,
        charger_entry_ids=[entries["a"].entry_id, entries["b"].entry_id],
        phase_wiring={
            entry.entry_id: {
                "phases": 3,
                "phase": None,
                "min_current_a": 6.0,
                CONF_MEASURED_CURRENT_SOURCE: source_to_dict(
                    PhaseMeasurementSource(
                        kind="separate_entities",
                        entity_ids={p: f"sensor.{name}_charger_{p.lower()}" for p in PHASES},
                    )
                ),
            }
            for name, entry in entries.items()
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site_controller = controller_of(hass, site_entry.entry_id)
    if site_controller._timer_cancel is not None:
        site_controller._timer_cancel()
        site_controller._timer_cancel = None
    if site_controller._state_listener_cancel is not None:
        site_controller._state_listener_cancel()
        site_controller._state_listener_cancel = None
    turn_on_calls.clear()
    return site_controller, coordinators, clocks, turn_on_calls


async def _tick_at(hass: HomeAssistant, site_controller, clocks: list[SecondsClock], at: float) -> None:
    for clock in clocks:
        clock.value = at
    await tick_site(hass, site_controller)


async def test_surplus_for_one_charger_starts_only_the_first_priority_one(hass: HomeAssistant) -> None:
    """4.2 kW of export is enough for one charger at 6 A. Charger `b` (joined second) is First: it
    arms and starts; `a` is offered nothing and never arms. Without the split both would start."""
    site_controller, coordinators, clocks, turn_on_calls = await _two_charger_site(
        hass, priority_a=None, priority_b="first"
    )
    set_site_power_w(hass, "pair_site", -1_400.0)
    await _tick_at(hass, site_controller, clocks, 0.0)
    assert coordinators["b"].state.state == "arming"
    assert coordinators["a"].state.state == "off"

    await _tick_at(hass, site_controller, clocks, 125.0)
    # The start itself makes the site recompute once more, so the latest verdict is already "hold".
    assert coordinators["b"].state.state == "on"
    assert coordinators["a"].state.state == "off"
    assert [call.data["entity_id"] for call in turn_on_calls] == ["switch.b_charger"]

    # `b` draws its 6 A and the export shrinks by as much: `a` is still offered nothing.
    hass.states.async_set("switch.b_charger", "on")
    set_charger_delivered_a(hass, "b_charger", 6.0)
    set_site_power_w(hass, "pair_site", -20.0)
    await _tick_at(hass, site_controller, clocks, 400.0)
    assert coordinators["b"].state.state == "on"
    assert coordinators["a"].state.state == "off"
    assert coordinators["a"].state.available_w == pytest.approx(0.0)


async def test_a_later_charger_gets_only_what_the_first_cannot_take(hass: HomeAssistant) -> None:
    """15 kW: `a` (First) can take 16 A, 11 040 W, leaving 3 960 W, too little for `b` to start on.
    18 kW leaves 6 960 W, and `b` arms."""
    site_controller, coordinators, clocks, _turn_on_calls = await _two_charger_site(
        hass, priority_a="first", priority_b="last"
    )
    set_site_power_w(hass, "pair_site", -5_000.0)
    await _tick_at(hass, site_controller, clocks, 0.0)
    assert coordinators["a"].state.state == "arming"
    assert coordinators["b"].state.state == "off"
    assert coordinators["b"].state.available_w == pytest.approx(15_000.0 - 16 * THREE_PHASE_W_PER_A)

    set_site_power_w(hass, "pair_site", -6_000.0)
    await _tick_at(hass, site_controller, clocks, 10.0)
    assert coordinators["a"].state.state == "arming"
    assert coordinators["b"].state.state == "arming"


async def test_equal_priorities_go_by_the_order_the_chargers_joined(hass: HomeAssistant) -> None:
    site_controller, coordinators, clocks, turn_on_calls = await _two_charger_site(
        hass, priority_a=None, priority_b=None
    )
    set_site_power_w(hass, "pair_site", -1_400.0)
    await _tick_at(hass, site_controller, clocks, 0.0)
    await _tick_at(hass, site_controller, clocks, 125.0)
    assert coordinators["a"].state.state == "on"
    assert coordinators["b"].state.state == "off"
    assert [call.data["entity_id"] for call in turn_on_calls] == ["switch.a_charger"]


async def test_a_charger_off_solar_is_house_load_to_the_others(hass: HomeAssistant) -> None:
    """With `b` (First) moved off `solar`, it takes no part: `a` is offered the whole surplus."""
    site_controller, coordinators, clocks, _turn_on_calls = await _two_charger_site(
        hass, priority_a="last", priority_b="first"
    )
    store = domain_data(hass).auto_store
    await store.async_update("b_charger", mutate=lambda s: replace(s, strategy="cheapest"))
    set_site_power_w(hass, "pair_site", -1_400.0)
    await _tick_at(hass, site_controller, clocks, 0.0)
    assert coordinators["a"].state.state == "arming"
    assert coordinators["a"].state.available_w == pytest.approx(4_200.0)
