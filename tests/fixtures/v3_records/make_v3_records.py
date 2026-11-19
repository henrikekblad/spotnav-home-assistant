"""Write the records SpotNav 3.0 (the v3 branch) stores, made by its own writers, as one JSON fixture."""
import json
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

from custom_components.spotnav.core.session import CarEnded, ChargeSession as CoreSession
from custom_components.spotnav.planning.auto_settings import (
    AutoSettings, FiscalOverride, GridTariff, TargetSocIntent,
)
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.sessions.costing import Slice
from custom_components.spotnav.sessions.model import ChargeSession
from custom_components.spotnav.sessions.store import _rows
from custom_components.spotnav.site.demand_tracker import HourTracker, RunningHour

UTC = timezone.utc
T = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)


def session(sid, start, end, **kw):
    return ChargeSession(
        id=sid, charger_id="charger_a", start=start, end=end, energy_kwh=6.0, energy_source="register",
        priced_kwh=0.0, cost_minor=None, reference_cost_minor=None, currency="SEK", major_unit="kr",
        minor_unit="öre", started_by="plan_window", strategy="cheapest", vehicle_id="car-1", vehicle_name="Kia",
        solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
        intervals=(Slice(start, start + timedelta(hours=1), 6.0, 0.05, 11.2, date(2026, 11, 18), 0.06),),
        area_id="SE4", **kw,
    )


finished = [
    session("A-1", T, T + timedelta(hours=1), plugged_in_at=T - timedelta(minutes=40), vehicle_decided_by="camera"),
    session("A-2", T + timedelta(hours=3), T + timedelta(hours=4), plugged_in_at=T + timedelta(hours=2)),
]
open_session = replace(
    session("A-3", T + timedelta(hours=6), None, plugged_in_at=T + timedelta(hours=5, minutes=50),
            vehicle_decided_by="answered", start_soc_percent=42.0, register_entity="sensor.charger_energy"),
    last_register_kwh=1234.5, last_sample_at=T + timedelta(hours=6, minutes=30), intervals=(),
)
big = _rows({"charger_a": finished}, {"charger_a": {"status": "done"}})
small = {"open": {"charger_a": open_session.as_dict()}, "closing": {"charger_a": [finished[-1].as_dict()]}}

settings = AutoSettings(
    revision=4, area_id="SE4", amps=16, departure_calendar="calendar.work", departure_calendar_only=True,
    target=TargetSocIntent(vehicle_id="car-1", target_percent=80.0), strategy="cheapest",
).validated()
auto_document = {
    "schema": 1,
    "chargers": {
        "charger_a": {"settings": {**settings.as_dict(), "phases": None}, "proposal": None, "energy_baseline": None,
                      "suggested": []},
    },
}

tariff = GridTariff(
    transfer_percent=FiscalOverride(True, 5.0),
    supplier_markup=FiscalOverride(True, 4.5),
    sell_adjustment=FiscalOverride(True, -2.0),
).validated()

tracker = HourTracker(tz="Europe/Stockholm")
tracker.month = (2026, 11)
tracker.hours = [
    HourMean(start=T, kw=2.5, house_kw=1.2, floor_kw=2.5, outdoor_c=3.4, ex_battery_kw=1.1),
    HourMean(start=T + timedelta(hours=1), kw=1.5, house_kw=1.0, floor_kw=1.5, outdoor_c=3.1, ex_battery_kw=0.9),
]
tracker.running = RunningHour(
    start=T + timedelta(hours=2), import_wh=500.0, covered_s=900.0, house_wh=300.0, house_covered_s=900.0,
    ex_battery_wh=200.0, ex_battery_covered_s=900.0, outdoor_sum=6.0, outdoor_n=2,
)

core = CoreSession(owner="plan", car_ended=CarEnded(at=T, soc=80.0, vehicle="car-1", wait=None))

json.dump(
    {
        "sessions_big": big,
        "sessions_small": small,
        "auto_settings": auto_document,
        "price_grid_tariff": tariff.as_dict(),
        "demand_month": tracker.month_dict(),
        "demand_running": {"running": tracker.running.as_dict()},
        "core_session": core.to_store(),
    },
    sys.stdout,
    indent=1,
    ensure_ascii=False,
    sort_keys=True,
)
