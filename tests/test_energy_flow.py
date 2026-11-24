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
    on_ac_side,
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


def test_the_soc_at_each_hours_end_from_the_recorder() -> None:
    from custom_components.spotnav.site.energy_statistics import soc_at_hour_ends

    starts = [H0 + timedelta(hours=i) for i in range(4)]
    short = [
        {"start": (H0 + timedelta(minutes=50)).timestamp(), "mean": 40.0},
        {"start": (H0 + timedelta(minutes=55)).timestamp(), "mean": 41.5},  # the last five minutes of hour 0
        {"start": H0 + timedelta(hours=2, minutes=55), "mean": 130.0},  # not a level: left out
    ]
    hourly = [
        {"start": (H0 + timedelta(hours=1)).timestamp(), "mean": 44.0},  # hour 1: no short row, its mean
        {"start": (H0 + timedelta(hours=2)).timestamp(), "mean": 47.0},
        {"start": (H0 + timedelta(hours=0)).timestamp(), "mean": 30.0},  # the short row wins
    ]
    assert soc_at_hour_ends(starts, short, hourly) == {starts[0]: 41.5, starts[1]: 44.0, starts[2]: 47.0}


def test_heat_is_split_out_of_the_house_where_the_meter_reads() -> None:
    # 4 kWh imported, the heat pump's meter 2.5: the house keeps 1.5; the sun covers the house before heat.
    close(allocate_hour(hour(2, grid_in=4.0, heat=2.5)), {("grid", "house"): 1.5, ("grid", "heat"): 2.5})
    close(
        allocate_hour(hour(11, solar=2.0, grid_in=1.0, heat=1.6)),
        {("solar", "house"): 1.4, ("solar", "heat"): 0.6, ("grid", "heat"): 1.0},
    )
    # A meter that reads more than the balance leaves the house at 0 and heat at what the other meters allow.
    close(allocate_hour(hour(3, grid_in=1.0, heat=1.4)), {("grid", "heat"): 1.0})
    hours = [hour(2, grid_in=4.0, heat=2.5), hour(3, grid_in=1.2, heat=None)]
    flow = energy_flow(hours, day=DAY, has_solar=False, has_battery=False, has_heat=True)
    assert tuple(flow) == FLOW_KEYS and flow["heat_hours"] == 1
    nodes = {node["id"]: node["kwh"] for node in flow["nodes"]}
    # The hour without the heat figure leaves it in the house.
    assert nodes == {"source.grid": 5.2, "use.house": 2.7, "use.heat": 2.5, "use.car": 0.0, "use.grid": 0.0}
    # Without a heat meter there is no heat node.
    plain = energy_flow(hours, day=DAY, has_solar=False, has_battery=False)
    assert "use.heat" not in {node["id"] for node in plain["nodes"]}
    # The heat figures are read per hour beside the others.
    starts = [H0 + timedelta(hours=i) for i in range(2)]
    read = energy_hours(starts, {starts[0]: 1.0, starts[1]: 2.0}, None, None, None, None, None, {starts[1]: 0.7})
    assert [item.heat for item in read] == [None, 0.7]


# ------------------------------------------------------------------------------------------------ by the quarter hour

NOON = datetime(2026, 11, 13, 11, tzinfo=timezone.utc)


def five_rows(start: datetime, values: list[float]) -> dict[datetime, float]:
    """5-minute rows from `start` on."""
    return {start + i * timedelta(minutes=5): value for i, value in enumerate(values)}


def test_a_sun_quarter_and_a_grid_charge_quarter_in_one_hour_stay_apart() -> None:
    """Quarter 1 the sun is exported, quarter 2 the grid charges the battery: by the hour the two would net into the
    sun charging the battery; by the quarter each keeps its own source."""
    from custom_components.spotnav.site.energy_flow import FlowSeries, quarter_hours

    q = [0.0] * 12
    sun = list(q)
    export = list(q)
    grid = list(q)
    charge = list(q)
    for i in range(3):
        sun[i] = export[i] = 1 / 3
        grid[3 + i] = charge[3 + i] = 1 / 3
    five = FlowSeries(
        grid_in=five_rows(NOON, grid),
        grid_out=five_rows(NOON, export),
        solar=five_rows(NOON, sun),
        battery_in=five_rows(NOON, charge),
        battery_out=five_rows(NOON, q),
        car=five_rows(NOON, q),
    )
    periods, missing = quarter_hours([NOON], five, NOON + timedelta(hours=2))
    assert missing == [] and [p.minutes for p in periods] == [15, 15, 15, 15]
    flow = energy_flow(periods, day=DAY, has_solar=True, has_battery=True, step_min=15)
    links = {(link["from"], link["to"]): link["kwh"] for link in flow["links"]}
    assert links == {("source.solar", "use.grid"): 1.0, ("source.grid", "use.battery"): 1.0}
    assert flow["step_min"] == 15 and flow["through"] == (NOON + timedelta(hours=1)).isoformat()
    # The same hour by the hour mixes them: the sun is taken into the battery and the rest is netted out.
    whole = EnergyHour(start=NOON, grid_in=1.0, grid_out=1.0, solar=1.0, battery_in=1.0, battery_out=0.0, car=0.0)
    close(allocate_hour(whole), {("solar", "battery"): 1.0})


def test_the_running_quarter_counts_its_finished_rows() -> None:
    from custom_components.spotnav.site.energy_flow import FlowSeries, quarter_hours

    rows = five_rows(NOON, [0.1, 0.2, 0.3, 0.4])  # 11:00-11:20
    five = FlowSeries(grid_in=rows, grid_out=rows, car=five_rows(NOON, [0.05, 0.05, 0.05]))
    periods, missing = quarter_hours([NOON], five, NOON + timedelta(minutes=21))
    assert missing == []
    assert [(p.start, p.minutes) for p in periods] == [(NOON, 15), (NOON + timedelta(minutes=15), 5)]
    assert abs(periods[0].grid_in - 0.6) < 1e-9 and abs(periods[1].grid_in - 0.4) < 1e-9
    # The cars' register has the first quarter whole, not the running one: that one counts them in the house.
    assert abs(periods[0].car - 0.15) < 1e-9 and periods[1].car is None
    flow = energy_flow(periods, day=DAY, has_solar=False, has_battery=False, step_min=15)
    assert flow["through"] == (NOON + timedelta(minutes=20)).isoformat() and flow["hours"] == 2
    # Just after a quarter's end the recorder may not have its last row yet: the quarter runs with the rows it has.
    early, _ = quarter_hours([NOON], FlowSeries(grid_in=five_rows(NOON, [0.1, 0.2])), NOON + timedelta(minutes=15, seconds=5))
    assert [(p.start, p.minutes) for p in early] == [(NOON, 10)]
    # The battery plan's quarter (no register) counts its share of the running quarter.
    planned = FlowSeries(grid_in=rows, car_quarters={NOON + timedelta(minutes=15): 0.9})
    running = quarter_hours([NOON], planned, NOON + timedelta(minutes=21))[0]
    assert abs(running[1].car - 0.3) < 1e-9


def test_a_finished_hour_missing_a_row_is_read_by_the_hour() -> None:
    from custom_components.spotnav.site.energy_flow import FlowSeries, quarter_hours

    hours = [NOON, NOON + timedelta(hours=1)]
    grid = five_rows(NOON, [0.1] * 24)
    export = dict(grid)
    del export[NOON + timedelta(minutes=35)]
    periods, missing = quarter_hours(hours, FlowSeries(grid_in=grid, grid_out=export), NOON + timedelta(hours=3))
    assert missing == [NOON] and [p.start for p in periods] == [NOON + timedelta(hours=1, minutes=15 * i) for i in range(4)]


def test_todays_first_minutes_are_an_empty_flow() -> None:
    from custom_components.spotnav.site.energy_flow import FlowSeries, quarter_hours

    midnight = datetime(2026, 11, 12, 23, tzinfo=timezone.utc)
    periods, missing = quarter_hours([midnight], FlowSeries(grid_in={}), midnight + timedelta(minutes=3))
    assert periods == [] and missing == []
    flow = energy_flow(periods, day=DAY, has_solar=True, has_battery=True, step_min=15, since=midnight)
    assert tuple(flow) == FLOW_KEYS
    assert (flow["hours"], flow["links"], flow["through"]) == (0, [], midnight.isoformat())
    assert all(node["kwh"] == 0 for node in flow["nodes"])
    assert energy_flow([], day=DAY, has_solar=True, has_battery=True) is None


def test_the_hours_of_the_cars_and_heat_from_quarters() -> None:
    from custom_components.spotnav.site.energy_flow import hour_figures

    def floor(moment: datetime) -> datetime:
        return moment.replace(minute=0)

    whole = [EnergyHour(start=NOON + timedelta(minutes=15 * i), grid_in=1.0, grid_out=0.0, car=0.5, minutes=15) for i in range(4)]
    part = [EnergyHour(start=NOON + timedelta(hours=1), grid_in=1.0, grid_out=0.0, car=0.5, minutes=15)]
    unread = [
        EnergyHour(start=NOON + timedelta(hours=2, minutes=15 * i), grid_in=1.0, grid_out=0.0, car=None if i else 1.0, minutes=15)
        for i in range(4)
    ]
    by_hour = [EnergyHour(start=NOON + timedelta(hours=3), grid_in=1.0, grid_out=0.0, car=2.0)]
    assert hour_figures([*whole, *part, *unread, *by_hour], "car", floor) == {NOON: 2.0, NOON + timedelta(hours=3): 2.0}


def test_the_short_term_window_decides_the_reading() -> None:
    from custom_components.spotnav.api.house import reads_quarters

    now = datetime(2026, 11, 16, 9, 7, tzinfo=timezone.utc)
    assert reads_quarters(now - timedelta(days=10), now, 10)
    assert not reads_quarters(now - timedelta(days=10, seconds=1), now, 10)
    assert reads_quarters(now - timedelta(days=3), now, 3) and not reads_quarters(now - timedelta(days=4), now, 3)
    assert not reads_quarters(now, now, None)


def test_the_pool_the_spa_and_the_sauna_move_from_the_house_into_heat_counted_once() -> None:
    """Their counted quarters are added to each period's heat: the house drops by their kWh, heat rises by the same,
    an hour takes its four quarters, a quarter its own, and a period without them keeps its heat as it was."""
    from custom_components.spotnav.site.energy_flow import EnergyHour, allocate_hour, with_heat_parts

    t0 = datetime(2026, 11, 24, 16, tzinfo=timezone.utc)
    sauna = {t0 + timedelta(minutes=15): 2.25, t0 + timedelta(minutes=30): 2.25, t0 + timedelta(minutes=45): 2.25}
    pool = {t0: 0.1, t0 + timedelta(hours=1): 0.1}
    hours = [
        EnergyHour(start=t0, grid_in=9.0, grid_out=0.0, heat=1.0),
        EnergyHour(start=t0 + timedelta(hours=1), grid_in=1.0, grid_out=0.0, heat=None),
        EnergyHour(start=t0 + timedelta(hours=2), grid_in=1.0, grid_out=0.0, heat=None),
    ]
    moved = with_heat_parts(hours, [sauna, pool])
    before, after = allocate_hour(hours[0]), allocate_hour(moved[0])
    assert moved[0].heat == 1.0 + 6.75 + 0.1
    assert after[("grid", "heat")] - before[("grid", "heat")] == 6.85
    assert round(before[("grid", "house")] - after[("grid", "house")], 6) == 6.85
    # An hour without a heat figure gets theirs alone; one without them keeps none.
    assert moved[1].heat == 0.1 and moved[2].heat is None
    # By the quarter hour: each quarter its own.
    quarters = [EnergyHour(start=t0 + timedelta(minutes=15 * q), grid_in=2.5, grid_out=0.0, minutes=15) for q in range(4)]
    assert [period.heat for period in with_heat_parts(quarters, [sauna])] == [None, 2.25, 2.25, 2.25]


def _house(flow: dict) -> float:
    return next(node["kwh"] for node in flow["nodes"] if node["role"] == "house")


def test_a_battery_read_at_the_battery_is_turned_to_the_ac_side_and_its_loss_said() -> None:
    loss = (0.0366, 0.065)
    # A night: 10 kWh charged from a 13 kWh import, a later hour 10 kWh given to the house.
    night = [hour(0, grid_in=13.0, battery_in=10.0), hour(1, grid_in=0.2, battery_out=10.0)]
    as_read = energy_flow(night, day=DAY, has_solar=True, has_battery=True)
    turned = on_ac_side(night, loss)
    assert turned[0].battery_in == 10.0 * 1.0366 and turned[1].battery_out == 10.0 * 0.935
    flow = energy_flow(turned, day=DAY, has_solar=True, has_battery=True)
    assert flow is not None and as_read is not None
    assert flow["inverter_loss_kwh"] == round(0.366 + 0.65, 2)
    # The loss leaves the house: 3 + 0.2 + 10 kWh as read, 1.02 less on the AC side.
    assert _house(as_read) == 13.2 and _house(flow) == round(13.2 - 0.366 - 0.65, 2)
    assert as_read["inverter_loss_kwh"] is None
    # Every node still balances.
    sources = sum(node["kwh"] for node in flow["nodes"] if node["side"] == "source")
    uses = sum(node["kwh"] for node in flow["nodes"] if node["side"] == "use")
    assert abs(sources - uses) < 0.02


def test_the_suns_charge_over_the_dc_bus_takes_the_way_out_loss_and_the_import_caps_the_grids_share() -> None:
    loss = (0.03, 0.05)
    # 4 kWh charged with 1 kWh imported: about 0.97 kWh of it from the grid (with its loss, at most the import).
    period = on_ac_side([hour(0, solar=5.0, grid_in=1.0, battery_in=4.0)], loss)[0]
    from_grid = 1.0 / 1.03
    assert abs(period.battery_in - (from_grid * 1.03 + (4.0 - from_grid) * 0.95)) < 1e-9
    assert period.inverter_loss is not None and period.inverter_loss < 0
    # A sunny day's net may be below zero: never a negative line.
    flow = energy_flow([period], day=DAY, has_solar=True, has_battery=True)
    assert flow is not None and flow["inverter_loss_kwh"] == 0.0
    # A period without the battery's figures is kept as it is.
    bare = hour(1, battery_in=None, battery_out=None)
    assert on_ac_side([bare], loss) == [bare]
