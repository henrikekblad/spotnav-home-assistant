"""Builders for the charge-session tests: priced intervals and finished sessions, without a charger."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.spotnav.planning.planner import ChartInterval
from custom_components.spotnav.sessions.model import ChargeSession, SOURCE_REGISTER, STARTED_OTHER

UTC = timezone.utc
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def day_intervals(
    day: date, prices: list[float], *, minutes: int = 60, zone: ZoneInfo = STOCKHOLM
) -> list[ChartInterval]:
    """One day's effective prices (minor unit per kWh), `minutes` apart from local midnight."""
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)
    rows = []
    for index, price in enumerate(prices):
        utc_start = start + timedelta(minutes=minutes * index)
        utc_end = utc_start + timedelta(minutes=minutes)
        rows.append(
            ChartInterval(
                start=utc_start.astimezone(zone),
                end=utc_end.astimezone(zone),
                utc_start=utc_start,
                utc_end=utc_end,
                day=day,
                raw_minor_per_kwh=price,
                effective_minor_per_kwh=price,
            )
        )
    return rows


def session(
    start: datetime,
    *,
    hours: float = 1.0,
    energy: float = 10.0,
    cost_minor: float | None = 500.0,
    currency: str | None = "SEK",
    charger_id: str = "entry_a",
    source: str = SOURCE_REGISTER,
    started_by: str = STARTED_OTHER,
    solar: tuple[float, float] = (0.0, 0.0),
    reference_minor: float | None = None,
) -> ChargeSession:
    """A finished session of `energy` kWh, all of it priced when `cost_minor` is given."""
    return ChargeSession(
        id=f"{charger_id}-{int(start.timestamp())}",
        charger_id=charger_id,
        start=start,
        end=start + timedelta(hours=hours),
        energy_kwh=energy,
        energy_source=source,
        priced_kwh=0.0 if cost_minor is None else energy,
        cost_minor=cost_minor,
        reference_cost_minor=(cost_minor if reference_minor is None else reference_minor),
        currency=currency,
        major_unit="kr",
        minor_unit="öre",
        started_by=started_by,
        strategy="cheapest",
        vehicle_id=None,
        vehicle_name=None,
        solar_kwh=solar[0],
        solar_known_kwh=solar[1],
        last_register_kwh=None,
        last_sample_at=None,
    )
