"""Today's energy flow (`site/energy_flow.py`): the allocation rule hour by hour, the balance of every node, the nodes a
site without sun or battery leaves out, and the Energy dashboard's meters it reads (`site/energy_statistics.py`)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from custom_components.spotnav.site.energy_flow import (
    FLOW_KEYS,
    LINK_KEYS,
    NODE_KEYS,
    EnergyHour,
    allocate_hour,
    energy_flow,
    energy_hours,
)
from custom_components.spotnav.site.energy_statistics import FlowIds, energy_flow_ids

H0 = datetime(2026, 11, 12, 23, tzinfo=timezone.utc)
DAY = date(2026, 11, 13)


def hour(i: int, **kw: float | None) -> EnergyHour:
    values: dict = {"grid_in": 0.0, "grid_out": 0.0, "solar": 0.0, "battery_in": 0.0, "battery_out": 0.0, "car": 0.0}
    values.update(kw)
    return EnergyHour(start=H0 + timedelta(hours=i), **values)


def close(flows: dict, expected: dict) -> None:
    assert set(flows) == set(expected), flows
    for key, value in expected.items():
        assert abs(flows[key] - value) < 1e-9, (key, flows[key])


def test_the_sun_feeds_house_car_battery_then_export() -> None:
    # 5 kWh of sun, a 1 kWh house, a 1.5 kWh car, 2 kWh into the battery, 0.5 exported.
    close(
        allocate_hour(hour(0, solar=5.0, battery_in=2.0, car=1.5, grid_out=0.5)),
        {("solar", "house"): 1.0, ("solar", "car"): 1.5, ("solar", "battery"): 2.0, ("solar", "grid"): 0.5},
    )


def test_a_night_grid_charge_and_a_sunny_hour_do_not_mix() -> None:
    night = hour(3, grid_in=4.5, battery_in=2.7)
    noon = hour(12, solar=3.0, battery_in=1.0, battery_out=0.0, grid_out=0.5)
    close(allocate_hour(night), {("grid", "battery"): 2.7, ("grid", "house"): 1.8})
    close(allocate_hour(noon), {("solar", "house"): 1.5, ("solar", "battery"): 1.0, ("solar", "grid"): 0.5})
    # Summed over the day the sun never charged the battery at night, nor the grid at noon.
    flow = energy_flow([night, noon], day=DAY, has_solar=True, has_battery=True)
    links = {(link["from"], link["to"]): link["kwh"] for link in flow["links"]}
    assert links[("source.grid", "use.battery")] == 2.7 and links[("source.solar", "use.battery")] == 1.0


def test_the_battery_covers_export_before_the_house_and_the_grid_charges_it() -> None:
    # The battery exports 2 kWh while the grid feeds the house: the export is the battery's, the house the grid's.
    close(
        allocate_hour(hour(18, battery_out=3.0, grid_in=1.0, grid_out=2.0)),
        {("battery", "grid"): 2.0, ("battery", "house"): 1.0, ("grid", "house"): 1.0},
    )
    # The battery feeds the house and the car before the grid does.
    close(
        allocate_hour(hour(19, battery_out=1.0, grid_in=2.0, car=2.0)),
        {("battery", "house"): 1.0, ("grid", "car"): 2.0},
    )


def test_a_battery_charging_and_discharging_in_one_hour_is_netted() -> None:
    flows = allocate_hour(hour(5, battery_in=1.0, battery_out=1.0, grid_in=0.5))
    close(flows, {("battery", "house"): 0.5, ("grid", "battery"): 0.5})
    assert ("battery", "battery") not in flows


def test_meters_that_disagree_take_the_shortfall_off_export_first() -> None:
    # Sources 1 kWh, uses 1.5: the house is 0 and export gives up the half.
    close(allocate_hour(hour(9, solar=1.0, grid_out=1.0, battery_in=0.5)), {("solar", "battery"): 0.5, ("solar", "grid"): 0.5})


def test_every_node_balances_and_the_shape_is_fixed() -> None:
    hours = [
        hour(0, grid_in=1.6),
        hour(3, grid_in=4.5, battery_in=2.7),
        hour(11, grid_in=12.7, solar=1.6, battery_in=7.4, car=4.7),
        hour(17, battery_out=2.1, grid_out=0.03),
    ]
    flow = energy_flow(hours, day=DAY, has_solar=True, has_battery=True, soc_pct=41.27)
    assert tuple(flow) == FLOW_KEYS
    assert all(tuple(node) == NODE_KEYS for node in flow["nodes"])
    assert all(tuple(link) == LINK_KEYS for link in flow["links"])
    assert [node["id"] for node in flow["nodes"]] == [
        "source.solar", "source.battery", "source.grid", "use.battery", "use.house", "use.car", "use.grid",
    ]
    for node in flow["nodes"]:
        key = "from" if node["side"] == "source" else "to"
        assert abs(sum(link["kwh"] for link in flow["links"] if link[key] == node["id"]) - node["kwh"]) < 0.02
    sources = sum(node["kwh"] for node in flow["nodes"] if node["side"] == "source")
    uses = sum(node["kwh"] for node in flow["nodes"] if node["side"] == "use")
    assert abs(sources - uses) < 0.02
    battery_use = next(node for node in flow["nodes"] if node["id"] == "use.battery")
    assert battery_use["soc_pct"] == 41.3
    assert (flow["hours"], flow["car_hours"], flow["through"]) == (4, 4, (H0 + timedelta(hours=18)).isoformat())


def test_a_site_without_sun_or_battery_has_no_such_nodes() -> None:
    hours = [hour(0, solar=None, battery_in=None, battery_out=None, grid_in=3.0, car=2.0)]
    flow = energy_flow(hours, day=DAY, has_solar=False, has_battery=False, soc_pct=50.0)
    assert [node["id"] for node in flow["nodes"]] == ["source.grid", "use.house", "use.car", "use.grid"]
    assert {(link["from"], link["to"]): link["kwh"] for link in flow["links"]} == {
        ("source.grid", "use.house"): 1.0,
        ("source.grid", "use.car"): 2.0,
    }
    assert energy_flow([], day=DAY, has_solar=False, has_battery=False) is None


def test_an_hour_a_meter_lacks_is_left_out_and_an_unread_car_is_in_the_house() -> None:
    starts = [H0 + timedelta(hours=i) for i in range(3)]
    grid_in = {starts[0]: 1.0, starts[1]: 2.0, starts[2]: 3.0}
    solar = {starts[0]: 0.0, starts[2]: 0.5}
    hours = energy_hours(starts, grid_in, None, solar, None, None, {starts[0]: 0.5})
    assert [h.start for h in hours] == [starts[0], starts[2]]
    assert hours[0].car == 0.5 and hours[1].car is None and hours[1].grid_out == 0.0
    flow = energy_flow(hours, day=DAY, has_solar=True, has_battery=False)
    assert flow["car_hours"] == 1
    house = next(node for node in flow["nodes"] if node["id"] == "use.house")
    assert house["kwh"] == 4.0


def test_the_energy_dashboards_meters() -> None:
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.import", "stat_energy_to": "sensor.export"},
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.old_import"}], "flow_to": [{"stat_energy_to": "sensor.old_export"}]},
            {"type": "solar", "stat_energy_from": "sensor.pv"},
            {"type": "battery", "stat_energy_from": "sensor.discharged", "stat_energy_to": "sensor.charged"},
            {"type": "battery", "stat_energy_from": "sensor.half_a_battery"},
        ]
    }
    assert energy_flow_ids(prefs) == FlowIds(
        grid_in=("sensor.import", "sensor.old_import"),
        grid_out=("sensor.export", "sensor.old_export"),
        solar=("sensor.pv",),
        battery_in=("sensor.charged",),
        battery_out=("sensor.discharged",),
    )
    assert energy_flow_ids(None) == FlowIds()
