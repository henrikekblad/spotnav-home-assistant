"""The one-off import of the charges from before the sessions feature, from Home Assistant's own
long-term statistics.

Sessions are recorded live from the moment the feature is installed. Earlier charges are rebuilt, once
per charger and in the background after start-up (never in setup), from the recorder's hourly statistics
of the charger's energy register (or the power integration's energy sensor), up to 24 months back and
only before the charger's first live session, so nothing overlaps:

* consecutive hours with at least `MIN_HOUR_KWH` of energy are one charge; an hour without ends it;
* each hour's energy is split over the price intervals of its day (the repository's day file, 15-minute
  or hourly alike, so a 15-minute day is the hour's average) and kept with their raw spot prices exactly
  like a live session, so a cost is made when it is read, with the person's settings then;
* an hour with no price is unpriced: its energy is kept, the cost stays absent;
* the sessions say `source: "imported_hourly"`, start `unknown`, and have no solar share.

The marker the store keeps per charger says the import ran and what it covered. If hours were unpriced
(the relay's history backfill may still be running), a later start tries again, at most once a day;
day files are fetched one at a time with a pause between requests. Nothing here blocks setup or raises.

`migrate_legacy` is the other half of the storage change: a record that only has a stored cost gets its
intervals re-derived from the day files the repository holds, when it holds them all.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .costing import merge_slices, Slice, spot_intervals, split_energy
from .inputs import local_zone, price_market, PriceMarket
from .model import (
    ChargeSession,
    SOURCE_IMPORTED_HOURLY,
    SOURCE_INTEGRATED,
    SOURCE_REGISTER,
    STARTED_UNKNOWN,
)
from .store import SessionStore

_LOGGER = logging.getLogger(__name__)

#: How far back statistics are read (the store's own retention).
IMPORT_DAYS: Final = 730
#: An hour with less energy than this is not charging.
MIN_HOUR_KWH: Final = 0.1
#: The pause after every day file asked of the relay.
FETCH_GAP_S: Final = 2.0
#: How long after start-up the import begins.
START_DELAY_S: Final = 120.0
#: An hour is priced when at least this share of its energy fell in a published interval.
PRICED_SHARE: Final = 0.999
MARKER_VERSION: Final = 2

STATUS_PENDING: Final = "pending"
STATUS_RUNNING: Final = "running"
STATUS_DONE: Final = "done"
STATUS_NO_REGISTER: Final = "no_register"
STATUS_NO_RECORDER: Final = "no_recorder"
STATUS_FAILED: Final = "failed"


@dataclass(frozen=True, slots=True)
class Hour:
    """The energy (kWh) a register gained in the hour starting at `start` (UTC)."""

    start: datetime
    kwh: float

    @property
    def end(self) -> datetime:
        return self.start + timedelta(hours=1)


HourFetcher = Callable[[str, datetime, datetime], Awaitable[list[Hour]]]


def hours_from_rows(rows: Iterable[dict[str, Any]]) -> list[Hour]:
    """The hours of recorder statistics rows (`start` as a timestamp or a datetime, `change` in kWh).

    A missing, non-finite or negative change is no energy; a start off the hour is not an hour row.
    """
    hours = []
    for row in rows:
        start, change = row.get("start"), row.get("change")
        if isinstance(start, (int, float)) and not isinstance(start, bool):
            moment = dt_util.utc_from_timestamp(start / 1000.0 if start > 1e11 else start)
        elif isinstance(start, datetime):
            moment = dt_util.as_utc(start)
        else:
            continue
        if isinstance(change, bool) or not isinstance(change, (int, float)) or not math.isfinite(change):
            continue
        if change > 0 and moment.minute == 0 and moment.second == 0:
            hours.append(Hour(moment, float(change)))
    return sorted(hours, key=lambda hour: hour.start)


async def recorder_hours(hass: HomeAssistant, entity_id: str, start: datetime, end: datetime) -> list[Hour]:
    """The register's hourly energy between `start` and `end`, read from the long-term statistics."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {entity_id}, "hour", {"energy": "kWh"}, {"change"}
    )
    return hours_from_rows(rows.get(entity_id, ()))


def group_hours(hours: Iterable[Hour]) -> list[list[Hour]]:
    """Charges: runs of consecutive hours with at least `MIN_HOUR_KWH`; a gap of an hour ends one."""
    groups: list[list[Hour]] = []
    for hour in sorted(hours, key=lambda item: item.start):
        if hour.kwh < MIN_HOUR_KWH:
            continue
        if groups and groups[-1][-1].end == hour.start:
            groups[-1].append(hour)
        else:
            groups.append([hour])
    return groups


@dataclass(frozen=True, slots=True)
class Built:
    sessions: list[ChargeSession]
    priced_hours: int
    unpriced_hours: int


def build_sessions(
    charger_id: str,
    groups: Sequence[Sequence[Hour]],
    spot: Sequence[Any],
    *,
    market: PriceMarket | None,
    energy_source: str,
) -> Built:
    """The imported sessions of `groups`, each hour split over `spot` (the raw intervals of the days)."""
    sessions = []
    priced_hours = unpriced_hours = 0
    for group in groups:
        slices: tuple[Slice, ...] = ()
        for hour in group:
            parts = split_energy(spot, hour.start, hour.end, hour.kwh)
            if sum(part.kwh for part in parts) >= hour.kwh * PRICED_SHARE:
                priced_hours += 1
            else:
                unpriced_hours += 1
            slices = merge_slices(slices, parts)
        first = group[0].start
        sessions.append(
            ChargeSession(
                id=f"{charger_id}-imp-{int(first.timestamp())}",
                charger_id=charger_id,
                start=first,
                end=group[-1].end,
                energy_kwh=sum(hour.kwh for hour in group),
                energy_source=energy_source,
                priced_kwh=0.0,
                cost_minor=None,
                reference_cost_minor=None,
                currency=None if market is None else market.area.currency,
                major_unit=None if market is None else market.area.major_unit,
                minor_unit=None if market is None else market.area.minor_unit,
                started_by=STARTED_UNKNOWN,
                strategy=None,
                vehicle_id=None,
                vehicle_name=None,
                solar_kwh=0.0,
                solar_known_kwh=0.0,
                last_register_kwh=None,
                last_sample_at=None,
                source=SOURCE_IMPORTED_HOURLY,
                intervals=slices,
                area_id=None if market is None else market.area_id,
            )
        )
    return Built(sessions, priced_hours, unpriced_hours)


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


class HistoryImporter:
    """One charger's import of earlier charges. `async_run` is safe to call at any start: it does the
    import once, re-prices at most once a day, and otherwise only reads the marker."""

    def __init__(
        self,
        hass: HomeAssistant,
        charger_id: str,
        store: SessionStore,
        *,
        register_entity: Callable[[], str | None],
        energy_from_power: Callable[[], bool] = lambda: False,
        fetch_hours: HourFetcher | None = None,
        market: Callable[[], PriceMarket | None] | None = None,
        now: Callable[[], datetime] = dt_util.utcnow,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        fetch_gap_s: float = FETCH_GAP_S,
        recorder_ready: Callable[[], bool] | None = None,
    ) -> None:
        self._hass = hass
        self._charger_id = charger_id
        self._store = store
        self._register_entity = register_entity
        self._energy_from_power = energy_from_power
        self._fetch_hours = fetch_hours or (lambda entity, start, end: recorder_hours(hass, entity, start, end))
        self._market = market or (lambda: price_market(hass, charger_id))
        self._now = now
        self._sleep = sleep
        self._gap = fetch_gap_s
        self._recorder_ready = recorder_ready or (lambda: "recorder" in hass.config.components)
        self.status = STATUS_PENDING
        self._days: dict[date, Any] = {}

    # ---- diagnostics

    def diagnostics(self) -> dict[str, Any]:
        """The import's status: what it covered, how many sessions, how many hours were priced."""
        marker = self._store.import_marker(self._charger_id) or {}
        return {
            "status": self.status,
            "range": None if not marker else {"from": marker.get("from"), "to": marker.get("to")},
            "sessions": marker.get("sessions"),
            "priced_hours": marker.get("priced_hours"),
            "unpriced_hours": marker.get("unpriced_hours"),
            "done_at": marker.get("done_at"),
            "last_price_check": marker.get("checked_day"),
        }

    # ---- the run

    async def async_run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed import must never reach setup; it is tried again
            self.status = STATUS_FAILED
            _LOGGER.exception("Importing earlier charges failed for %s", self._charger_id)

    async def _run(self) -> None:
        await migrate_legacy(self._hass, self._store, self._charger_id)
        marker = self._store.import_marker(self._charger_id)
        self._days = {}
        today = self._now().astimezone(local_zone(self._hass)).date().isoformat()
        if marker is not None:
            self.status = STATUS_DONE
            # A version-1 marker was made before archive days were asked for: re-price it at once.
            outdated = int(marker.get("version", 1)) < MARKER_VERSION
            if marker.get("unpriced_hours", 0) > 0 and (outdated or marker.get("checked_day") != today):
                await self._reprice(marker, today)
            return
        entity = self._register_entity()
        if entity is None:
            self.status = STATUS_NO_REGISTER
            return
        if not self._recorder_ready():
            self.status = STATUS_NO_RECORDER
            return
        self.status = STATUS_RUNNING
        now = self._now()
        first_live = self._store.first_live_start(self._charger_id)
        cutoff = _floor_hour(now if first_live is None else min(first_live, now))
        begin = _floor_hour(now - timedelta(days=IMPORT_DAYS))
        built = await self._build(entity, begin, cutoff)
        marker = {
            "version": MARKER_VERSION,
            "entity_id": entity,
            "from": begin.isoformat(),
            "to": cutoff.isoformat(),
            "done_at": now.isoformat(),
            "sessions": len(built.sessions),
            "priced_hours": built.priced_hours,
            "unpriced_hours": built.unpriced_hours,
            "checked_day": today,
        }
        self._store.replace_imported(self._charger_id, built.sessions, marker, now)
        self.status = STATUS_DONE
        _LOGGER.debug(
            "Imported %d earlier charge(s) for %s (%d priced hours, %d unpriced)",
            len(built.sessions), self._charger_id, built.priced_hours, built.unpriced_hours,
        )

    async def _reprice(self, marker: dict[str, Any], today: str) -> None:
        """Try the unpriced hours again; the new import replaces the old only if it priced more."""
        entity = marker.get("entity_id")
        begin, cutoff = dt_util.parse_datetime(str(marker.get("from"))), dt_util.parse_datetime(str(marker.get("to")))
        if not isinstance(entity, str) or begin is None or cutoff is None or not self._recorder_ready():
            return
        now = self._now()
        built = await self._build(entity, begin, cutoff)
        if built.priced_hours > int(marker.get("priced_hours", 0)):
            marker = {
                **marker,
                "version": MARKER_VERSION,
                "sessions": len(built.sessions),
                "priced_hours": built.priced_hours,
                "unpriced_hours": built.unpriced_hours,
                "checked_day": today,
            }
            self._store.replace_imported(self._charger_id, built.sessions, marker, now)
        else:
            self._store.set_import_marker(
                self._charger_id, {**marker, "version": MARKER_VERSION, "checked_day": today}
            )

    async def _build(self, entity: str, begin: datetime, cutoff: datetime) -> Built:
        hours = [hour for hour in await self._fetch_hours(entity, begin, cutoff) if hour.end <= cutoff]
        groups = group_hours(hours)
        market = self._market()
        spot: tuple[Any, ...] = ()
        if market is not None and groups:
            zone = market.zone or local_zone(self._hass)
            days = sorted(
                {
                    moment.astimezone(zone).date()
                    for group in groups
                    for hour in group
                    for moment in (hour.start, hour.end - timedelta(seconds=1))
                }
            )
            documents = []
            for day in days:
                document = await self._day(market, day)
                if document is not None:
                    documents.append(document)
            spot = spot_intervals(documents, market.area.currency)
        source = SOURCE_INTEGRATED if self._energy_from_power() else SOURCE_REGISTER
        return build_sessions(self._charger_id, groups, spot, market=market, energy_source=source)

    async def _day(self, market: PriceMarket, day: date) -> Any:
        """The day's document: what the repository holds, else asked of the relay (archive) one request at a time."""
        if day in self._days:
            return self._days[day]
        repository = market.repository
        document = repository.day_snapshot(market.area_id, day).document
        if document is None:
            # An archive read: the index lists only the recent days, the relay has the older ones.
            document = await repository.async_get_archive_day(market.area_id, day)
            await self._sleep(self._gap)
        self._days[day] = document
        return document


async def migrate_legacy(hass: HomeAssistant, store: SessionStore, charger_id: str) -> int:
    """Re-derive the intervals of records that only carry a stored cost, from the day files held now.

    The record's energy is spread evenly over its start to end (as a live sample span is) and split over
    the day files' intervals. Done only when every day it touched is held and the energy that falls in
    published intervals matches what the record had priced; otherwise the record keeps its stored cost
    (`cost_basis: "stored"`). Returns how many records were migrated. Nothing is fetched.
    """
    legacy = [item for item in store.closed_raw(charger_id) if item.cost_basis == "stored"]
    if not legacy:
        return 0
    market = price_market(hass, charger_id)
    if market is None:
        return 0
    await market.repository.async_restore()
    zone = market.zone or local_zone(hass)
    migrated: dict[str, ChargeSession] = {}
    for item in legacy:
        if item.end is None or (item.currency not in (None, market.area.currency)):
            continue
        first = item.start.astimezone(zone).date()
        last = item.end.astimezone(zone).date()
        documents = []
        day = first
        while day <= last:
            snapshot = market.repository.day_snapshot(market.area_id, day)
            if snapshot.document is None:
                break
            documents.append(snapshot.document)
            day += timedelta(days=1)
        else:
            spot = spot_intervals(documents, market.area.currency)
            slices = tuple(split_energy(spot, item.start, item.end, item.energy_kwh))
            if abs(sum(part.kwh for part in slices) - item.priced_kwh) <= max(0.01, item.priced_kwh * 0.02):
                migrated[item.id] = replace(
                    item, intervals=slices, area_id=market.area_id, cost_minor=None,
                    reference_cost_minor=None, priced_kwh=0.0,
                )
    if not migrated:
        return 0
    store.replace_closed(
        charger_id,
        [migrated.get(item.id, item) for item in store.closed_raw(charger_id)],
        dt_util.utcnow(),
    )
    return len(migrated)
