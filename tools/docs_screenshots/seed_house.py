"""The made-up house's measured hours, for the screenshot tool's throwaway Home Assistant.

`ha_launch.py` calls `install()`: when a site loads its house-load history and has none, it is given the hours of
the demo house's simulation (`demo_components/demo_house/model.py`), so the house forecast, the battery plan and
the heat plan have what a few weeks of running would have given them. In memory only; the integration is untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _patch() -> None:
    from custom_components.spotnav.site.house_history import HouseHistory

    if getattr(HouseHistory, "_docs_seeded", False):
        return
    HouseHistory._docs_seeded = True
    original = HouseHistory.restore

    def restore(self, raw):
        original(self, raw)
        if self.gross:
            return
        from custom_components.demo_house import model

        m = model.get()
        now = datetime.now(timezone.utc)
        for a in range(0, m.n - 12, 12):
            start = m.t[a]
            if start + (m.t[1] - m.t[0]) * 12 > now:
                break
            hour = range(a, a + 12)
            house = sum(m.house[i] + m.heat[i] for i in hour) / 12
            grid = sum(m.grid[i] for i in hour) / 12
            outdoor = sum(m.t_out[i] for i in hour) / 12
            self.rows[start] = (max(grid, 0.0), round(outdoor, 1))
            self.gross[start] = (house, True)
        self.migrated_at = self.gross_migrated_at = now

    HouseHistory.restore = restore


def _hot_water(result):
    """The site's heat store with the hot water's measured runs and ten shadow days (reviews), as a few weeks of
    watching would have left them: the peaks are learned from the runs and the shadow progress line has its days."""
    from datetime import timedelta

    from custom_components.demo_house import model

    m = model.get()
    now = datetime.now(timezone.utc)
    today = now.astimezone(model.ZONE).date()
    runs = [r for r in m.runs if r[1] <= now and r[0] >= now - timedelta(days=14)]
    days = {}
    for back in range(1, 6):
        day = today - timedelta(days=back)
        hits = 2 if back != 3 else 1
        days[day.isoformat()] = {"review": {
            "date": day.isoformat(), "mode": "shadow", "would_cost": 38.2 + back, "actual_cost": 44.9 + back,
            "saved": round(6.7 - 0.3 * back, 2), "moved_kwh": 1.6, "hits": hits, "peaks": 2, "misses": 0,
            "cold_risks": 0, "blocks_cut": 0, "a_mode": "shadow", "per_peak": []}}
    body = dict(result or {})
    body["hot_water"] = {
        "runs": [{"start": a.isoformat(), "end": b.isoformat(), "kwh": round(k, 3)} for a, b, k in runs],
        "seen": [(today - timedelta(days=d)).isoformat() for d in range(1, 15)],
        "days": days,
    }
    return body


def _battery_plan(result):
    """The battery plan's store with a month of reviewed days (the plan's day against the battery's own way and
    against self-consumption) and the battery's day money, as a month in test mode would have left them: the battery
    ran under its own control, SpotNav only worked out what its plan would have done."""
    import math
    from datetime import timedelta

    from custom_components.demo_house import model

    now = datetime.now(timezone.utc)
    today = now.astimezone(model.ZONE).date()
    body = dict(result or {})
    reviews, days = [], {}
    for back in range(1, 30):
        day = today - timedelta(days=back)
        wave = 0.5 + 0.5 * math.sin(back * 1.7)
        base = 3900 + 700 * math.sin(back * 0.9)
        own = 170 + 230 * wave
        extra = 90 + 130 * (1 - wave)
        run = lambda cost: {"cost": round(cost, 2), "end_soc_pct": 40.0, "net": round(cost, 2)}  # noqa: E731
        reviews.append({
            "date": day.isoformat(), "reviewed_at": (datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=model.ZONE) + timedelta(hours=3)).isoformat(),
            "quarters": 96, "covered": 96, "unplanned": 0, "start_soc_pct": 40.0, "end_value": 80.0, "usable_kwh": 15.0,
            "actual": run(base - own), "mode": "balanced", "plan": run(base - own - extra),
            "modes": {"self_consumption": run(base - own + 40), "balanced": run(base - own - extra),
                      "max_profit": run(base - own - extra - 30)},
            "self_consumption": run(base), "soc_mae": 4.1,
            "curtail": {"kwh": 0.0, "saving": 0.0},
            "baselines": {"no_battery_cost": round(base + 520 + 90 * wave, 2), "sun_gave": 0.0, "sun_quarters": None},
        })
        days[day.isoformat()] = {"sold": 0.0, "bought": round(base, 2), "arbitrage": round(own, 2),
                                  "battery_export_kwh": 0.0, "quarters": 96}
    body["reviews"] = reviews
    body["stored_value"] = {"kwh": 9.6, "cost": 760.0, "estimated": False, "last": now.isoformat(), "days": days}
    return body


#: The recorded day's spot prices (EUR/kWh a quarter hour), read once at install (not on the event loop).
_SPOT: list[float] = []


def _decision_ledger(hass, result):
    """The site's decision history (the Plan tab's days gone by) with the hours before the first one recorded, from
    three days back: per hour the demo house's measured means, the prices as the plan had them (the recorded day of
    `nordpool`, the same every day: sell = spot, buy = spot with VAT and fees), the fuse as the ceiling, and per load
    what the plan said at the hour's start and what ran. The car (controlled) charges 01-04 at night and once took
    less than planned (`car_limited`); the battery and the heat are only watched (test mode), as in the demo site."""
    from datetime import timedelta

    from custom_components.demo_house import model
    from custom_components.spotnav.site.decision_ledger import (
        HINT_CAR_LIMITED, HourRow, Ledger, LoadRow, Planned, hour_number)

    try:
        ledger = Ledger.from_store(result)
    except Exception:  # noqa: BLE001 - nothing readable: start from the seed alone
        ledger = Ledger()
    garage = next((e.entry_id for e in hass.config_entries.async_entries("spotnav") if e.title == "Garage"), None)
    m = model.get()
    now = datetime.now(timezone.utc)
    running = hour_number(now)
    first = min([*ledger.hours, ledger.open.hour if ledger.open is not None else running, running])
    spot = _SPOT
    today = now.astimezone(model.ZONE).date()
    start = datetime.combine(today - timedelta(days=3), datetime.min.time(), tzinfo=model.ZONE).astimezone(timezone.utc)
    hour = start
    while hour_number(hour) < first:
        local = hour.astimezone(model.ZONE)
        h = local.hour
        back = (today - local.date()).days
        quarter = spot[h * 4:h * 4 + 4]
        sell = round(sum(quarter) / len(quarter) * 1129, 2)
        price = round(sell * 1.25 + 82.5, 2)
        lo = m.index(hour)
        span = range(lo, min(lo + 12, m.n))
        mean = lambda series, span=span: sum(series[i] for i in span) / len(span)  # noqa: E731
        sun, heat, batt = mean(m.sun), mean(m.heat), mean(m.batt)
        house = mean(m.house) + heat
        car_plan = 6.93 if 1 <= h < 4 else None
        car_ran = None if car_plan is None else (3.2 if back == 1 and h == 3 else 6.9)
        grid = house + batt - sun + (car_ran or 0.0)
        planned_net = house + batt - sun + (car_plan or 0.0)
        number = hour_number(hour)
        rows = []
        if garage is not None:
            rows.append(LoadRow(
                number, garage, "car", "Garage", False,
                Planned("charge", car_plan, None) if car_plan else Planned(None, None, None),
                "charge" if car_ran else None, car_ran,
                HINT_CAR_LIMITED if car_ran is not None and car_ran < car_plan - 1 else None, None))
        if batt > 0.1:
            what = "grid_charge" if 2 <= h < 6 and sun < 0.5 else "sun_charge"
        elif batt < -0.1:
            what = "cover"
        else:
            what = None
        rows.append(LoadRow(
            number, "sensor.sigen_plant_ess_power", "home_battery", "Sigenergy", True,
            Planned(what, round(batt, 2) if what else None, None), what, round(batt, 2), None, None))
        if h in (7, 8, 17, 18, 19):
            heat_plan = Planned("lower", None, "dear_hour", -1)
        elif h in (5, 16):
            heat_plan = Planned("preheat", None, "cheap_before_dear", 1)
        else:
            heat_plan = Planned(None, None, None, 0)
        rows.append(LoadRow(
            number, "switch.heat_pump_curve_relay", "heat", "Heat pump Curve relay", True, heat_plan, None,
            round(heat, 2), None, None))
        ledger.keep(HourRow(number, 60, 0, price, sell, round(grid, 2), round(house - sun, 2), 17.32, "fuse",
                            round(planned_net, 2)), rows)
        hour += timedelta(hours=1)
    return ledger.as_store()


def install() -> None:
    """Patch the integration's house-history restore once a site's load-forecast store is read, and give the heat
    store its hot-water shadow."""
    import json
    import os

    from homeassistant.helpers import storage

    with open(os.environ["DOCS_PRICE_FIXTURE"], encoding="utf-8") as handle:
        _SPOT[:] = json.load(handle)["prices"]
    original = storage.Store.async_load

    async def load(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        key = str(getattr(self, "key", ""))
        if key.startswith("spotnav.load_forecast."):
            _patch()
        elif key.startswith("spotnav.battery_plan.") and not (result or {}).get("reviews"):
            result = _battery_plan(result)
        elif key.startswith("spotnav.heat_shadow.") and not (result or {}).get("hot_water"):
            result = _hot_water(result)
        elif key.startswith("spotnav.decision_ledger."):
            try:
                result = _decision_ledger(self.hass, result)
            except Exception:  # noqa: BLE001 - a seed that fails leaves the history as it was
                import logging

                logging.getLogger(__name__).exception("docs: the decision history seed failed")
        return result

    storage.Store.async_load = load
