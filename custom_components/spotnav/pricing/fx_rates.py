"""The ECB's euro reference rates, held by Home Assistant itself.

Every day document is in EUR/kWh and carries the rate that converts it (`fx`, `fx_date`, `fx_src`): the
planner converts with the day's own rate and plans nothing without one (`missing_fx_rate`), and session
costing stores the EUR price and the rate per interval. The relay attaches that rate today. A provider that
reads prices without the relay (Nord Pool through Home Assistant, ENTSO-E) gets EUR only, so the rate has to
come from here: one keyless request a day, the same numbers the relay publishes.

* **Source.** The daily file ([DAILY_URL], one table) and the 90-day history ([HISTORY_URL], every table of
  the last ninety days, newest first). The history is read when nothing is held or more than one table is
  missing (a first start, a week offline); otherwise the daily file adds the newest table.
* **The rule** ([rate_for], the relay's `ecb.Cache.On`): a day is converted with the newest table dated on or
  before it, at most [MAX_RATE_AGE] before it. Tomorrow, priced the afternoon before, gets today's table; a
  Monday gets Friday's; a day with no table that close gets no rate, which is the honest answer.
* **Failure policy.** An unreachable ECB, an error status or a body that is not a reference table changes
  nothing held: yesterday's table is an ordinary thing. After a week without one, non-EUR days get no rate.
* **When.** The ECB publishes "at around 16:00 CET every working day, except on TARGET closing days". One
  request a day after that ([PUBLICATION_TIME] + [PUBLICATION_SETTLE] Frankfurt time, plus up to
  [JITTER_SPAN] so installations do not arrive together); failures back off through [BACKOFF_STEPS_SECONDS];
  a table that has not moved yet (late, or a holiday) is asked again hourly until [LATEST_RETRY].
* **Store.** The newest [MAX_TABLES] tables in their own HA `Store`, so a restart converts without the network.

Terms of use, checked 2026-10-30. The rates page
(https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
says "The reference rates are published for information purposes only. Using the rates for transaction
purposes is strongly discouraged." The ECB's Disclaimer & Copyright page
(https://www.ecb.europa.eu/services/disclaimer/html/index.en.html) says "users of this website may make free
use of the information obtained directly from it" and "When such information is distributed or reproduced, it
must appear accurately and the ECB must be cited as the source." SpotNav uses the rates to show and compare
prices (information, not transactions), never alters a rate, and names the source (`fx_src: "ECB"`, `fx_date`).

Not set up by the integration yet: the providers that need it (S6, S9) own its start and stop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientSession
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN


_LOGGER = logging.getLogger(__name__)

DAILY_URL: Final = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HISTORY_URL: Final = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"

#: Who is asking, so the ECB can tell this traffic apart from a browser's.
USER_AGENT: Final = "SpotNav (Home Assistant integration; +https://github.com/henrikekblad/spotnav-home-assistant)"

#: The daily file is ~1.5 kB and the 90-day history ~70 kB; a megabyte bounds a mistaken body.
REQUEST_TIMEOUT_S: Final = 20.0
MAX_RESPONSE_BYTES: Final = 1024 * 1024
CHUNK_BYTES: Final = 64 * 1024

#: The rates the daily table must carry, as the relay insists (`ecb.Needed`): a table without them is
#: truncated and refused. A currency the table does not carry otherwise just has no rate.
NEEDED: Final = ("SEK", "NOK", "DKK")

#: How far before a day its table may be and still convert it (the relay's `MaxDatedAge`).
MAX_RATE_AGE: Final = timedelta(days=7)

#: Tables kept, newest first: the 90-day history holds ~65, so this keeps all of it with room.
MAX_TABLES: Final = 100

#: The ECB's clock and publication time ("around 16:00 CET"), and how long after it the first request goes.
ECB_ZONE: Final = ZoneInfo("Europe/Berlin")
PUBLICATION_TIME: Final = time(16, 0)
PUBLICATION_SETTLE: Final = timedelta(minutes=20)

#: Spread added to the daily request, uniformly in `[0, JITTER_SPAN)`.
JITTER_SPAN: Final = timedelta(minutes=30)

#: Transport-failure backoff, in seconds, capped at the last step.
BACKOFF_STEPS_SECONDS: Final[tuple[int, ...]] = (60, 300, 900, 1800, 3600)

#: A table that has not moved after the publication is asked again this often, but not after this time
#: of the ECB's day (a TARGET holiday publishes nothing; tomorrow's slot takes over).
STALE_RETRY: Final = timedelta(hours=1)
LATEST_RETRY: Final = time(22, 0)

#: The first request after a start that finds the held table behind, spread over a minute or two.
STARTUP_DELAY: Final = timedelta(seconds=60)

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}_fx_rates"

FxFailureCode = Literal["timeout", "network", "http_status", "too_large", "not_ecb"]


@dataclass(frozen=True, slots=True)
class EcbTable:
    """One day's reference rates as published: units of each currency per EUR."""

    day: date
    values: Mapping[str, float]


class FxParseError(ValueError):
    """A body that is not an ECB reference-rate table (or holds a wrong entry)."""


class _Failure(Exception):
    def __init__(self, code: FxFailureCode, status: int | None = None) -> None:
        super().__init__(code)
        self.code: FxFailureCode = code
        self.status = status


# ------------------------------------------------------------------ parsing


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rate(currency: str, text: str | None, day: str) -> float:
    try:
        value = float(text or "")
    except ValueError as err:
        raise FxParseError(f"ecb rate {currency}={text!r} on {day} is not a number") from err
    if not math.isfinite(value) or value <= 0:
        raise FxParseError(f"ecb rate {currency}={text!r} on {day} is not a rate")
    return value


def _tables(body: bytes) -> list[EcbTable]:
    """Every dated table in an ECB envelope, in document order. A wrong entry fails the whole body: a table
    with one wrong rate would convert some day silently wrong."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as err:
        raise FxParseError("ecb body is not reference-rate XML") from err
    tables: list[EcbTable] = []
    for outer in root:
        if _local(outer.tag) != "Cube":
            continue
        for cube in outer:
            stamp = cube.get("time")
            if _local(cube.tag) != "Cube" or not stamp or len(cube) == 0:
                continue
            try:
                day = date.fromisoformat(stamp)
            except ValueError as err:
                raise FxParseError(f"ecb table date {stamp!r}") from err
            values = {
                entry.get("currency", ""): _rate(entry.get("currency", ""), entry.get("rate"), stamp)
                for entry in cube
                if _local(entry.tag) == "Cube"
            }
            values.pop("", None)
            if values:
                tables.append(EcbTable(day, values))
    if not tables:
        raise FxParseError("ecb body has no dated rate table")
    return tables


def parse_daily(body: bytes) -> EcbTable:
    """The daily file's one table; refused without the [NEEDED] rates (a truncated table)."""
    table = _tables(body)[0]
    for currency in NEEDED:
        if currency not in table.values:
            raise FxParseError(f"ecb table dated {table.day} has no {currency} rate")
    return table


def parse_history(body: bytes) -> list[EcbTable]:
    """Every table of a history file (the 90-day one), newest first as published."""
    return _tables(body)


def table_for(tables: Iterable[EcbTable], day: date) -> EcbTable | None:
    """The table that converts `day` (a calendar date in the day's own zone): the newest one dated on or before
    it, no more than [MAX_RATE_AGE] before it; `None` when none is held that close."""
    limit = day - MAX_RATE_AGE
    best: EcbTable | None = None
    for table in tables:
        if limit <= table.day <= day and (best is None or table.day > best.day):
            best = table
    return best


# ------------------------------------------------------------------ the ECB's calendar


def _publication(day: date) -> datetime:
    """When a request for `day`'s table is worth making (UTC)."""
    local = datetime.combine(day, PUBLICATION_TIME, tzinfo=ECB_ZONE) + PUBLICATION_SETTLE
    return dt_util.as_utc(local)


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


def expected_table(now: datetime) -> date:
    """The newest table that should exist at `now`: the latest weekday whose publication has passed. TARGET
    holidays are not known here; on one the held table simply stays behind until the next working day."""
    day = now.astimezone(ECB_ZONE).date()
    while not (_is_weekday(day) and _publication(day) <= now):
        day -= timedelta(days=1)
    return day


def next_publication(now: datetime) -> datetime:
    """The next weekday publication after `now` (UTC), before jitter."""
    day = now.astimezone(ECB_ZONE).date()
    while not (_is_weekday(day) and _publication(day) > now):
        day += timedelta(days=1)
    return _publication(day)


def _previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while not _is_weekday(day):
        day -= timedelta(days=1)
    return day


async def _read_bounded(stream) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise _Failure("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


# ------------------------------------------------------------------ the holder


class FxRates:
    """The held ECB tables, their store and their daily refresh. One per Home Assistant instance.

    [rate_for] is synchronous and reads only what is held; [async_start] restores the store and installs the
    daily timer, [async_stop] removes it. [async_refresh] is single-flight.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        session: ClientSession | None = None,
        store: Store[dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
        jitter: Callable[[], float] | None = None,
        scheduler: Callable[[HomeAssistant, Callable[[datetime], None], datetime], Callable[[], None]]
        | None = None,
    ) -> None:
        self._hass = hass
        # `None` means the shared HA client, fetched when a request is made.
        self._session = session
        self._store: Store[dict[str, Any]] = store if store is not None else Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._now = now if now is not None else dt_util.utcnow
        self._jitter: Callable[[], float] = jitter if jitter is not None else random.random
        self._scheduler = scheduler if scheduler is not None else async_track_point_in_utc_time
        self._tables: dict[date, EcbTable] = {}
        self._fetched_at: datetime | None = None
        self._attempt_error: FxFailureCode | None = None
        self._failures = 0
        self._restore_task: asyncio.Task[None] | None = None
        self._inflight: asyncio.Task[bool] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._stopped = False

    # -------------------------------------------------------------- reading

    def rate_for(self, currency: str, day: date) -> tuple[float, date] | None:
        """`(units per EUR, the table's date)` for converting `day`, or `None` when no table within
        [MAX_RATE_AGE] on or before it carries the currency. EUR has no rate (a EUR day needs none)."""
        table = table_for(self._tables.values(), day)
        if table is None:
            return None
        rate = table.values.get(currency)
        return None if rate is None else (rate, table.day)

    @property
    def newest(self) -> date | None:
        return max(self._tables) if self._tables else None

    @property
    def fetched_at(self) -> datetime | None:
        return self._fetched_at

    @property
    def attempt_error(self) -> FxFailureCode | None:
        return self._attempt_error

    # -------------------------------------------------------------- store

    async def async_ensure_restored(self) -> None:
        """Read the store once (single-flight); a failed or unreadable store is an empty one."""
        if self._restore_task is None:
            self._restore_task = self._hass.async_create_task(self._restore())
        await asyncio.shield(self._restore_task)

    async def _restore(self) -> None:
        try:
            payload = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a broken store is an absent one
            _LOGGER.warning("The stored exchange rates could not be read; they will be fetched again")
            return
        if not isinstance(payload, dict):
            return
        raws = payload.get("tables")
        restored: list[EcbTable] = []
        for raw in raws if isinstance(raws, list) else []:
            table = _restored_table(raw)
            if table is not None:
                restored.append(table)
        self._adopt(restored)
        fetched = payload.get("fetched_at")
        if isinstance(fetched, str) and self._fetched_at is None:
            self._fetched_at = dt_util.parse_datetime(fetched)

    def _adopt(self, tables: Iterable[EcbTable]) -> bool:
        """Add tables (a newer fetch of a date replaces the held one) and keep the newest [MAX_TABLES]; whether
        anything changed."""
        changed = False
        for table in tables:
            if self._tables.get(table.day) != table:
                self._tables[table.day] = table
                changed = True
        for day in sorted(self._tables, reverse=True)[MAX_TABLES:]:
            del self._tables[day]
        return changed

    def _payload(self) -> dict[str, Any]:
        return {
            "tables": [
                {"date": table.day.isoformat(), "values": dict(table.values)}
                for table in sorted(self._tables.values(), key=lambda table: table.day, reverse=True)
            ],
            "fetched_at": None if self._fetched_at is None else self._fetched_at.isoformat(),
        }

    # -------------------------------------------------------------- fetching

    def _client(self) -> ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self._hass)
        return self._session

    async def _fetch(self, url: str) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/xml"}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async with self._client().get(url, headers=headers) as response:
                    if response.status != 200:
                        raise _Failure("http_status", response.status)
                    return await _read_bounded(response.content)
        except TimeoutError as err:
            raise _Failure("timeout") from err
        except ClientError as err:
            raise _Failure("network") from err

    def _wants_history(self) -> bool:
        """Whether more than the newest table is missing, so one daily file would leave a gap."""
        newest = self.newest
        return newest is None or newest < _previous_weekday(expected_table(self._now()))

    async def async_refresh(self) -> bool:
        """Ask the ECB once (the history when more than one table is missing, else the daily file); whether a
        table was read. Concurrent callers share one request. A failure keeps everything held."""
        await self.async_ensure_restored()
        if self._inflight is None:
            self._inflight = self._hass.async_create_task(self._refresh())
        task = self._inflight
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._inflight is task:
                self._inflight = None

    async def _refresh(self) -> bool:
        history = self._wants_history()
        try:
            body = await self._fetch(HISTORY_URL if history else DAILY_URL)
            tables = parse_history(body) if history else [parse_daily(body)]
        except _Failure as err:
            self._attempt_error = err.code
            self._failures += 1
            _LOGGER.debug("The ECB exchange rates could not be fetched: %s (%s)", err.code, err.status)
            return False
        except FxParseError as err:
            self._attempt_error = "not_ecb"
            self._failures += 1
            _LOGGER.debug("The ECB answered with something that is not a rate table: %s", err)
            return False
        self._attempt_error = None
        self._failures = 0
        self._fetched_at = self._now()
        self._adopt(tables)
        try:
            await self._store.async_save(self._payload())
        except Exception:  # noqa: BLE001 - the tables are held; the next save writes them
            _LOGGER.warning("The exchange rates could not be stored")
        return True

    # -------------------------------------------------------------- the daily timer

    async def async_start(self) -> None:
        """Restore the store and install the timer: soon when the held table is behind, else at the next
        publication."""
        self._stopped = False
        await self.async_ensure_restored()
        now = self._now()
        newest = self.newest
        if newest is None or newest < expected_table(now):
            self._schedule(now + STARTUP_DELAY * (1.0 + self._jitter()))
        else:
            self._schedule_publication(now)

    async def async_stop(self) -> None:
        """Remove the timer; a refresh already running finishes and is stored, nothing is scheduled after it."""
        self._stopped = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def _schedule(self, when: datetime) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = self._scheduler(self._hass, self._fire, when)

    def _schedule_publication(self, now: datetime) -> None:
        self._schedule(next_publication(now) + JITTER_SPAN * self._jitter())

    @callback
    def _fire(self, _when: datetime) -> None:
        self._cancel_timer = None
        if self._stopped:
            return
        self._run_task = self._hass.async_create_task(self._run())

    async def _run(self) -> None:
        read = await self.async_refresh()
        if self._stopped:
            return
        now = self._now()
        publication = next_publication(now) + JITTER_SPAN * self._jitter()
        if not read:
            step = min(max(self._failures, 1), len(BACKOFF_STEPS_SECONDS)) - 1
            retry = now + timedelta(seconds=BACKOFF_STEPS_SECONDS[step]) * (1.0 + 0.1 * self._jitter())
            self._schedule(min(retry, publication))
            return
        newest = self.newest
        retry = now + STALE_RETRY
        if newest is not None and newest < expected_table(now) and retry.astimezone(ECB_ZONE).time() < LATEST_RETRY:
            # Behind after a successful read: late, or a TARGET holiday. Hourly until the evening.
            self._schedule(min(retry, publication))
            return
        self._schedule(publication)


def _restored_table(raw: Any) -> EcbTable | None:
    """A stored table, checked as a fetched one is; anything else is dropped."""
    if not isinstance(raw, dict) or not isinstance(raw.get("values"), dict):
        return None
    try:
        day = date.fromisoformat(raw.get("date"))
        values = {
            str(currency): _rate(str(currency), str(value), day.isoformat())
            for currency, value in raw["values"].items()
            if not isinstance(value, bool)
        }
    except (TypeError, ValueError):
        return None
    return EcbTable(day, values) if values else None
