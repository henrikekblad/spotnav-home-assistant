"""`spotnav/get_heat`'s answer from the pure view (`heat/view.py`), and the contract fixtures the card reads
(`tests/fixtures/heat_card/`, `SPOTNAV_WRITE_FIXTURES=1` rewrites them):

* `villa.json`: a villa like the owner's on 16 November 2026 at 17:20, a NIBE without a network (no pump facts),
  its meter on the Energy dashboard, three indoor sensors, three electric floor zones and an air-to-air heat pump
  upstairs, a car planned tonight, the cooler night on; an administrator's answer (with `choices`).
* `network_pump.json`: a house with a NIBE on its local integration whose own price control runs and whose
  degree minutes are disabled, while another optimiser is set up; away until Friday; a household member's answer.
* `controlled.json`: the villa once SpotNav's heat control runs (the check passed, the household's automations turned
  off): SpotNav lowered 07-09 and since 17:00, until 19:00.

In the villa SpotNav's heat control is turned on but waits for the household's automation on the relay.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.api.heat import config_block
from custom_components.spotnav.heat.automations import SteeringAutomation
from custom_components.spotnav.heat.config import HeatConfig, Night, SensorOffset, WaterHeater, Zone
from custom_components.spotnav.notifications.heat_settings import HeatNotifications
from custom_components.spotnav.pool.config import PoolConfig, PoolHeatPump
from custom_components.spotnav.sauna.config import SaunaConfig
from custom_components.spotnav.spa.config import Bath, SpaConfig
from custom_components.spotnav.heat.facts import CapabilityStatus, HeatPumpFacts, PriceControl
from custom_components.spotnav.heat.meter_split import levels, split_day
from custom_components.spotnav.heat.relay_effect import BlockResult, Effect
from custom_components.spotnav.heat.review import review_day
from custom_components.spotnav.heat.rooms import SensorReading, indoor
from custom_components.spotnav.heat.indoor_day import LIVE_STEP_MIN, indoor_day
from custom_components.spotnav.heat.view import (
    AHEAD_HOUR_KEYS,
    BAND_HOUR_KEYS,
    HEAT_KEYS,
    HeatViewInputs,
    MeterInputs,
    Outdoor,
    RelayInputs,
    ZoneReading,
    heat_view,
)

from .test_heat_meter_split import villa_day

#: The villa's pool (the owner's: a Madimack on `tuya_local`, a Fibaro relay for the circulation) and a Balboa spa.
POOL = PoolConfig(
    heat_pump=PoolHeatPump("climate.poolpump"),
    circulation_id="switch.pool_cirkulation",
    water_sensor="sensor.pool_vatten",
    heat_pump_kw=1.6,
    control_circulation=True,
)
SPA = SpaConfig("climate.spa", "balboa", baths=(Bath((5, 6), "19:00"),), heater_kw=3.0)

FIXTURES = Path(__file__).parent / "fixtures" / "heat_card"
ZONE = ZoneInfo("Europe/Stockholm")
MIDNIGHT = datetime(2026, 11, 16, 0, 0, tzinfo=ZONE).astimezone(timezone.utc)
NOW = MIDNIGHT + timedelta(hours=17, minutes=20)
PRICES = [80, 78, 76, 75, 77, 85, 120, 190, 210, 160, 120, 110, 105, 100, 105, 130, 180, 240, 260, 220, 150, 110, 95, 85]


def _slots() -> tuple[tuple[datetime, datetime, float], ...]:
    out = []
    for index, price in enumerate(PRICES):
        start = MIDNIGHT + timedelta(hours=index)
        out.extend((start + timedelta(minutes=15 * q), start + timedelta(minutes=15 * (q + 1)), float(price)) for q in range(4))
    return tuple(out)


class _Entry:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def _write_or_compare(name: str, answer: dict[str, Any]) -> None:
    path = FIXTURES / name
    text = json.dumps(answer, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SPOTNAV_WRITE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text)


def _choice(entity_id: str, name: str, value: str | None, **extra: Any) -> dict[str, Any]:
    return {"entity_id": entity_id, "name": name, "value": value, **extra}


def villa_relay() -> RelayInputs:
    """The blueprint closed the relay 07:00-09:00 and since 16:00; it is expected to open at 19:00."""
    minutes = {MIDNIGHT + timedelta(hours=h): (60, False) for h in (7, 8, 16)}
    minutes[MIDNIGHT + timedelta(hours=17)] = (60, True)
    minutes[MIDNIGHT + timedelta(hours=18)] = (60, True)
    return RelayInputs(
        entity_id="switch.larm_utgang_1",
        name="Larm utgång 1",
        closed=True,
        since=MIDNIGHT + timedelta(hours=16),
        source="blueprint",
        automation=("automation.varmepanna_sank_varmekurvan_nar_elen_ar_dyr", "Värmepanna: sänk värmekurvan när elen är dyr"),
        minutes=minutes,
        effect=Effect(0.46, 0.82, 7),
    )


def villa_automations() -> tuple[SteeringAutomation, ...]:
    return (
        SteeringAutomation(
            "automation.golvvarme_borvarde_efter_elpris_och_larm",
            "Golvvärme – börvärde efter elpris och larm",
            ("climate.golvvarme_toalett_nere",),
        ),
        SteeringAutomation(
            "automation.varmepanna_sank_varmekurvan_nar_elen_ar_dyr",
            "Värmepanna: sänk värmekurvan när elen är dyr",
            ("switch.larm_utgang_1",),
        ),
    )


def villa_review() -> dict[str, Any]:
    """Yesterday: the blueprint lowered 16-19, SpotNav would have lowered 7-9 and 17-19."""
    yesterday = MIDNIGHT - timedelta(days=1)
    hours = [yesterday + timedelta(hours=h) for h in range(24)]
    prices = dict(zip(hours, map(float, PRICES), strict=True))
    coldest = {yesterday + timedelta(minutes=5 * i): (20.4 if 17 * 12 <= i < 17 * 12 + 4 else 20.8) for i in range(27 * 12)}
    return review_day(
        (NOW - timedelta(days=1)).astimezone(ZONE).date(),
        hours,
        prices=prices,
        heat_kwh={hour: 1.1 for hour in hours},
        relay=[(hours[16], hours[19])],
        plan_lowered=[hours[7], hours[8], hours[17], hours[18], hours[19]],
        measured=[BlockResult(hours[16], hours[19], 1.15, 0.6, 1.6)],
        effect=Effect(0.46, 0.82, 7),
        coldest=coldest,
        low_c=20.5,
    )


def villa_rooms() -> dict[str, dict[datetime, float]]:
    """The indoor sensors' 5-minute means since midnight: the kitchen steady, upstairs (offset -1) a degree lower and
    cooling half a degree through the lowered morning hours, the toilet a little below the kitchen."""
    out: dict[str, dict[datetime, float]] = {"sensor.inomhus_kok_temperatur": {}, "sensor.ovanvaning_temperatur": {}, "sensor.toalett_nv_temperatur": {}}
    moment = MIDNIGHT
    while moment < NOW - timedelta(minutes=5):
        hour = (moment - MIDNIGHT).total_seconds() / 3600
        dip = 0.5 if 7 <= hour < 9 else 0.0
        out["sensor.inomhus_kok_temperatur"][moment] = 21.3 - dip / 2
        out["sensor.ovanvaning_temperatur"][moment] = 20.5 - dip
        out["sensor.toalett_nv_temperatur"][moment] = 20.7
        moment += timedelta(minutes=5)
    return out


def villa_zone_periods() -> dict[datetime, float]:
    """The floor heating's own meters since midnight: 0.6 kW together through the night, off by day."""
    out: dict[datetime, float] = {}
    moment = MIDNIGHT
    while moment < NOW - timedelta(minutes=5):
        out[moment] = 0.05 if moment.hour < 6 else 0.0
        moment += timedelta(minutes=5)
    return out


def villa_answer(zone_periods: Any = "villa", more: bool = False, **overrides: Any) -> dict[str, Any]:
    """The villa; `more`: the upstairs toilet's floor and the water heater switched by SpotNav too."""
    config = HeatConfig(
        target_c=21.0,
        night=Night(True, 1.0, "23:00", "06:00"),
        indoor_sensors=("sensor.inomhus_kok_temperatur", "sensor.ovanvaning_temperatur", "sensor.toalett_nv_temperatur"),
        # Upstairs is kept a degree cooler than the house.
        indoor_offsets=(SensorOffset("sensor.ovanvaning_temperatur", -1.0),),
        zones=(
            Zone("climate.golvvarme_toalett_nere", 0.0),
            Zone("climate.golvvarme_toalett_uppe", 0.0, more),
            Zone("climate.golvvarme_tvattstuga", -1.0),
            Zone("climate.ovanvaning", -0.5),
        ),
        control_path="relay",
        control_entity="switch.larm_utgang_1",
        control_enabled=True,
        water_heater=WaterHeater("switch.varmvattenberedare", True) if more else None,
        pool=POOL if more else None,
        spa=SPA if more else None,
        sauna=SaunaConfig("switch.bastu", "relay", heater_kw=9.0) if more else None,
        heat_notifications=HeatNotifications(("mobile_app_pixel",)) if more else HeatNotifications(),
    )
    entry = _Entry({"heat": config.as_dict(), "heat_meter_entity": "sensor.varme_effekt"})
    # The last day of 5-minute rows: yesterday evening and today until now, the same shape each day.
    window = {start - timedelta(days=1): kw for start, kw in villa_day().items() if start - timedelta(days=1) >= NOW - timedelta(days=1)}
    window |= {start: kw for start, kw in villa_day().items() if start < NOW}
    found = levels(window)
    split = split_day(window, MIDNIGHT, MIDNIGHT + timedelta(days=1))
    sensors = [
        SensorReading("sensor.inomhus_kok_temperatur", "Inomhus kök temperatur", 21.3, ((NOW - timedelta(minutes=40), 21.2),)),
        SensorReading("sensor.ovanvaning_temperatur", "Ovanvåning temperatur", 20.5, ((NOW - timedelta(minutes=40), 20.5),), None, -1.0),
        SensorReading("sensor.toalett_nv_temperatur", "Toalett NV temperatur", 20.6, ((NOW - timedelta(minutes=40), 20.7),)),
    ]
    car = frozenset(MIDNIGHT + timedelta(hours=h) for h in (1, 2, 3, 4))
    choices = {
        "indoor": [
            _choice("sensor.inomhus_kok_temperatur", "Inomhus kök temperatur", "21.3 °C"),
            _choice("sensor.ovanvaning_temperatur", "Ovanvåning temperatur", "20.5 °C"),
            _choice("sensor.toalett_nv_temperatur", "Toalett NV temperatur", "20.6 °C"),
            _choice("sensor.garage_temperatur", "Garage temperatur", "13.5 °C"),
        ],
        "outdoor": [
            _choice("sensor.utomhus_garage_temperatur", "Utomhus garage temperatur", "9.4 °C"),
            _choice("weather.forecast_hem", "Forecast Hem", "cloudy"),
        ],
        "zones": [
            _choice("climate.golvvarme_toalett_nere", "Golvvärme toalett nedervåning", "off", kind="floor"),
            _choice("climate.golvvarme_toalett_uppe", "Golvvärme toalett ovanvåning", "heat", kind="floor"),
            _choice("climate.golvvarme_tvattstuga", "Golvvärme tvättstuga", "heat", kind="floor"),
            _choice("climate.ovanvaning", "Värmepump ovanvåning", "off", kind="air"),
        ],
        "relay": [_choice("switch.larm_utgang_1", "Larm utgång 1", "off"), _choice("switch.larm_utgang_2", "Larm utgång 2", "off")],
        "offset": [],
        "setpoint": [],
        "hot_water": [_choice("switch.larm_utgang_2", "Larm utgång 2", "off")],
        "meter": [_choice("sensor.varme_effekt", "Elmätare värme (Nibe + panna) Effekt", "1128.84 W")],
        "water_heater": [_choice("switch.varmvattenberedare", "Varmvattenberedare", "on"), _choice("switch.larm_utgang_2", "Larm utgång 2", "off")],
        "tank_sensor": [_choice("sensor.inomhus_kok_temperatur", "Inomhus kök temperatur", "21.3 °C")],
        "pool": {
            "heat_pump": [
                {
                    **_choice("climate.poolpump", "Poolpump", "heat"),
                    "score": 0.95, "ask": False, "shape": "climate", "switch_id": None, "number_id": None, "cloud": False,
                }
            ],
            "circulation": [{**_choice("switch.pool_cirkulation", "Pool cirkulation", "off"), "score": 0.5, "ask": True}],
            "water_sensor": [{**_choice("sensor.pool_vatten", "Pool vatten", "14.5 °C"), "score": 0.5, "ask": True}],
            "suggested": {
                "heat_pump": {"entity_id": "climate.poolpump", "shape": "climate", "switch_id": None, "number_id": None},
                "circulation_id": "switch.pool_cirkulation",
                "water_sensor": "sensor.pool_vatten",
                "ask": True,
            },
        },
        "spa": [
            {
                **_choice("climate.spa", "Spa", "heat"),
                "score": 1.0, "ask": False, "family": "balboa", "controllable": True, "cloud": False,
            }
        ],
        "sauna": [
            {
                **_choice("climate.harvia", "Bastu", "off"),
                "score": 1.0, "ask": False, "family": "harvia_sauna", "lock_id": "binary_sensor.harvia_door", "cloud": True,
            }
        ],
        "phones": [{"service": "mobile_app_pixel", "name": "Pixel"}],
    }
    upstairs = (
        {
            "enabled": True, "state": "running", "reason": "planned", "home_c": 22.0, "held_c": 21.0,
            "until": (MIDNIGHT + timedelta(hours=19)).isoformat(), "check": "passed", "action": "lower",
            "automation": None, "paused_until": None,
        }
        if more
        else None
    )
    return heat_view(
        replace(HeatViewInputs(
            site_id="site_villa",
            now=NOW,
            tz="Europe/Stockholm",
            writable=True,
            config=config,
            config_block=config_block(entry),  # type: ignore[arg-type]
            indoor=indoor(sensors, NOW),
            outdoor=Outdoor(6.2, "sensor.utomhus_garage_temperatur", "Utomhus garage temperatur", "house_forecast"),
            zones=(
                ZoneReading("climate.golvvarme_toalett_nere", "Golvvärme toalett nedervåning", "floor", 0.0, 19.5, 17.0, "off", False, 0.0),
                ZoneReading("climate.golvvarme_toalett_uppe", "Golvvärme toalett ovanvåning", "floor", 0.0, 21.5, 17.0, "heat", False, 0.0, control=upstairs),
                ZoneReading("climate.golvvarme_tvattstuga", "Golvvärme tvättstuga", "floor", -1.0, 20.4, 17.0, "heat", False, 0.0),
                ZoneReading("climate.ovanvaning", "Värmepump ovanvåning", "air", -0.5, 21.5, 21.0, "off", False, None),
            ),
            meter=MeterInputs(
                status="found",
                entity_id="sensor.varme_effekt",
                name="Elmätare värme (Nibe + panna) Effekt",
                source="setting",
                setting_offered=True,
                candidates=(),
                kw_now=1.13,
                last_period_kw=1.12,
                levels=found,
                split=split,
                periods={start: kw / 12.0 for start, kw in window.items() if start >= MIDNIGHT},
                zone_periods=villa_zone_periods() if zone_periods == "villa" else zone_periods,
            ),
            prices=_slots(),
            money=("SEK", "kr", "öre"),
            car_hours=car,
            choices=choices,
            tight=frozenset({MIDNIGHT + timedelta(hours=20)}),
            kept={
                MIDNIGHT + timedelta(hours=7): ("lower", "dear_hour"),
                MIDNIGHT + timedelta(hours=8): ("lower", "dear_hour"),
                MIDNIGHT + timedelta(hours=15): ("lower", "tight_hour"),
                MIDNIGHT + timedelta(hours=16): ("lower", "dear_hour"),
            },
            relay=villa_relay(),
            automations=villa_automations(),
            names={"climate.golvvarme_toalett_nere": "Golvvärme toalett nedervåning"},
            review=villa_review(),
            hot_water=villa_hot_water(),
            indoor_day=indoor_day(
                config, villa_rooms(), MIDNIGHT, MIDNIGHT + timedelta(days=1), NOW, LIVE_STEP_MIN,
                names={sensor.entity_id: sensor.name for sensor in sensors},
            ),
            control=control_status(
                "waiting",
                "automation",
                automation={"entity_id": "automation.varmepanna_sank_varmekurvan_nar_elen_ar_dyr", "name": "Värmepanna: sänk värmekurvan när elen är dyr"},
            ),
        ), **overrides)
    )


def _at(hours: float, day: int = 0) -> str:
    return (MIDNIGHT + timedelta(days=day, hours=hours)).isoformat()


def hot_water_gate(**changes: Any) -> dict[str, Any]:
    return {
        "level": "B", "days": 0, "days_needed": 7, "weekdays": 0, "peaks": 0, "peaks_needed": 10, "hits": 0,
        "hit_needed": 0.7, "saved": 0.0, "open": False, "backed_off": None, **changes,
    }


def villa_hot_water() -> dict[str, Any]:
    """The villa's hot water in shadow (`heat/dhw_runtime.HotWater.view`): a relay pulse on the pump's hot-water input
    started a run five minutes ago; the morning's would-be boost at 03:15 against the pump's own 05:10 run, the
    evening's at 14:00, tomorrow's morning at 03:30; day 5 of the gate's 7."""
    return {
        "boost": {"state": "running", "source": "user", "started": _at(17 + 15 / 60), "ends": _at(17.75), "ends_estimated": True, "stoppable": False},
        "peaks": [{"ready_by": "05:00", "kwh": 1.2, "learned": True}, {"ready_by": "17:00", "kwh": 1.0, "learned": False}],
        "planned": [
            {"start": _at(3.25), "end": _at(3.75), "kwh": 1.2, "price": 75.0, "reason": "cheap", "peak": "05:00", "live": False, "pump_run": _at(5 + 10 / 60)},
            {"start": _at(14), "end": _at(14.5), "kwh": 1.0, "price": 100.0, "reason": "cheap", "peak": "17:00", "live": False, "pump_run": None},
            {"start": _at(3.5, 1), "end": _at(4, 1), "kwh": 1.2, "price": 76.0, "reason": "cheap", "peak": "05:00", "live": False, "pump_run": None},
        ],
        "runs_today": [
            {"start": _at(5 + 10 / 60), "end": _at(5.5), "kwh": 1.05, "source": "pump"},
            {"start": _at(17 + 15 / 60), "end": _at(17.5), "kwh": 0.4, "source": "button"},
        ],
        "review": {
            "date": "2026-11-15", "mode": "shadow", "would_cost": 81.0, "actual_cost": 141.75, "saved": 60.75,
            "moved_kwh": 1.05, "hits": 1, "peaks": 2, "misses": 0, "cold_risks": 1, "blocks_cut": 2, "a_mode": "shadow",
            "per_peak": [
                {"peak": "05:00", "hit": True, "pick": _at(3.25, -1), "pump_run": _at(5 + 10 / 60, -1), "miss": False, "cold_risk": True},
                {"peak": "17:00", "hit": False, "pick": None, "pump_run": None, "miss": False, "cold_risk": False},
            ],
        },
        "gate": hot_water_gate(days=5, weekdays=5, peaks=10, hits=8, saved=212.5),
        "level": "B",
        "mode": "shadow",
        # The relay blocks the compressor, so hot water too: level A in shadow, day 5 of its 14; it would keep the
        # hour before the evening shower and tomorrow's before the morning one free of a block.
        "blocking": {
            "mode": "shadow",
            "gate": hot_water_gate(level="A", days=5, days_needed=14, weekdays=5, peaks=10, hits=8),
            "cuts": [
                {"start": _at(16), "end": _at(17), "reason": "peak", "peak": "17:00", "live": False},
                {"start": _at(4, 1), "end": _at(5, 1), "reason": "peak", "peak": "05:00", "live": False},
            ],
            "tank": False,
            "strict": False,
        },
    }


def network_pump_hot_water() -> dict[str, Any]:
    """A NIBE whose own price control is on: watch only; its temporary lux is idle and can be pressed."""
    return {
        "boost": {"state": "idle", "source": None, "started": None, "ends": None, "ends_estimated": False, "stoppable": False},
        "peaks": [{"ready_by": "05:00", "kwh": 1.0, "learned": False}, {"ready_by": "17:00", "kwh": 1.0, "learned": False}],
        "planned": [],
        "runs_today": [],
        "review": None,
        "gate": hot_water_gate(),
        "level": "B",
        "mode": "watch",
        "blocking": None,
    }


def control_status(state: str, reason: str | None, **extra: Any) -> dict[str, Any]:
    """The heat control's status as `HeatControl.status` answers it."""
    return {
        "enabled": state != "off",
        "state": state,
        "reason": reason,
        "automation": None,
        "check": None,
        "lowered": False,
        "since": None,
        "until": None,
        "paused_until": None,
        "raised": False,
        "raise_until": None,
        "path": "relay",
        "untested": False,
        "enable_entities": None,
        "offset_home": None,
        **extra,
    }


def controlled_answer() -> dict[str, Any]:
    """The villa with the heat control running: the relay is SpotNav's, lowered 07-09 and from 17:00 to 19:00."""
    minutes = {MIDNIGHT + timedelta(hours=h): (60, False) for h in (7, 8)}
    minutes[MIDNIGHT + timedelta(hours=17)] = (20, False)
    minutes[MIDNIGHT + timedelta(hours=18)] = (60, True)
    relay = RelayInputs(
        entity_id="switch.larm_utgang_1",
        name="Larm utgång 1",
        closed=True,
        since=MIDNIGHT + timedelta(hours=17),
        source="spotnav",
        automation=None,
        minutes=minutes,
        effect=Effect(0.46, 0.82, 7),
    )
    executed = {MIDNIGHT + timedelta(hours=7): 60, MIDNIGHT + timedelta(hours=8): 60, MIDNIGHT + timedelta(hours=17): 20}
    heater = {
        "entity_id": "switch.varmvattenberedare", "name": "Varmvattenberedare", "on": False, "metered": True, "meter_waiting": False,
        "mode": "metered", "kw": 3.0, "state": "running", "reason": "planned_off", "check": "passed",
        "last_full": (MIDNIGHT + timedelta(hours=4)).isoformat(), "until": (MIDNIGHT + timedelta(days=1, hours=1)).isoformat(),
        "windows": [
            {"start": (MIDNIGHT + timedelta(days=1, hours=1)).isoformat(), "end": (MIDNIGHT + timedelta(days=1, hours=4)).isoformat(), "reason": "cheap"},
            {"start": (MIDNIGHT + timedelta(days=1, hours=13)).isoformat(), "end": (MIDNIGHT + timedelta(days=1, hours=15)).isoformat(), "reason": "cheap"},
        ],
        "live": True, "automation": None, "paused_until": None,
    }
    pool = {
        "season_open": True, "water_c": 24.6, "water_source": "heat_pump",
        "water_at": (MIDNIGHT + timedelta(hours=7, minutes=40)).isoformat(), "target_c": 27.0,
        "heat_pump": {
            "on": True, "heating": False, "kw": 1.6, "kw_source": "entered", "live": False, "state": "watched", "reason": "waiting", "paused_until": None,
            "failures": 0,
        },
        # The circulation SpotNav controls (its opt-in on): one write did not read back once.
        "circulation": {
            "on": False, "kw": 0.42, "kw_source": "learned", "live": True, "state": "controlled", "reason": "waiting", "paused_until": None, "failures": 1,
        },
        "today": {"need_h": 8.25, "need_source": "water", "done_h": 3.0, "planned_h": 4.0, "heat_h": 2.0, "kwh": None},
        "planned": [
            {"start": (MIDNIGHT + timedelta(hours=22)).isoformat(), "end": (MIDNIGHT + timedelta(days=1, hours=2)).isoformat(), "what": "filter", "reason": "cheap"},
            {"start": (MIDNIGHT + timedelta(days=1, hours=12)).isoformat(), "end": (MIDNIGHT + timedelta(days=1, hours=14)).isoformat(), "what": "heat", "reason": "sun"},
        ],
        "extra": None, "season_hint": False,
        "would": {"action": "off", "until": (MIDNIGHT + timedelta(hours=22)).isoformat(), "reason": "waiting"},
        "notes": [],
    }
    spa = {
        "family": "balboa", "water_c": 36.5, "bath_c": 38.0, "idle_c": 30.0, "floor_c": 10.0, "heating": True, "kw": 3.0, "kw_source": "entered",
        "rate_c_h": 2.0, "rate_learned": True, "state": "heating", "ready_at": (MIDNIGHT + timedelta(hours=18, minutes=40)).isoformat(),
        "next_bath": {"at": (MIDNIGHT + timedelta(hours=19)).isoformat(), "source": "weekly"},
        "baths": [{"weekdays": [5, 6], "time": "19:00"}], "bath_once": None, "bath_now": None, "live": False,
        "control": {"state": "watched", "reason": None, "paused_until": None, "failures": 0},
        "would": {"action": "heat", "until": (MIDNIGHT + timedelta(hours=18, minutes=45)).isoformat(), "reason": "cheap"},
        "not_ready": None,
        "planned": [
            {"start": (MIDNIGHT + timedelta(hours=17)).isoformat(), "end": (MIDNIGHT + timedelta(hours=18, minutes=45)).isoformat(), "what": "heat", "reason": "cheap"},
            {"start": (MIDNIGHT + timedelta(hours=18, minutes=45)).isoformat(), "end": (MIDNIGHT + timedelta(hours=21)).isoformat(), "what": "hold", "reason": "bath"},
        ],
        "notes": [],
    }
    sauna = {
        "family": "relay", "temp_c": None, "target_c": 80.0, "heating": False, "kw": 9.0, "kw_source": "entered", "rate_c_h": 80.0,
        "rate_learned": False, "state": "idle", "ready_at": None, "next_session": None, "sessions": [], "session_once": None,
        "session_now": None, "locked": None, "lock_id": None, "safety_declared": False, "live": False,
        "control": {"state": "watch_only", "reason": "no_safety", "paused_until": None, "failures": 0},
        "would": {"action": "off", "until": None, "reason": "no_session"},
        "not_ready": {
            "at": (MIDNIGHT + timedelta(days=1, hours=17)).isoformat(), "cause": "fuse", "reach_c": 60.0, "short_c": 20.0,
            "short_kwh": 2.25, "later": (MIDNIGHT + timedelta(days=1, hours=18)).isoformat(),
        },
        "planned": [],
        "notes": [],
    }
    return villa_answer(
        more=True,
        pool=pool,
        spa=spa,
        sauna=sauna,
        water_heater=heater,
        controlled=frozenset({"climate.golvvarme_toalett_uppe", "switch.varmvattenberedare"}),
        relay=relay,
        automations=(),
        control=control_status(
            "running",
            "planned",
            check="passed",
            lowered=True,
            since=(MIDNIGHT + timedelta(hours=17)).isoformat(),
            until=(MIDNIGHT + timedelta(hours=19)).isoformat(),
        ),
        executed=executed,
    )


def network_pump_answer() -> dict[str, Any]:
    config = HeatConfig(target_c=20.5, mode="away", away_until=(MIDNIGHT + timedelta(days=4, hours=16)).isoformat())
    pump = HeatPumpFacts(
        integration="nibe_heatpump",
        brand="nibe",
        label="NIBE F750",
        anchor_entity_id="sensor.nibe_bt1_outdoor_temperature",
        device_id="device_nibe",
        outdoor_temp_c=4.5,
        supply_temp_c=34.0,
        hot_water_temp_c=48.5,
        compressor_running=True,
        priority="heating",
        price_control=PriceControl("active", "smart_price_adaption_on", ("switch.nibe_smart_price_adaption",)),
        control="watch_only",
        control_reason="price_control",
        capabilities=(
            CapabilityStatus("offset", "number.nibe_heating_offset_s1", "number.set_value", True, True, "0", -10, 10, 1),
            CapabilityStatus("hot_water_boost", "switch.nibe_temporary_lux", "switch.turn_on", True, True, "off"),
        ),
        missing={"degree_minutes": "disabled", "room_temp": "not_found"},
    )
    return heat_view(
        HeatViewInputs(
            site_id="site_nibe",
            now=NOW,
            tz="Europe/Stockholm",
            writable=False,
            config=config,
            config_block=config_block(_Entry({"heat": config.as_dict()})),  # type: ignore[arg-type]
            indoor=indoor([], NOW),
            outdoor=Outdoor(4.5, "sensor.nibe_bt1_outdoor_temperature", "NIBE BT1", "pump"),
            pump=pump,
            other_controllers=("effektguard",),
            prices=_slots(),
            money=("SEK", "kr", "öre"),
            control=control_status("off", None),
            hot_water=network_pump_hot_water(),
        )
    )


def sg_api_pump_answer() -> dict[str, Any]:
    """A NIBE S series pump whose SG Ready API is chosen as the control path, still off: the two coils are disabled
    in the integration (the opt-in's confirm enables them), and the path has not run on a real pump yet."""
    mode, switch = "number.s1155_requested_operating_mode_sg_ready_46009", "switch.s1155_activate_sg_ready_via_api_43033"
    config = HeatConfig(control_path="sg_ready_api", control_entity=mode, control_entity_b=switch)
    pump = HeatPumpFacts(
        integration="nibe_heatpump",
        brand="nibe_s",
        label="NIBE S series (local)",
        anchor_entity_id="sensor.s1155_current_outdoor_temperature_bt1_30002",
        device_id="device_s1155",
        outdoor_temp_c=4.5,
        supply_temp_c=34.0,
        calc_supply_temp_c=35.5,
        compressor_running=True,
        priority="heating",
        price_control=PriceControl("off", "off", ("switch.s1155_activated_smart_price_adaption_40844",)),
        capabilities=(
            CapabilityStatus("offset", "number.s1155_heating_offset_climate_system_1_40031", "number", True, True, "0", -10, 10, 1),
            CapabilityStatus("sg_ready", switch, "switch", False, False),
            CapabilityStatus("sg_ready", mode, "number", False, False, None, 0, 3, 1),
        ),
    )
    return heat_view(
        HeatViewInputs(
            site_id="site_nibe_s",
            now=NOW,
            tz="Europe/Stockholm",
            writable=True,
            config=config,
            config_block=config_block(_Entry({"heat": config.as_dict()})),  # type: ignore[arg-type]
            indoor=indoor([], NOW),
            outdoor=Outdoor(4.5, "sensor.s1155_current_outdoor_temperature_bt1_30002", "BT1", "pump"),
            pump=pump,
            prices=_slots(),
            money=("SEK", "kr", "öre"),
            choices={
                "indoor": [], "outdoor": [], "zones": [], "setpoint": [], "hot_water": [], "meter": None, "water_heater": [], "tank_sensor": [],
                "pool": None, "spa": [], "sauna": [], "phones": [],
                "offset": [{"entity_id": "number.s1155_heating_offset_climate_system_1_40031", "name": "Heating offset climate system 1", "value": "0"}],
                "relay": [
                    {"entity_id": mode, "name": "Requested operating mode (SG Ready)", "value": None},
                    {"entity_id": switch, "name": "Activate SG Ready via API", "value": None},
                ],
            },
            control=control_status(
                "off",
                None,
                enabled=False,
                path="sg_ready_api",
                untested=True,
                enable_entities={
                    "integration": "S1155",
                    "entities": [
                        {"entity_id": switch, "name": "Activate SG Ready via API"},
                        {"entity_id": mode, "name": "Requested operating mode (SG Ready)"},
                    ],
                },
            ),
        )
    )


def test_the_pump_with_the_sg_ready_api() -> None:
    answer = sg_api_pump_answer()
    paths = answer["pump"]["paths"]
    assert [item["path"] for item in paths] == ["sg_ready_api", "offset"]
    assert paths[0]["control_entity"] == answer["config"]["control_entity"] and paths[0]["control_entity_b"] == answer["config"]["control_entity_b"]
    assert answer["control"]["path"] == "sg_ready_api" and answer["control"]["untested"] is True
    assert len(answer["control"]["enable_entities"]["entities"]) == 2
    assert answer["shadow"] is True and answer["relay"] is None
    _write_or_compare("sg_api_pump.json", answer)


def test_the_villa() -> None:
    answer = villa_answer()
    assert tuple(answer) == HEAT_KEYS
    assert all(tuple(hour) == BAND_HOUR_KEYS for hour in answer["band"]["hours"])
    assert answer["comfort"] == {"target_c": 21.0, "low_c": 20.5, "high_c": 21.5, "mode": "home", "away_until": None, "aim_c": 20.5}
    # Upstairs reads 20.5 but aims a degree lower: the toilet at 20.6 is the room closest to too cold.
    indoor_block = answer["indoor"]
    assert (indoor_block["value_c"], indoor_block["relative_c"], indoor_block["coldest"]) == (20.6, 20.6, "sensor.toalett_nv_temperatur")
    assert [sensor["offset_c"] for sensor in indoor_block["sensors"]] == [0.0, -1.0, 0.0]
    assert answer["config"]["indoor_offsets"] == [{"entity_id": "sensor.ovanvaning_temperatur", "offset_c": -1.0}]
    # 17:20 lies in the evening's dearest hours: lowered to the band's low edge until 19:00 (three in a row at most).
    would = answer["status"]["would"]
    assert (would["action"], would["reason"]) == ("lower", "dear_hour")
    assert would["until"] == (MIDNIGHT + timedelta(hours=19)).isoformat()
    band_hours = answer["band"]["hours"]
    # 20:00 is tight in the joint plan (the car at the import cap): lowered after an hour back to normal.
    assert [band_hours[h]["action"] for h in (18, 19, 20)] == ["lower", "normal", "lower"]
    assert band_hours[20]["reason"] == "tight_hour"
    # 15:00 is past: it shows what was planned when it began, not the plan made now.
    assert (band_hours[15]["action"], band_hours[15]["reason"], band_hours[15]["target_c"]) == ("lower", "tight_hour", 20.5)
    # The relay beside the plan: closed 07-09 and since 16:00, expected to open at 19:00.
    assert [band_hours[h]["relay_min"] for h in (7, 8, 9, 16, 17, 18, 19)] == [60, 60, 0, 60, 60, 60, 0]
    assert [band_hours[h]["relay_expected"] for h in (16, 17, 18)] == [False, True, True]
    assert answer["relay"]["source"] == "blueprint" and answer["relay"]["closed"] is True
    assert answer["relay"]["effect"] == {"lower_share": 0.46, "rebound": 0.82, "blocks": 7}
    review = answer["review"]
    assert review["relay"]["measured"] == 1 and review["plan"]["overlap_hours"] == 2.0
    assert review["day"] == {"coldest_min_c": 20.4, "minutes_below": 20}
    assert answer["status"]["pump_state"] == "heating" and answer["status"]["pump_state_source"] == "meter"
    actions = [hour["action"] for hour in answer["band"]["hours"]]
    assert actions[0] == "night" and actions[23] == "night"
    assert [hour["car"] for hour in answer["band"]["hours"]][1:5] == [True] * 4
    hot = [hour["start"] for hour in answer["band"]["hours"] if hour["hot_water"]]
    # The measured runs at 05 and 07, the planned boosts at 03 and 14 (tomorrow's is not today's).
    assert hot == [(MIDNIGHT + timedelta(hours=h)).isoformat() for h in (3, 5, 7, 14)]
    assert "hot_water_pick" not in answer["band"], "the first planned boost is `hot_water.planned`'s"
    assert answer["hot_water"]["gate"]["days"] == 5
    today = answer["today"]
    assert today["hot_water_kwh"] > 1.5 and today["cost"] > 0 and today["money"]["minor_unit"] == "öre"
    # Heat holds the zones' own meters (6 hours at 0.6 kW); hot water stays the heat pump meter's own split.
    alone = villa_answer(zone_periods=None)["today"]
    assert (today["zones_kwh"], alone["zones_kwh"]) == (3.6, None)
    assert today["heat_kwh"] == pytest.approx(alone["heat_kwh"] + 3.6, abs=0.001)
    assert today["total_kwh"] == pytest.approx(alone["total_kwh"] + 3.6, abs=0.001)
    assert today["hot_water_kwh"] == alone["hot_water_kwh"] and today["cost"] > alone["cost"]
    # The zones aim at the house's aim plus their own offset.
    assert [zone["aim_c"] for zone in answer["zones"]] == [20.5, 20.5, 19.5, 20.0]
    # One note per automation that references a chosen relay or zone; the relay's own blueprint says so.
    assert [note["code"] for note in answer["notes"]] == ["automation_steers", "automation_steers"]
    assert answer["notes"][0]["params"]["targets"] == ["Golvvärme toalett nedervåning"]
    assert answer["notes"][0]["params"]["relay"] is False and answer["notes"][1]["params"]["relay"] is True
    assert answer["pump"] is None
    # SpotNav's heat control is on but waits for the household's automation on the relay: that note says so.
    assert answer["shadow"] is True and answer["control"]["state"] == "waiting"
    assert answer["control"]["reason"] == "automation"
    assert answer["notes"][1]["params"]["blocks"] is True and answer["notes"][0]["params"]["blocks"] is False
    assert all(hour["executed_min"] == 0 for hour in band_hours)
    _write_or_compare("villa.json", answer)


def test_the_villa_controlled_by_spotnav() -> None:
    answer = controlled_answer()
    assert tuple(answer) == HEAT_KEYS
    assert answer["shadow"] is False and answer["control"]["state"] == "running" and answer["control"]["lowered"] is True
    assert answer["relay"]["source"] == "spotnav" and answer["notes"] == []
    # A zone's thermostat and the water heater are SpotNav's too.
    assert answer["zones"][1]["control"]["held_c"] == 21.0 and answer["zones"][0]["control"] is None
    assert answer["water_heater"]["live"] is True and answer["config"]["water_heater"]["control"] is True
    band_hours = answer["band"]["hours"]
    assert [band_hours[h]["executed_min"] for h in (6, 7, 8, 9, 17, 18)] == [0, 60, 60, 0, 20, 0]
    _write_or_compare("controlled.json", answer)


def test_the_network_pump_and_its_notes() -> None:
    answer = network_pump_answer()
    assert answer["choices"] is None and answer["writable"] is False
    assert [note["code"] for note in answer["notes"]] == ["pump_price_control", "readings_disabled", "other_controller"]
    assert answer["notes"][0]["params"] == {"label": "NIBE F750", "where": "nibe_smart_price"}
    assert answer["notes"][1]["params"] == {"entities": ["degree_minutes"], "device_id": "device_nibe"}
    assert answer["pump"]["capabilities"] == ["hot_water_boost", "offset"]
    assert answer["status"]["pump_state"] == "heating" and answer["status"]["pump_state_source"] == "pump"
    assert answer["status"]["would"]["action"] == "away"
    assert answer["comfort"]["aim_c"] == 17.5 and answer["today"] is None and answer["meter"] is None
    assert answer["relay"] is None and answer["review"] is None
    assert all(hour["relay_min"] is None and hour["relay_expected"] is False for hour in answer["band"]["hours"])
    # No relay path: the heat control was never turned on.
    assert answer["control"]["state"] == "off" and answer["shadow"] is True
    _write_or_compare("network_pump.json", answer)


def test_the_villa_has_its_indoor_curve_up_to_now() -> None:
    """The day's rooms by the quarter hour up to now, upstairs on the house's scale (offset -1), the lowered morning
    showing as a dip; the band's hours carry the setpoint, so the curve has none of its own."""
    curve = villa_answer()["indoor_day"]
    assert curve["step_min"] == 15 and curve["setpoint"] is None
    assert (curve["low_c"], curve["high_c"]) == (20.5, 21.5)
    assert len(curve["points"]) == 17 * 4 + 1
    assert all(point["start"] < NOW.isoformat() for point in curve["points"])
    before, lowered = curve["points"][0], curve["points"][7 * 4]
    assert (before["mean_c"], before["coldest_c"]) == (21.17, 20.7)
    assert lowered["coldest_c"] == 20.7 and lowered["mean_c"] < before["mean_c"]
    assert before["coldest"] == "sensor.toalett_nv_temperatur"
    assert curve["rooms"][0] == {"entity_id": "sensor.inomhus_kok_temperatur", "name": "Inomhus kök temperatur"}
    assert villa_answer(indoor_day=None)["indoor_day"] is None


def _two_days(drop: tuple[int, ...] = ()) -> tuple[tuple[datetime, datetime, float], ...]:
    """Today's prices and tomorrow's, but for the hours of tomorrow in `drop`."""
    tomorrow = tuple(
        (start + timedelta(days=1), end + timedelta(days=1), price)
        for start, end, price in _slots()
        if (start - MIDNIGHT).seconds // 3600 not in drop
    )
    return _slots() + tomorrow


def test_the_hours_ahead_are_a_rolling_day_across_midnight() -> None:
    """The strip under the dial: the plan from the running hour on, 24 hours. Today's hours are the band's own,
    tomorrow's follow, priced or not."""
    answer = villa_answer()
    ahead = answer["ahead"]
    assert all(tuple(hour) == AHEAD_HOUR_KEYS for hour in ahead)
    band_ahead = [hour for hour in answer["band"]["hours"] if not hour["past"]]
    assert [hour["start"] for hour in ahead[:7]] == [hour["start"] for hour in band_ahead]
    assert [(hour["action"], hour["target_c"]) for hour in ahead[:7]] == [(hour["action"], hour["target_c"]) for hour in band_ahead]
    # Late evening and no price for tomorrow: still 24 hours, tomorrow's planned as the plan has them (normal, and
    # the cooler night, which needs no price).
    assert len(ahead) == 24 and ahead[0]["start"] == (MIDNIGHT + timedelta(hours=17)).isoformat()
    assert [hour["start"] for hour in ahead] == [(MIDNIGHT + timedelta(hours=17 + index)).isoformat() for index in range(24)]
    assert [hour["action"] for hour in ahead[7:13]] == ["night"] * 6 and ahead[13]["action"] == "normal"
    assert {hour["action"] for hour in ahead[14:]} == {"normal"}


def test_the_hours_ahead_cross_midnight_with_tomorrow_planned_and_a_price_missing() -> None:
    """With tomorrow priced the strip plans it by the prices; a missing price hour stays in the strip, unlowered."""
    full = villa_answer(prices=_two_days())["ahead"]
    assert len(full) == 24
    assert [hour["start"] for hour in full] == [(MIDNIGHT + timedelta(hours=17 + index)).isoformat() for index in range(24)]
    # The cooler night runs on past midnight; tomorrow's hot water is its planned boost at 03:30.
    assert full[7]["action"] == "night"
    assert [hour["hot_water"] for hour in full[7:12]] == [False, False, False, True, False]
    assert "lower" in {hour["action"] for hour in full[13:]}
    # Tomorrow's 08:00 is a dear hour; without its price it is not lowered, and no hour goes missing.
    gap = villa_answer(prices=_two_days(drop=(8,)))["ahead"]
    assert len(gap) == 24 and [hour["start"] for hour in gap] == [hour["start"] for hour in full]
    at = (MIDNIGHT + timedelta(hours=24 + 8)).isoformat()
    assert next(hour for hour in full if hour["start"] == at)["action"] == "lower"
    assert next(hour for hour in gap if hour["start"] == at)["action"] == "normal"


def test_the_hours_ahead_in_the_late_evening_show_tomorrow() -> None:
    """At 23:20 the strip holds this hour and a whole of tomorrow."""
    late = MIDNIGHT + timedelta(hours=23, minutes=20)
    ahead = villa_answer(now=late, prices=_two_days())["ahead"]
    assert len(ahead) == 24
    assert ahead[0]["start"] == (MIDNIGHT + timedelta(hours=23)).isoformat()
    assert ahead[-1]["start"] == (MIDNIGHT + timedelta(hours=46)).isoformat()
    assert ahead[1]["start"] == (MIDNIGHT + timedelta(hours=24)).isoformat()


def test_an_automation_on_a_controlled_zone_blocks_it_and_the_water_heater_s_note() -> None:
    from custom_components.spotnav.heat.view import notes

    automation = SteeringAutomation("automation.golv", "Golv", ("climate.golvvarme_toalett_uppe",))
    out = notes(None, (), (automation,), "switch.larm_utgang_1", controlled=frozenset({"climate.golvvarme_toalett_uppe"}))
    assert out[0]["params"]["blocks"] is True and out[0]["params"]["relay"] is False
    assert notes(None, (), (automation,), "switch.larm_utgang_1")[0]["params"]["blocks"] is False
    heater = {"entity_id": "switch.varmvattenberedare", "name": "Varmvattenberedare"}
    assert notes(None, (), (), None, water_heater=heater) == [
        {"code": "water_heater_not_full", "params": {"name": "Varmvattenberedare", "entity_id": "switch.varmvattenberedare"}}
    ]
