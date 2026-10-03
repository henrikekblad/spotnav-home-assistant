"""Synthetic charge history for the screenshot tool's throwaway Home Assistant.

`ha_launch.py` calls `install()`: once the integration loads its session store, a charger the store has
no sessions for is given the sessions below (about 75 days back from now, none of them real), so the
charge history dialog has a month to show. Nothing is written to disk and the integration is untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Stockholm")
FX = 11.0  # SEK per EUR
# Raw spot price in EUR per kWh by local hour: cheap at night, dearer in the morning and evening.
HOURLY = [0.045, 0.040, 0.038, 0.037, 0.042, 0.055, 0.085, 0.120, 0.110, 0.090, 0.080, 0.075,
          0.070, 0.070, 0.075, 0.085, 0.100, 0.130, 0.140, 0.115, 0.090, 0.070, 0.055, 0.048]
CAUSES = ["plan_window", "plan_window", "plan_window", "manual", "plan_window", "plan_window", "other"]


def sessions_for(charger_id: str, now: datetime | None = None) -> list:
    from custom_components.spotnav.sessions.costing import Slice
    from custom_components.spotnav.sessions.model import ChargeSession

    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZONE).date()
    out = []
    for back in range(1, 76):
        if back % 3 == 1 or back % 7 == 0:
            continue
        day = today - timedelta(days=back)
        start_local = datetime(day.year, day.month, day.day, 21 + back % 3, (back * 7) % 4 * 15, tzinfo=ZONE)
        hours = 3 + back % 5
        end_local = start_local + timedelta(hours=hours)
        kwh = round(hours * (10.2 + (back % 4) * 0.3) * (0.8 + (back % 3) * 0.1), 2)
        factor = 0.85 + (back * 37 % 30) / 100
        slices = []
        cursor = start_local.astimezone(timezone.utc)
        end = end_local.astimezone(timezone.utc)
        total = (end - cursor).total_seconds()
        while cursor < end:
            local = cursor.astimezone(ZONE)
            boundary = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(timezone.utc)
            stop = min(boundary, end)
            slices.append(Slice(
                boundary - timedelta(hours=1), boundary, kwh * (stop - cursor).total_seconds() / total,
                round(HOURLY[local.hour] * factor, 4), FX, local.date(), 0.085 * factor,
            ))
            cursor = stop
        out.append(ChargeSession(
            id=f"demo-{back}", charger_id=charger_id, start=start_local.astimezone(timezone.utc),
            end=end, energy_kwh=kwh, energy_source="register", priced_kwh=0.0, cost_minor=None,
            reference_cost_minor=None, currency="SEK", major_unit="kr", minor_unit="öre",
            started_by=CAUSES[back % len(CAUSES)], strategy=None, vehicle_id=None, vehicle_name="Family car",
            solar_kwh=0.0, solar_known_kwh=0.0, last_register_kwh=None, last_sample_at=None,
            intervals=tuple(slices),
        ))
    return out


def install() -> None:
    """Patch the session store's reads (once the integration has loaded it) to seed unknown chargers."""
    from homeassistant.helpers import storage

    original = storage.Store.async_load

    async def load(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        if getattr(self, "key", "") == "spotnav_sessions":
            from custom_components.spotnav.sessions import store as module

            cls = module.SessionStore
            if not getattr(cls, "_docs_seeded", False):
                cls._docs_seeded = True
                for name in ("closed", "closed_raw"):
                    inner = getattr(cls, name)

                    def wrapped(self, charger_id, _inner=inner):
                        if charger_id not in self._closed:
                            self._closed[charger_id] = sessions_for(charger_id)
                        return _inner(self, charger_id)

                    setattr(cls, name, wrapped)
        return result

    storage.Store.async_load = load
