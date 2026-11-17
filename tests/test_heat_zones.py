"""The heat zones in the house card's heat (`site/heat_zones.py`, `site/heat_meter.py`, `site/energy_flow.py`): which
sensor a zone is read by, that each is counted once, that the flow's heat holds them and the house drops by as much,
and that a zone without a sensor (or a broken period) is left out."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.spotnav.site import heat_meter as heat_meter_module
from custom_components.spotnav.site import heat_zones as zones_module
from custom_components.spotnav.site.energy_flow import ROLE_HEAT, ROLE_HOUSE, energy_flow, energy_hours
from custom_components.spotnav.site.heat_meter import (
    KIND_ENERGY,
    KIND_POWER,
    NO_METER,
    STATUS_FOUND,
    HeatMeter,
    async_heat_periods,
    combine_hours,
    dashboard_nesting,
)
from custom_components.spotnav.site.heat_zones import (
    REASON_HEAT_METER,
    REASON_IN_HEAT_METER,
    REASON_IN_ZONE,
    REASON_NO_SENSOR,
    REASON_SHARED,
    DeviceSensor,
    ZoneSource,
    ZoneSources,
    async_zone_periods,
    find_zones,
    pick_zone_sensor,
    resolve_zones,
    summed_periods,
)

UTC = timezone.utc
HOUR = datetime(2026, 11, 17, 6, tzinfo=UTC)
PUMP = HeatMeter(STATUS_FOUND, "sensor.varme_effekt", KIND_POWER)


# ------------------------------------------------------------------------------------------------ which sensor


def test_an_energy_sensor_goes_before_a_power_sensor_and_total_increasing_first() -> None:
    sensors = [
        DeviceSensor("sensor.golv_power", "power", "W", "measurement"),
        DeviceSensor("sensor.golv_energy_today", "energy", "kWh", "total"),
        DeviceSensor("sensor.golv_energy", "energy", "kWh", "total_increasing"),
        DeviceSensor("sensor.golv_temperature", "temperature", "°C", "measurement"),
    ]
    assert pick_zone_sensor(sensors) == ("sensor.golv_energy", KIND_ENERGY)
    assert pick_zone_sensor(sensors[:2]) == ("sensor.golv_energy_today", KIND_ENERGY)
    assert pick_zone_sensor(sensors[:1]) == ("sensor.golv_power", KIND_POWER)


def test_a_sensor_without_statistics_or_another_domain_is_no_source() -> None:
    assert pick_zone_sensor([DeviceSensor("sensor.golv_energy", "energy", "kWh", None)]) is None
    assert pick_zone_sensor([DeviceSensor("sensor.golv_power", "power", "W", None)]) is None
    assert pick_zone_sensor([DeviceSensor("number.golv_power", "power", "W", "measurement")]) is None
    # Told by the unit where the device class is missing.
    assert pick_zone_sensor([DeviceSensor("sensor.golv_kwh", None, "kWh", "total_increasing")]) == ("sensor.golv_kwh", KIND_ENERGY)
    assert pick_zone_sensor([]) is None


# ------------------------------------------------------------------------------------------------ counted once


def _zone(climate: str, sensor: str | None, kind: str = KIND_ENERGY, device: str | None = None):
    return (climate, climate.split(".")[1], None if sensor is None else (sensor, kind), device or f"dev_{climate}")


def test_each_zone_is_counted_once_and_never_twice_with_the_heat_pump() -> None:
    zones = [
        _zone("climate.tvattstuga", "sensor.tvattstuga_energy"),
        _zone("climate.toalett", "sensor.toalett_energy"),
        # Two thermostats of one device: its sensor counts once.
        _zone("climate.toalett_b", "sensor.toalett_energy"),
        # The air-to-air pump's own sensor is the heat pump's meter: the meter holds it.
        _zone("climate.ovanvaning", "sensor.varme_effekt", KIND_POWER),
        # On the heat pump meter's own device.
        _zone("climate.nibe", "sensor.nibe_energy", device="dev_meter"),
        # Nested inside the heat pump's meter on the Energy dashboard.
        _zone("climate.hall", "sensor.hall_energy"),
        # Nested inside another zone's sensor.
        _zone("climate.bad", "sensor.bad_energy"),
        # No sensor of its own: left out (listed only).
        _zone("climate.linnas_rum", None),
    ]
    nested = {"sensor.hall_energy": "sensor.varme_effekt", "sensor.bad_energy": "sensor.tvattstuga_energy"}
    found = resolve_zones(zones, PUMP, meter_device="dev_meter", nested=nested)
    assert found.sensor_ids == ("sensor.tvattstuga_energy", "sensor.toalett_energy")
    assert dict(found.unmeasured) == {
        "climate.toalett_b": REASON_SHARED,
        "climate.ovanvaning": REASON_HEAT_METER,
        "climate.nibe": REASON_HEAT_METER,
        "climate.hall": REASON_IN_HEAT_METER,
        "climate.bad": REASON_IN_ZONE,
        "climate.linnas_rum": REASON_NO_SENSOR,
    }
    assert {"entity_id": "climate.linnas_rum", "reason": REASON_NO_SENSOR} in found.diagnostics()["unmeasured"]


def test_without_a_heat_pump_meter_the_zones_are_the_heat_and_a_moved_meter_comes_after() -> None:
    zones = [_zone("climate.tvattstuga", "sensor.tvattstuga_energy")]
    moved = [("sensor.shelly_golv", "Shelly golvvärme", "dev_shelly"), ("sensor.tvattstuga_energy", "Same", None)]
    found = resolve_zones(zones, NO_METER, moved=moved)
    assert [(s.sensor_id, s.origin) for s in found.sources] == [
        ("sensor.tvattstuga_energy", "zone"),
        ("sensor.shelly_golv", "meter"),
    ]
    assert dict(found.unmeasured) == {"sensor.tvattstuga_energy": REASON_SHARED}


def test_the_dashboard_pairs_a_power_sensor_with_its_statistic() -> None:
    prefs = {
        "device_consumption": [
            {"stat_consumption": "sensor.varme_energi", "stat_rate": "sensor.varme_effekt", "name": "Värme"},
            {"stat_consumption": "sensor.golv_energi", "included_in_stat": "sensor.varme_energi"},
        ]
    }
    nested, pairs, names = dashboard_nesting(prefs)
    assert nested == {"sensor.golv_energi": "sensor.varme_energi"}
    assert pairs == {"sensor.varme_effekt": "sensor.varme_energi"}
    assert names == {"sensor.varme_energi": "Värme", "sensor.golv_energi": ""}
    # A zone nested in the meter's statistic, the meter read by its power sensor: still counted once.
    found = resolve_zones(
        [_zone("climate.golv", "sensor.golv_energi")], PUMP, meter_ids=("sensor.varme_effekt", "sensor.varme_energi"), nested=nested
    )
    assert found.sources == () and dict(found.unmeasured) == {"climate.golv": REASON_IN_HEAT_METER}
    assert dashboard_nesting(None) == ({}, {}, {})


async def test_the_registries_give_each_zone_its_device_s_sensor(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain="mqtt")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    golv = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("mqtt", "golv")}, name="Golvvärme tvättstuga")
    air = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("mqtt", "air")}, name="Värmepump ovanvåning")
    entities.async_get_or_create("climate", "mqtt", "golv", device_id=golv.id, suggested_object_id="golvvarme_tvattstuga")
    entities.async_get_or_create("sensor", "mqtt", "golv_e", device_id=golv.id, suggested_object_id="golvvarme_tvattstuga_energy")
    entities.async_get_or_create("sensor", "mqtt", "golv_p", device_id=golv.id, suggested_object_id="golvvarme_tvattstuga_power")
    entities.async_get_or_create("climate", "mqtt", "air", device_id=air.id, suggested_object_id="ovanvaning")
    hass.states.async_set("climate.golvvarme_tvattstuga", "heat", {"friendly_name": "Golvvärme tvättstuga"})
    hass.states.async_set(
        "sensor.golvvarme_tvattstuga_energy", "0.77",
        {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    hass.states.async_set(
        "sensor.golvvarme_tvattstuga_power", "0", {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"}
    )
    found = find_zones(hass, ["climate.golvvarme_tvattstuga", "climate.ovanvaning", "climate.gone"], PUMP)
    assert [(s.entity_id, s.name, s.sensor_id, s.kind) for s in found.sources] == [
        ("climate.golvvarme_tvattstuga", "Golvvärme tvättstuga", "sensor.golvvarme_tvattstuga_energy", KIND_ENERGY)
    ]
    assert dict(found.unmeasured) == {"climate.ovanvaning": REASON_NO_SENSOR, "climate.gone": REASON_NO_SENSOR}


# ------------------------------------------------------------------------------------------------ their energy


async def test_the_zones_periods_are_read_in_one_go_and_a_broken_period_is_left_out(hass: HomeAssistant, monkeypatch) -> None:
    sources = ZoneSources(
        (
            ZoneSource("climate.a", "A", "sensor.a_energy", KIND_ENERGY),
            ZoneSource("climate.b", "B", "sensor.b_power", KIND_POWER),
        )
    )
    calls: list[tuple[frozenset[str], str]] = []

    def statistics(_hass, _start, _end, ids, period, _units, types):
        calls.append((frozenset(ids), next(iter(types))))
        if "change" in types:
            return {"sensor.a_energy": [{"start": HOUR, "change": 0.4}, {"start": HOUR + timedelta(hours=1), "change": 378.0}]}
        return {"sensor.b_power": [{"start": HOUR, "mean": 0.6}]}

    class _Recorder:
        async def async_add_executor_job(self, job, *args):
            return job(*args)

    import homeassistant.components.recorder as recorder
    import homeassistant.components.recorder.statistics as stats

    monkeypatch.setattr(recorder, "get_instance", lambda _hass: _Recorder())
    monkeypatch.setattr(stats, "statistics_during_period", statistics)
    read = await async_zone_periods(hass, sources, HOUR, HOUR + timedelta(hours=2), cap_kw=13.8)
    assert sorted(calls, key=lambda c: c[1]) == [(frozenset({"sensor.a_energy"}), "change"), (frozenset({"sensor.b_power"}), "mean")]
    # The 378 kWh hour (a plug whose total dropped and came back) is no energy.
    assert read == {"sensor.a_energy": {HOUR: 0.4}, "sensor.b_power": {HOUR: 0.6}}
    assert summed_periods(read) == {HOUR: 1.0}
    assert await async_zone_periods(hass, ZoneSources(), HOUR, HOUR + timedelta(hours=1)) == {}


async def test_the_flow_s_heat_is_the_meter_plus_the_zones(hass: HomeAssistant, monkeypatch) -> None:
    zones = ZoneSources((ZoneSource("climate.a", "A", "sensor.a_energy", KIND_ENERGY),))

    async def pump(_hass, _meter, _start, _end, _period="hour"):
        return {HOUR: 1.0}

    async def zone(_hass, _zones, _start, _end, _period="hour", _cap=None):
        return {"sensor.a_energy": {HOUR: 0.3, HOUR + timedelta(hours=1): 0.2}}

    monkeypatch.setattr(heat_meter_module, "async_heat_hours", pump)
    monkeypatch.setattr(zones_module, "async_zone_periods", zone)
    end = HOUR + timedelta(hours=2)
    # The hour the meter lacks keeps its heat in the house: never a partial heat figure.
    assert await async_heat_periods(hass, PUMP, zones, HOUR, end) == {HOUR: 1.3}
    assert await async_heat_periods(hass, PUMP, ZoneSources(), HOUR, end) == {HOUR: 1.0}
    assert await async_heat_periods(hass, NO_METER, zones, HOUR, end) == {HOUR: 0.3, HOUR + timedelta(hours=1): 0.2}
    assert await async_heat_periods(hass, NO_METER, ZoneSources(), HOUR, end) is None
    assert combine_hours({HOUR: 1.0}, {HOUR: 0.5, HOUR + timedelta(hours=1): 9.0}) == {HOUR: 1.5}
    assert combine_hours(None, {HOUR: 0.5}) == {HOUR: 0.5}


def _node(flow, role: str) -> float:
    return next(node["kwh"] for node in flow["nodes"] if node["role"] == role and node["side"] == "use")


def test_the_house_drops_by_what_the_zones_add_to_heat_and_the_totals_still_add_up() -> None:
    starts = [HOUR, HOUR + timedelta(hours=1)]
    grid = {HOUR: 3.0, HOUR + timedelta(hours=1): 2.0}
    pump_only = energy_hours(starts, grid, None, None, None, None, None, {HOUR: 1.0, HOUR + timedelta(hours=1): 0.8})
    with_zones = energy_hours(starts, grid, None, None, None, None, None, {HOUR: 1.4, HOUR + timedelta(hours=1): 1.0})
    before = energy_flow(pump_only, day=date(2026, 11, 17), has_solar=False, has_battery=False, has_heat=True, heat_sources=1)
    after = energy_flow(with_zones, day=date(2026, 11, 17), has_solar=False, has_battery=False, has_heat=True, heat_sources=3)
    assert before is not None and after is not None
    assert _node(after, ROLE_HEAT) == round(_node(before, ROLE_HEAT) + 0.6, 2)
    assert _node(after, ROLE_HOUSE) == round(_node(before, ROLE_HOUSE) - 0.6, 2)
    for flow in (before, after):
        uses = sum(node["kwh"] for node in flow["nodes"] if node["side"] == "use")
        assert uses == sum(node["kwh"] for node in flow["nodes"] if node["side"] == "source") == 5.0
    assert (before["heat_sources"], after["heat_sources"]) == (1, 3)
    no_heat = energy_flow(pump_only, day=date(2026, 11, 17), has_solar=False, has_battery=False)
    assert no_heat is not None and no_heat["heat_sources"] == 0


async def test_the_site_holds_the_zones_beside_the_meter(hass: HomeAssistant, monkeypatch) -> None:
    from custom_components.spotnav.site.heat_meter import SiteHeatMeter

    zones = ZoneSources((ZoneSource("climate.a", "A", "sensor.a_energy", KIND_ENERGY),))
    climates = ["climate.a"]
    site = SiteHeatMeter(hass, lambda: None, lambda: climates)
    monkeypatch.setattr(site, "current_zones", lambda: zones if climates else ZoneSources())

    async def prefs(_hass):
        return None

    async def zone(_hass, _zones, start, end, _period="hour", _cap=None):
        return {"sensor.a_energy": {HOUR: 0.5}}

    monkeypatch.setattr(heat_meter_module, "async_energy_prefs", prefs)
    monkeypatch.setattr(zones_module, "async_zone_periods", zone)
    assert await site.async_refresh(HOUR + timedelta(hours=2)) is True
    assert site.zone_by_hour == {HOUR: 0.5} and site.source_count == 1
    # No heat pump meter: the zones alone are the house card's heat.
    assert site.combined_by_hour() == {HOUR: 0.5}
    assert site.diagnostics()["zones"]["sources"][0]["sensor_id"] == "sensor.a_energy"
    # Read again with the same zones: nothing changes what is measured.
    assert await site.async_refresh(HOUR + timedelta(hours=3)) is False
    # The zone taken out of the settings: its hours go.
    climates.clear()
    assert await site.async_refresh(HOUR + timedelta(hours=3)) is True
    assert site.zone_by_hour == {} and site.combined_by_hour() == {}


def test_a_controller_reads_the_zones_and_the_moved_meters_from_its_entry() -> None:
    from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

    entry = SimpleNamespace(
        data={
            "heat": {"zones": [{"entity_id": "climate.a", "offset_c": 0}, {"entity_id": "climate.b", "offset_c": -1}]},
            "meter_belongs": {"sensor.shelly": "heat", "sensor.nas": "none", "sensor.bad": "kitchen"},
        }
    )
    fake = SimpleNamespace(hass=SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda _id: entry)), entry_id="x")
    assert SiteCapacityController._heat_zones(fake) == ("climate.a", "climate.b")  # type: ignore[arg-type]
    assert SiteCapacityController._heat_moved_meters(fake) == ("sensor.shelly",)  # type: ignore[arg-type]
