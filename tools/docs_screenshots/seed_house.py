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
        elif key.startswith("spotnav.heat_shadow.") and not (result or {}).get("hot_water"):
            result = _hot_water(result)
        return result

    storage.Store.async_load = load
