"""The wall energy one percent of a car's battery takes, learned from its charges, and used once trusted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import DRIVER_MANUAL_KWH
from custom_components.spotnav.planning.learned_energy import (
    Anchor,
    EnergySample,
    EnergySampler,
    fits,
    learned_from,
    LearnedEnergyStore,
    MIN_DELTA_PERCENT,
    parse_stored,
    SocSeen,
    with_learned,
)
from custom_components.spotnav.runtime import preview_for
from custom_components.spotnav.vehicles.soc_estimate import CHARGE_EFFICIENCY

from .relay import serve
from .test_dashboard_api import NOW
from .world import CAPACITY, charger_and_car

#: A car that really charges at 0.80: 77 kWh / 0.8 / 100 = 0.9625 wall kWh per percent.
REAL_EFFICIENCY = 0.80
RATIO = CAPACITY / REAL_EFFICIENCY / 100.0
CAR = "car-one"
START = datetime(2026, 9, 22, 1, tzinfo=timezone.utc)


class Charge:
    """A car charging at `kw` from `soc`, looked at once a minute, its level read every `every` minutes (made
    `age_s` before it is seen), the register posting each minute."""

    def __init__(
        self,
        sampler: EnergySampler,
        *,
        at: datetime = START,
        soc: float = 20.0,
        register: float = 1000.0,
        kw: float = 11.0,
        session: str = "plug_in:1",
        vehicle: str | None = CAR,
        capacity: float | None = CAPACITY,
        ratio: float = RATIO,
    ) -> None:
        self.sampler, self.now, self.soc, self.register = sampler, at, soc, register
        self.kw, self.session, self.vehicle, self.capacity, self.ratio = kw, session, vehicle, capacity, ratio

    def look(self, *, fresh: bool = True, age_s: float = 30.0, estimated: bool = False, soc: float | None = None) -> None:
        made = self.soc - self.kw * age_s / 3600.0 / self.ratio if soc is None else soc
        self.sampler.observe(
            self.now,
            vehicle_id=self.vehicle,
            session=self.session,
            soc=SocSeen(soc_percent=made, age_s=age_s if fresh else 3600.0, estimated=estimated),
            register_kwh=self.register,
            register_posted_at=self.now,
            capacity_kwh=self.capacity,
        )

    def run(self, minutes: int, *, read_every: int = 5, charging: bool = True) -> None:
        for minute in range(1, minutes + 1):
            self.now += timedelta(minutes=1)
            if charging:
                kwh = self.kw / 60.0
                self.register += kwh
                self.soc = min(100.0, self.soc + kwh / self.ratio)
            self.look(fresh=minute % read_every == 0)

    def to(self, soc: float) -> None:
        """Charge until the level reaches `soc`."""
        minutes = round((soc - self.soc) * self.ratio / self.kw * 60.0)
        self.run(minutes)


def _store(hass: HomeAssistant) -> LearnedEnergyStore:
    return LearnedEnergyStore(hass, "entry")


def _session(sampler: EnergySampler, index: int, **kwargs: Any) -> Charge:
    charge = Charge(sampler, at=START + timedelta(days=index), session=f"plug_in:{index}", **kwargs)
    charge.look()
    charge.to(80.0)
    return charge


# ------------------------------------------------------------------ the rule


def test_bounds_with_and_without_a_battery_size() -> None:
    assert fits(RATIO, CAPACITY), "0.80"
    assert fits(CAPACITY / 100.0, CAPACITY), "1.00"
    assert not fits(CAPACITY / 0.69 / 100.0, CAPACITY), "below 0.70"
    assert not fits(CAPACITY / 1.05 / 100.0, CAPACITY), "above 1.00"
    assert fits(0.5, None) and not fits(0.1, None) and not fits(2.5, None), "10..200 kWh at 0.9"


def test_trusted_with_three_samples_near_their_median() -> None:
    def sample(index: int, ratio: float) -> EnergySample:
        return EnergySample(f"s{index}", ratio * 50.0, 50.0, START + timedelta(days=index))

    two = learned_from(CAR, [sample(0, RATIO), sample(1, RATIO)])
    assert two.kwh_per_percent == pytest.approx(RATIO) and not two.trusted and two.use_kwh_per_percent is None
    three = learned_from(CAR, [sample(0, RATIO), sample(1, RATIO * 1.05), sample(2, RATIO * 0.95)])
    assert three.trusted and three.use_kwh_per_percent == pytest.approx(RATIO)
    spread = learned_from(CAR, [sample(0, RATIO), sample(1, RATIO), sample(2, RATIO * 1.2)])
    assert not spread.trusted, "one of them 20 % off the median"
    many = learned_from(CAR, [sample(index, RATIO * (2.0 if index < 3 else 1.0)) for index in range(8)])
    assert len(many.samples) == 5 and many.kwh_per_percent == pytest.approx(RATIO), "the last five"


def test_the_learned_ratio_as_a_battery_size() -> None:
    learned = learned_from(CAR, [EnergySample(f"s{i}", RATIO * 50.0, 50.0, START) for i in range(3)])
    size = with_learned(learned, CAPACITY)
    assert size is not None and size / CHARGE_EFFICIENCY / 100.0 == pytest.approx(RATIO)
    untrusted = learned_from(CAR, [EnergySample("s", RATIO * 50.0, 50.0, START)])
    assert with_learned(untrusted, CAPACITY) == CAPACITY and with_learned(untrusted, None) is None


# ------------------------------------------------------------------ the sampler


async def test_three_clean_charges_are_trusted_and_two_are_not(hass: HomeAssistant) -> None:
    store = _store(hass)
    sampler = EnergySampler(store)
    for index in range(2):
        _session(sampler, index)
    learned = store.learned(CAR, CAPACITY)
    assert len(learned.samples) == 2 and not learned.trusted
    assert learned.kwh_per_percent == pytest.approx(RATIO, rel=0.02)
    _session(sampler, 2)
    learned = store.learned(CAR, CAPACITY)
    assert learned.trusted and learned.kwh_per_percent == pytest.approx(RATIO, rel=0.02)
    assert learned.samples[0].delta_percent == pytest.approx(60.0, abs=1.5)


async def test_a_charge_over_several_windows_is_one_sample(hass: HomeAssistant) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    charge.look()
    charge.to(40.0)
    charge.run(120, charging=False)  # between two windows, the car read now and then
    charge.to(60.0)
    charge.run(60, charging=False)
    charge.to(80.0)
    learned = store.learned(CAR, CAPACITY)
    assert len(learned.samples) == 1
    assert learned.samples[0].delta_percent == pytest.approx(60.0, abs=1.5)
    assert learned.kwh_per_percent == pytest.approx(RATIO, rel=0.02)


async def test_rounded_levels_still_give_the_ratio(hass: HomeAssistant) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store), soc=20.0)
    charge.look(soc=20.0)
    for _ in range(60):
        charge.run(5, read_every=1000)
        charge.look(soc=float(int(charge.soc - 11.0 * 30 / 3600 / RATIO)))
    assert store.learned(CAR, CAPACITY).kwh_per_percent == pytest.approx(RATIO, rel=0.03)


@pytest.mark.parametrize("end", ["start", "end"])
async def test_a_stale_level_at_either_end_gives_no_sample(hass: HomeAssistant, end: str) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    if end == "start":
        charge.look(fresh=False)
        charge.run(int(60 * RATIO / 11 * 60), read_every=10_000)  # 20 -> 80 with no fresh reading at all
        charge.look()
    else:
        charge.look()
        charge.run(int(60 * RATIO / 11 * 60), read_every=10_000)
        charge.look(fresh=False)
        charge.look(estimated=True)
    assert store.learned(CAR, CAPACITY).samples == ()


async def test_less_than_ten_points_gives_no_sample(hass: HomeAssistant) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    charge.look()
    charge.to(20.0 + MIN_DELTA_PERCENT - 1.0)
    assert store.learned(CAR, CAPACITY).samples == ()
    charge.to(20.0 + MIN_DELTA_PERCENT + 3.0)
    assert len(store.learned(CAR, CAPACITY).samples) == 1


async def test_a_plug_out_in_the_middle_gives_no_sample(hass: HomeAssistant) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    charge.look()
    charge.to(28.0)
    charge.session = "plug_in:2"
    charge.to(36.0)
    assert store.learned(CAR, CAPACITY).samples == (), "8 points either side of the plug-out"


async def test_another_car_or_none_gives_no_sample(hass: HomeAssistant) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    charge.look()
    charge.to(28.0)
    charge.vehicle = "car-two"
    charge.to(36.0)
    charge.vehicle = CAR
    charge.to(44.0)
    assert store.learned(CAR, CAPACITY).samples == () and store.learned("car-two", CAPACITY).samples == ()
    charge.vehicle = None
    charge.look()
    assert store.anchor is None, "an unidentified car is not one car"
    assert store.learned(None, CAPACITY).trusted is False


async def test_a_register_that_went_back_starts_again(hass: HomeAssistant) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    charge.look()
    charge.to(28.0)
    charge.register = 0.0
    charge.to(36.0)
    assert store.learned(CAR, CAPACITY).samples == ()


async def test_an_out_of_bounds_sample_is_discarded_and_counted_once(hass: HomeAssistant) -> None:
    store = _store(hass)
    # A car that took 1.6 kWh per percent at 77 kWh: an efficiency of 0.48.
    charge = Charge(EnergySampler(store), ratio=1.6)
    charge.look()
    charge.to(80.0)
    learned = store.learned(CAR, CAPACITY)
    assert learned.samples == () and learned.discarded == 1


async def test_a_new_battery_size_rechecks_the_samples(hass: HomeAssistant) -> None:
    store = _store(hass)
    sampler = EnergySampler(store)
    for index in range(3):
        _session(sampler, index)
    assert store.learned(CAR, CAPACITY).trusted
    # 40 kWh at 0.9625 kWh per percent would be an efficiency of 0.42: none of them fits any more.
    learned = store.learned(CAR, 40.0)
    assert learned.samples == () and learned.discarded == 3 and not learned.trusted
    assert store.learned(CAR, CAPACITY).samples == (), "dropped, not hidden"


async def test_without_a_battery_size_the_wide_bounds_apply(hass: HomeAssistant) -> None:
    store = _store(hass)
    sampler = EnergySampler(store)
    for index in range(3):
        _session(sampler, index, capacity=None)
    assert store.learned(CAR, None).trusted


async def test_a_dst_night_counts_real_time(hass: HomeAssistant) -> None:
    """Europe/Stockholm falls back at 03:00 on 25 October 2026: the charge runs across it in local time."""
    store = _store(hass)
    sampler = EnergySampler(store)
    local = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/Stockholm"))
    for index in range(3):
        charge = Charge(sampler, at=local + timedelta(minutes=index), session=f"plug_in:{index}")
        charge.look()
        charge.to(80.0)
    learned = store.learned(CAR, CAPACITY)
    assert learned.trusted and learned.kwh_per_percent == pytest.approx(RATIO, rel=0.02)


async def test_a_restart_mid_charge_keeps_the_start(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    store = _store(hass)
    charge = Charge(EnergySampler(store))
    charge.look()
    charge.to(50.0)
    assert store.learned(CAR, CAPACITY).samples[0].delta_percent == pytest.approx(30.0, abs=1.5)
    start = store.anchor
    assert start is not None and start.soc_percent == pytest.approx(20.0, abs=1.0)
    hass_storage["spotnav.learned_energy.entry"] = {
        "version": 1, "minor_version": 1, "key": "spotnav.learned_energy.entry", "data": store._to_store(),  # noqa: SLF001
    }
    restarted = _store(hass)
    await restarted.async_load()
    assert restarted.anchor == start
    charge.sampler = EnergySampler(restarted)  # a new process: no register values in memory
    charge.to(80.0)
    learned = restarted.learned(CAR, CAPACITY)
    assert len(learned.samples) == 1 and learned.samples[0].delta_percent == pytest.approx(60.0, abs=1.5)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "x",
        {"vehicles": []},
        {"vehicles": {"": {"samples": []}}},
        {"vehicles": {"car": {"samples": "no"}}},
        {"vehicles": {"car": {"samples": [{"id": "a", "kwh": -1, "delta_percent": 20, "at": START.isoformat()}]}}},
        {"vehicles": {"car": {"samples": [{"id": "a", "kwh": 10, "delta_percent": 5, "at": START.isoformat()}]}}},
        {"vehicles": {"car": {"samples": [{"id": "a", "kwh": 10, "delta_percent": 20, "at": "yesterday"}]}}},
        {"vehicles": {"car": {"samples": [{"id": "a", "kwh": 1e999, "delta_percent": 20, "at": START.isoformat()}]}}},
        {"vehicles": {"car": {"samples": [{"id": 1, "kwh": 10, "delta_percent": 20, "at": START.isoformat()}]}}},
    ],
)
def test_a_malformed_store_is_ignored(raw: Any) -> None:
    cars, anchor = parse_stored(raw)
    assert all(not car.samples for car in cars.values()) and anchor is None


def test_a_malformed_anchor_is_ignored() -> None:
    good = Anchor(CAR, "plug_in:1", 20.0, START, 1000.0)
    assert parse_stored({"anchor": good.as_dict()})[1] == good
    for broken in ({**good.as_dict(), "soc_percent": 120}, {**good.as_dict(), "at": "2026-09-22T01:00:00"}, 3):
        assert parse_stored({"anchor": broken})[1] is None


@pytest.mark.parametrize("version", [99, "corrupt"])
async def test_a_store_that_cannot_be_read_starts_empty(
    hass: HomeAssistant, hass_storage: dict[str, Any], version: Any, caplog: pytest.LogCaptureFixture
) -> None:
    key = "spotnav.learned_energy.entry"
    hass_storage[key] = "not a store" if version == "corrupt" else {
        "version": version, "minor_version": 1, "key": key, "data": {"vehicles": {}},
    }
    store = _store(hass)
    await store.async_load()
    assert store.learned(CAR, CAPACITY).samples == ()
    assert "learned energy per percent could not be read" in caplog.text


# ------------------------------------------------------------------ the need


def _trusted(store: LearnedEnergyStore, vehicle_id: str, count: int, ratio: float = RATIO) -> None:
    for index in range(count):
        store.put(vehicle_id, EnergySample(f"s{index}", ratio * 50.0, 50.0, START), CAPACITY)


@pytest.mark.usefixtures("offline_relay")
async def test_the_need_for_a_target_follows_the_learned_ratio_once_trusted(
    hass: HomeAssistant, transport: Any
) -> None:
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, _ = await charger_and_car(hass, soc_percent="20", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        before = preview.snapshot().proposal.requested_kwh
        assert before == pytest.approx(60.0 * CAPACITY / 100.0 / CHARGE_EFFICIENCY)

        _trusted(preview._learned_energy, device_id, 2)  # noqa: SLF001
        await preview.async_recalculate()
        assert preview.snapshot().proposal.requested_kwh == pytest.approx(before), "two samples are not enough"
        facts = preview.plan_watch_facts()["learned_energy"]
        assert len(facts["samples"]) == 2 and not facts["trusted"] and not facts["used"]

        _trusted(preview._learned_energy, device_id, 3)  # noqa: SLF001
        await preview.async_recalculate()
        assert preview.snapshot().proposal.requested_kwh == pytest.approx(60.0 * RATIO)
        facts = preview.plan_watch_facts()["learned_energy"]
        assert facts["trusted"] and facts["used"] and facts["kwh_per_percent"] == pytest.approx(RATIO, abs=1e-4)


@pytest.mark.usefixtures("offline_relay")
async def test_a_fixed_amount_is_unchanged(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, device_id, _ = await charger_and_car(
            hass, soc_percent="20", amps=16, phases=3, driver=DRIVER_MANUAL_KWH, requested_kwh=20.0
        )
        preview = preview_for(hass, charger.entry_id)
        assert preview.snapshot().proposal.requested_kwh == pytest.approx(20.0)
        _trusted(preview._learned_energy, device_id, 3, ratio=RATIO)  # noqa: SLF001
        await preview.async_recalculate()
        assert preview.snapshot().proposal.requested_kwh == pytest.approx(20.0)
        assert not preview.plan_watch_facts()["learned_energy"]["used"]


@pytest.mark.usefixtures("offline_relay")
async def test_another_cars_learning_is_not_used(hass: HomeAssistant, transport: Any) -> None:
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, _device_id, _ = await charger_and_car(hass, soc_percent="20", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        _trusted(preview._learned_energy, "someone-else", 3)  # noqa: SLF001
        await preview.async_recalculate()
        assert preview.snapshot().proposal.requested_kwh == pytest.approx(60.0 * CAPACITY / 100.0 / CHARGE_EFFICIENCY)


async def test_a_malformed_store_on_disk_plans_as_before(
    hass: HomeAssistant, hass_storage: dict[str, Any], transport: Any, offline_relay: Any
) -> None:
    hass_storage["spotnav.learned_energy.soc_charger"] = {
        "version": 1, "minor_version": 1, "key": "spotnav.learned_energy.soc_charger",
        "data": {"vehicles": {"x": {"samples": [{"id": 1}]}}, "anchor": "start"},
    }
    serve(transport, rising=True)
    with freeze_time(NOW):
        charger, _, _ = await charger_and_car(hass, soc_percent="20", amps=16, phases=3)
        proposal = preview_for(hass, charger.entry_id).snapshot().proposal
    assert proposal.requested_kwh == pytest.approx(60.0 * CAPACITY / 100.0 / CHARGE_EFFICIENCY)


@pytest.mark.usefixtures("offline_relay")
async def test_a_plan_charge_is_learned_from_at_the_plan_watch(hass: HomeAssistant, transport: Any) -> None:
    """The look at the plan's open window once a minute feeds the sampler: the car's own readings and the register."""
    from homeassistant.util import dt as dt_util  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: PLC0415

    from .test_learned_power import _record_switch, ATTRIBUTES  # noqa: PLC0415
    from .world import controller_of, REGISTER  # noqa: PLC0415

    serve(transport, rising=True)
    _record_switch(hass)
    with freeze_time(NOW) as frozen:
        charger, device_id, soc_entity = await charger_and_car(hass, soc_percent="40", amps=16, phases=3)
        preview = preview_for(hass, charger.entry_id)
        # This switch charger cannot tell a plug-in: one is given, as a charger that reports it would.
        controller_of(hass, charger.entry_id)._plugged_in_at = dt_util.utcnow()  # noqa: SLF001
        register, soc, kw, seen = 1000.0, 40.0, 11.0, 0
        for _step in range(80):
            frozen.tick(timedelta(minutes=1))
            if hass.states.get("switch.wallbox").state == "on":
                register += kw / 60.0
                soc += kw / 60.0 / RATIO
                seen += 1
            hass.states.async_set(REGISTER, f"{register:.3f}", ATTRIBUTES)
            if _step % 5 == 0:
                hass.states.async_set(soc_entity, str(int(soc)), {"device_class": "battery", "unit_of_measurement": "%"})
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        learned = preview._learned_energy.learned(device_id, CAPACITY)  # noqa: SLF001
    assert seen >= 60, "the plan charged"
    assert len(learned.samples) == 1 and learned.samples[0].delta_percent >= MIN_DELTA_PERCENT
    assert learned.kwh_per_percent == pytest.approx(RATIO, rel=0.12)
