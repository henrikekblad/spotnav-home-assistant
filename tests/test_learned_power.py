"""The power a car really draws, learned from the plan's clean minutes, and planned with once trusted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.learned_power import (
    learned_from,
    LearnedPowerStore,
    MAX_SESSIONS,
    parse_stored,
    PowerSample,
    PowerSampler,
    Session,
    subject_for,
    SUBJECT_CHARGER,
    Wiring,
)
from custom_components.spotnav.planning.planner import energy_per_slot_kwh, power_kw, slots_needed
from custom_components.spotnav.runtime import preview_for

from .relay import serve
from .test_dashboard_api import NOW
from .world import REGISTER, charger_and_car

PLANNED_KW = power_kw(16, 3)
LOW_KW = power_kw(6, 3)
WIRING = Wiring(phases=3, voltage_v=400.0, amps=16)
ATTRIBUTES = {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"}


def _sessions(*pairs: tuple[float, float]) -> list[Session]:
    return [Session(id=f"s{index}", minutes=minutes, kwh=kwh) for index, (minutes, kwh) in enumerate(pairs)]


# ------------------------------------------------------------------ the rule


def test_not_trusted_before_twenty_minutes_over_two_plug_ins() -> None:
    one = learned_from("charger", _sessions((60, 7.0)), LOW_KW, PLANNED_KW)
    assert one.kw == pytest.approx(7.0) and not one.trusted and one.cap_kw is None
    short = learned_from("charger", _sessions((8, 1.0), (8, 1.0)), LOW_KW, PLANNED_KW)
    assert not short.trusted, "16 minutes is too few"
    two = learned_from("charger", _sessions((10, 7.0 / 6), (10, 7.0 / 6)), LOW_KW, PLANNED_KW)
    assert two.trusted and two.cap_kw == pytest.approx(7.0)


def test_a_plug_in_of_a_few_minutes_gives_no_power_of_its_own() -> None:
    learned = learned_from("charger", _sessions((3, 1.0), (30, 3.5)), LOW_KW, PLANNED_KW)
    assert learned.sessions == 1 and learned.kw == pytest.approx(7.0) and not learned.trusted


def test_the_median_of_the_plug_ins() -> None:
    learned = learned_from("charger", _sessions((30, 3.5), (30, 3.6), (30, 1.0)), LOW_KW, PLANNED_KW)
    assert learned.kw == pytest.approx(7.0)


def test_bounded_to_the_least_power_and_the_planned_power() -> None:
    above = learned_from("charger", _sessions((30, 10.0), (30, 10.0)), LOW_KW, PLANNED_KW)
    assert above.kw == pytest.approx(PLANNED_KW)
    below = learned_from("charger", _sessions((30, 0.1), (30, 0.1)), LOW_KW, PLANNED_KW)
    assert below.kw == pytest.approx(LOW_KW)


def test_per_vehicle_else_per_charger() -> None:
    assert subject_for("abc") == "vehicle:abc"
    assert subject_for(None) == SUBJECT_CHARGER == subject_for("")


async def test_each_subject_learns_alone_and_a_wiring_change_starts_again(hass: HomeAssistant) -> None:
    store = LearnedPowerStore(hass, "entry")
    for session in ("a", "b"):
        store.add("vehicle:one", WIRING, session, 3.5, 30)
    assert store.power("vehicle:one", WIRING, LOW_KW, PLANNED_KW).cap_kw == pytest.approx(7.0)
    assert store.power("vehicle:two", WIRING, LOW_KW, PLANNED_KW).kw is None
    assert store.power(SUBJECT_CHARGER, WIRING, LOW_KW, PLANNED_KW).kw is None
    one_phase = Wiring(phases=1, voltage_v=400.0, amps=16)
    assert store.power("vehicle:one", one_phase, LOW_KW, PLANNED_KW).kw is None, "learned on three phases"
    store.add("vehicle:one", one_phase, "c", 1.8, 30)
    assert store.power("vehicle:one", WIRING, LOW_KW, PLANNED_KW).kw is None, "the three-phase value is gone"
    assert store.power("vehicle:one", Wiring(phases=1, voltage_v=230.0, amps=16), LOW_KW, PLANNED_KW).kw is None
    assert store.power("vehicle:one", Wiring(phases=1, voltage_v=400.0, amps=10), LOW_KW, PLANNED_KW).kw is None


async def test_only_the_last_plug_ins_are_kept(hass: HomeAssistant) -> None:
    store = LearnedPowerStore(hass, "entry")
    for index in range(MAX_SESSIONS + 3):
        store.add("charger", WIRING, f"s{index}", 1.0, 10)
    assert store.power("charger", WIRING, 0.1, PLANNED_KW).sessions == MAX_SESSIONS


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"subjects": []},
        {"subjects": {"charger": {"wiring": {"phases": 2, "voltage_v": 400, "amps": 16}, "sessions": []}}},
        {"subjects": {"charger": {"wiring": {"phases": 3, "voltage_v": "x", "amps": 16}, "sessions": []}}},
        {"subjects": {"charger": {"wiring": {"phases": 3, "voltage_v": 400, "amps": True}, "sessions": []}}},
        {"subjects": {"charger": {"wiring": WIRING.as_dict(), "sessions": "no"}}},
        {"subjects": {"charger": {"wiring": WIRING.as_dict(), "sessions": [{"id": "a", "minutes": -1, "kwh": 1}]}}},
        {"subjects": {"charger": {"wiring": WIRING.as_dict(), "sessions": [{"id": "", "minutes": 10, "kwh": 1}]}}},
        {"subjects": {"charger": {"wiring": WIRING.as_dict(), "sessions": [{"id": "a", "minutes": 10, "kwh": "1"}]}}},
        {"subjects": {"charger": {"wiring": WIRING.as_dict(), "sessions": [{"id": "a", "minutes": 1e999, "kwh": 1}]}}},
    ],
)
def test_a_malformed_store_is_ignored(raw: Any) -> None:
    assert parse_stored(raw) == {}


def test_a_well_formed_store_reads_back() -> None:
    stored = {"subjects": {"charger": {"wiring": WIRING.as_dict(), "sessions": [
        {"id": "a", "minutes": 30, "kwh": 3.5}, {"id": "b", "minutes": "bad", "kwh": 1},
    ]}}}
    assert parse_stored(stored) == {"charger": (WIRING, [Session(id="a", minutes=30.0, kwh=3.5)])}


async def test_the_sampler_counts_clean_stretches_only(hass: HomeAssistant) -> None:
    store = LearnedPowerStore(hass, "entry")
    sampler = PowerSampler(store)
    start = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)

    def sample(minute: int, kwh: float, session: str = "p1", wiring: Wiring = WIRING) -> PowerSample:
        return PowerSample(at=start + timedelta(minutes=minute), kwh=kwh, session=session, subject="c", wiring=wiring)

    sampler.sample(sample(0, 10.0), PLANNED_KW)
    sampler.sample(sample(1, 10.1), PLANNED_KW)
    sampler.sample(None, PLANNED_KW)  # held back for a minute: the stretch ends
    sampler.sample(sample(3, 10.5), PLANNED_KW)
    sampler.sample(sample(4, 10.6), PLANNED_KW)
    sampler.sample(sample(20, 12.0), PLANNED_KW)  # a gap far longer than a check: nothing
    sampler.sample(sample(21, 11.0), PLANNED_KW)  # a register that went back: nothing
    sampler.sample(sample(22, 11.1, session="p2"), PLANNED_KW)  # another plug-in: a new stretch
    learned = store.power("c", WIRING, 0.1, PLANNED_KW)
    assert learned.minutes == pytest.approx(2.0)


# ------------------------------------------------------------------ the planner


def test_the_planner_counts_on_the_lower_of_the_two() -> None:
    assert energy_per_slot_kwh(16, 3, 400.0, 7.0) == pytest.approx(7.0 / 4)
    assert energy_per_slot_kwh(16, 3, 400.0, 50.0) == pytest.approx(PLANNED_KW / 4)
    assert slots_needed(10.0, 16, 3, 400.0) == 4
    assert slots_needed(10.0, 16, 3, 400.0, 7.0) == 6



# ------------------------------------------------------------------ in Home Assistant


def _record_switch(hass: HomeAssistant) -> None:
    @callback
    def on_call(event: Any) -> None:
        service = event.data["service"]
        if service not in ("turn_on", "turn_off"):
            return
        targets = event.data.get("service_data", {}).get("entity_id", [])
        for entity_id in [targets] if isinstance(targets, str) else targets:
            hass.states.async_set(entity_id, "on" if service == "turn_on" else "off")

    hass.bus.async_listen("call_service", on_call)


@pytest.mark.usefixtures("offline_relay")
async def test_the_plan_counts_on_learned_power_once_trusted_and_not_before(
    hass: HomeAssistant, transport: Any
) -> None:
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, _ = await charger_and_car(hass, soc_percent="70", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        store = preview._learned  # noqa: SLF001
        first = preview.snapshot().proposal
        assert first.power_kw == pytest.approx(PLANNED_KW)

        store.add(subject_for(device_id), WIRING, "one", PLANNED_KW / 2 * 0.5, 30)
        await preview.async_recalculate()
        assert preview.snapshot().proposal.power_kw == pytest.approx(PLANNED_KW), "one plug-in is not enough"
        facts = preview.plan_watch_facts()["learned_power"]
        assert facts["sessions"] == 1 and not facts["trusted"] and not facts["planned_with"]

        store.add(subject_for(device_id), WIRING, "two", PLANNED_KW / 2 * 0.5, 30)
        await preview.async_recalculate()
        proposal = preview.snapshot().proposal
        assert proposal.power_kw == pytest.approx(PLANNED_KW / 2)
        assert proposal.slots_needed >= 2 * first.slots_needed - 1, "about twice the quarter-hours"
        facts = preview.plan_watch_facts()["learned_power"]
        assert facts["trusted"] and facts["planned_with"] and facts["subject"] == subject_for(device_id)


@pytest.mark.usefixtures("offline_relay")
async def test_a_plan_charge_at_full_current_is_learned_from(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    _record_switch(hass)
    drawn = PLANNED_KW * 0.6
    with freeze_time(NOW) as frozen:
        charger, device_id, _ = await charger_and_car(hass, soc_percent="70", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        register = 1000.0
        for _step in range(9):
            frozen.tick(timedelta(minutes=5))
            if hass.states.get("switch.wallbox").state == "on":
                register += drawn * 5 / 60
            hass.states.async_set(REGISTER, f"{register:.3f}", ATTRIBUTES)
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        learned = preview._learned.power(subject_for(device_id), WIRING, LOW_KW, PLANNED_KW)  # noqa: SLF001
    assert learned.minutes >= 20
    assert learned.kw == pytest.approx(drawn, rel=0.02)
    assert not learned.trusted, "one plug-in"


async def test_a_malformed_store_on_disk_plans_as_before(
    hass: HomeAssistant, hass_storage: dict[str, Any], transport: Any, offline_relay: Any
) -> None:
    hass_storage["spotnav.learned_power.soc_charger"] = {
        "version": 1,
        "minor_version": 1,
        "key": "spotnav.learned_power.soc_charger",
        "data": {"subjects": {"charger": {"wiring": "three", "sessions": [{"id": 1}]}}},
    }
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, _, _ = await charger_and_car(hass, soc_percent="70", amps=16, phases=3)
        proposal = preview_for(hass, charger.entry_id).snapshot().proposal
    assert proposal.power_kw == pytest.approx(PLANNED_KW)
