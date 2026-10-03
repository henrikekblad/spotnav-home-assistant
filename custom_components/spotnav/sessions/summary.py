"""Sessions summed per local day and per local month, and written as CSV.

A session counts on the local day (and month) it **started**, in Home Assistant's time zone, so a
charge that runs past midnight is one row, not two halves, and a DST night needs no special case:
the zone converts an absolute instant to a date, nothing here adds hours.

Costs are summed only for sessions in the bucket's currency (the most recent session's), so a person
who changed market never sees two currencies added together. The savings are the same priced energy at
the day's average effective price minus what it cost: an estimate, and always labelled one.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import date, datetime, tzinfo
from typing import Any, Final

from .model import ChargeSession

SAVINGS_BASIS: Final = "day_average_price"

CSV_COLUMNS: Final = (
    "start", "end", "energy_kwh", "energy_source", "estimated", "cost", "currency",
    "average_price_minor_per_kwh", "started_by", "strategy", "vehicle", "solar_share",
    "reference_cost", "savings",
)


def local_day(session: ChargeSession, zone: tzinfo) -> date:
    return session.start.astimezone(zone).date()


def summarize_bucket(period: str, sessions: Sequence[ChargeSession]) -> dict[str, Any]:
    """One bucket: totals over these sessions (any order, closed ones)."""
    ordered = sorted(sessions, key=lambda item: item.start)
    currency = next((item.currency for item in reversed(ordered) if item.currency is not None), None)
    energy = float(sum(item.energy_kwh for item in ordered))
    priced = [item for item in ordered if item.currency == currency and item.cost_minor is not None]
    priced_kwh = sum(item.priced_kwh for item in priced)
    cost_minor = sum(item.cost_minor or 0.0 for item in priced)
    reference_minor = sum(item.reference_cost_minor or 0.0 for item in priced)
    solar_kwh = sum(item.solar_kwh for item in ordered)
    solar_known = sum(item.solar_known_kwh for item in ordered)
    has_cost = bool(priced)
    last = ordered[-1] if ordered else None
    return {
        "period": period,
        "sessions": len(ordered),
        "energy_kwh": round(energy, 3),
        "cost": round(cost_minor / 100, 4) if has_cost else None,
        "currency": currency,
        "major_unit": None if last is None else last.major_unit,
        "minor_unit": None if last is None else last.minor_unit,
        "average_price_minor_per_kwh": round(cost_minor / priced_kwh, 3) if has_cost and priced_kwh > 0 else None,
        "solar_share": round(min(1.0, solar_kwh / solar_known), 3) if solar_known > 0 else None,
        "reference_cost": round(reference_minor / 100, 4) if has_cost else None,
        "savings": round((reference_minor - cost_minor) / 100, 4) if has_cost else None,
        "savings_basis": SAVINGS_BASIS,
        "savings_estimate": True,
        "estimated": any(item.estimated for item in ordered),
    }


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def previous_month(day: date) -> str:
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return f"{year:04d}-{month:02d}"


def summarize(sessions: Iterable[ChargeSession], zone: tzinfo, *, by: str) -> list[dict[str, Any]]:
    """Every day (`by="day"`) or month (`by="month"`) that has a session, newest first."""
    buckets: dict[str, list[ChargeSession]] = {}
    for session in sessions:
        if session.end is None:
            continue
        day = local_day(session, zone)
        key = day.isoformat() if by == "day" else month_key(day)
        buckets.setdefault(key, []).append(session)
    return [summarize_bucket(key, buckets[key]) for key in sorted(buckets, reverse=True)]


def month_summary(sessions: Iterable[ChargeSession], zone: tzinfo, key: str) -> dict[str, Any]:
    """The month `key` (`YYYY-MM`), all zeros when nothing was charged in it."""
    members = [
        session
        for session in sessions
        if session.end is not None and month_key(local_day(session, zone)) == key
    ]
    return summarize_bucket(key, members)


def sessions_summary(sessions: Sequence[ChargeSession], zone: tzinfo, now: datetime) -> dict[str, Any]:
    """The dashboard's additive `sessions_summary` block: this month and last month."""
    today = now.astimezone(zone).date()
    return {
        "this_month": month_summary(sessions, zone, month_key(today)),
        "last_month": month_summary(sessions, zone, previous_month(today)),
    }


def _safe(value: str) -> str:
    """A text cell a spreadsheet will not run as a formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def sessions_csv(
    sessions: Iterable[ChargeSession], zone: tzinfo, *, first: date | None, last: date | None
) -> str:
    """The closed sessions that started on a local day in `first..last` (either end open), one row
    each, oldest first. Instants are local time with their offset."""
    rows = sorted(
        (
            session
            for session in sessions
            if session.end is not None
            and (first is None or local_day(session, zone) >= first)
            and (last is None or local_day(session, zone) <= last)
        ),
        key=lambda item: item.start,
    )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for session in rows:
        record = session.public()
        cells: list[Any] = []
        for column in CSV_COLUMNS:
            if column == "start":
                cells.append(session.start.astimezone(zone).isoformat())
            elif column == "end":
                cells.append("" if session.end is None else session.end.astimezone(zone).isoformat())
            else:
                value = record[column]
                cells.append("" if value is None else _safe(value) if isinstance(value, str) else value)
        writer.writerow(cells)
    return out.getvalue()
