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


def install() -> None:
    """Patch the integration's house-history restore once a site's load-forecast store is read, and give the heat
    store its hot-water shadow."""
    from homeassistant.helpers import storage

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
        return result

    storage.Store.async_load = load
