"""The suite's one fake relay wire and its doubles.

`StubTransport` routes by path and serves bodies as streams, `StoreDouble` and `Clock` are the
repository's store and injected time, `FakeScheduler` records timers instead of waiting, and
`serve` publishes the fixture documents. Every test that needs a relay imports from here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from aiohttp import ClientError
from homeassistant.core import HomeAssistant

FIXTURES = Path(__file__).parent / "fixtures" / "relay"


BASE_URL = "https://relay.test"


STOCKHOLM = ZoneInfo("Europe/Stockholm")


TODAY = date(2026, 9, 22)


YESTERDAY = date(2026, 9, 21)


TOMORROW = date(2026, 9, 23)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def day_body(area: str, when: date) -> str:
    """A valid document for any area/date, built from the checked-in one.

    `start` moves with the date on purpose: it must be that local day's midnight,
    and a test that forgot would be testing the parser's coverage rule by accident.
    """
    document = json.loads(fixture("day_SE4_2026-09-22_96.json"))
    document["area"] = area
    document["date"] = when.isoformat()
    document["start"] = datetime(when.year, when.month, when.day, tzinfo=STOCKHOLM).isoformat()
    return json.dumps(document)


class Response:
    """A chunked body as a *real* stream, which is what Finding 2 is about.

    Each `read(limit)` advances a cursor and hands back at most `limit` bytes, and
    may stop well short of `limit` without being at EOF -- the behaviour a single
    `read(n)` call in the production code mistook for a whole body. It can also be
    told to fail or to stall part-way through, so a mid-stream transport failure
    and a mid-stream timeout are testable without any sockets.
    """

    def __init__(
        self,
        status: int,
        chunks: list[bytes],
        *,
        fail_after: int | None = None,
        stall_after: int | None = None,
        stall: asyncio.Event | None = None,
    ) -> None:
        self.status = status
        self.content = self
        self._chunks = chunks
        self._fail_after = fail_after
        self._stall_after = stall_after
        self._stall = stall
        self._index = 0
        self._offset = 0
        self.reads = 0

    @property
    def unread(self) -> int:
        """Bytes the client never asked for -- the proof that it stopped early."""
        remaining = sum(len(chunk) for chunk in self._chunks[self._index :])
        return remaining - self._offset

    async def read(self, limit: int) -> bytes:
        self.reads += 1
        if self._fail_after is not None and self._index >= self._fail_after:
            raise ClientError("the connection dropped mid-body")
        if self._stall_after is not None and self._index >= self._stall_after:
            assert self._stall is not None
            await self._stall.wait()
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index][self._offset :]
        if len(chunk) > limit:
            self._offset += limit
            return chunk[:limit]
        self._index += 1
        self._offset = 0
        return chunk

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def make_response(status: int, body: str, **kwargs: object) -> Response:
    return Response(status, [body.encode("utf-8")], **kwargs)  # type: ignore[arg-type]


def split(body: str, size: int = 7) -> list[bytes]:
    """The same bytes, delivered in pieces small enough to prove it is streamed."""
    raw = body.encode("utf-8")
    return [raw[index : index + size] for index in range(0, len(raw), size)]


class Request:
    """A request context manager, shaped like aiohttp's own.

    `session.get(url)` in aiohttp is *both* awaitable and an async context
    manager, so a stub that returned a plain coroutine would fail in a way that
    looks like a hang.
    """

    def __init__(self, transport: StubTransport, url: str) -> None:
        self._transport = transport
        self._url = url

    async def _resolve(self) -> Response:
        return await self._transport.resolve(self._url)

    def __await__(self):
        return self._resolve().__await__()

    async def __aenter__(self) -> Response:
        return await self._resolve()

    async def __aexit__(self, *exc: object) -> None:
        return None


class StubTransport:
    """Routes by path, counts calls, and serves bodies as streams.

    A route is a list of chunks plus optional stream behaviour, and each request
    gets a *fresh* [Response] over it: a stream is consumed once, and a double that
    handed out the same bytes twice would hide a reader that never reached EOF.
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, list[bytes], dict[str, Any]]] = {}
        self.calls: list[str] = []
        self.responses: dict[str, Response] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}

    def serve(self, path: str, status: int, body: str) -> None:
        self.serve_chunks(path, status, [body.encode("utf-8")])

    def serve_chunks(self, path: str, status: int, chunks: list[bytes], **behaviour: Any) -> None:
        self.routes[path] = (status, chunks, behaviour)

    def serve_area(self, area: str = "SE4", *, chunked: int | None = None) -> None:
        """The three resources, optionally delivered in tiny pieces."""
        bodies = {
            "/v1/areas.json": fixture("areas.json"),
            "/v1/index.json": fixture("index.json"),
        }
        for day in (TODAY, YESTERDAY):
            bodies[self.day_path(area, day)] = day_body(area, day)
        for path, body in bodies.items():
            chunks = split(body, chunked) if chunked is not None else [body.encode("utf-8")]
            self.serve_chunks(path, 200, chunks)

    @staticmethod
    def day_path(area: str, day: date) -> str:
        return f"/v1/{area}/{day.year:04d}/{day.month:02d}-{day.day:02d}.json"

    def hold(self, path: str) -> None:
        self.gates[path] = asyncio.Event()
        self.entered[path] = asyncio.Event()

    def release(self, path: str) -> None:
        self.gates[path].set()

    def get(self, url: str) -> Request:
        return Request(self, url)

    async def resolve(self, url: str) -> Response:
        path = url.removeprefix(BASE_URL)
        self.calls.append(path)
        entered = self.entered.get(path)
        if entered is not None:
            entered.set()
        gate = self.gates.get(path)
        if gate is not None:
            await gate.wait()
        status, chunks, behaviour = self.routes.get(path, (404, [b"not found"], {}))
        response = Response(status, list(chunks), **behaviour)
        self.responses[path] = response
        return response

    def call_count(self, path: str) -> int:
        return self.calls.count(path)


class StoreDouble:
    """The store, as a test can hold it open and count what it was asked.

    `async_load` blocks on `gate` when one is set, which is how the old race is
    reproduced deterministically: the restore is inside the store read, and the
    test decides when it may finish.
    """

    def __init__(self, payload: Any = None) -> None:
        self.payload = payload
        self.loads = 0
        self.saves = 0
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def async_load(self) -> Any:
        self.loads += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.payload

    async def async_save(self, data: Any) -> None:
        self.saves += 1
        self.payload = data

    async def async_remove(self) -> None:
        self.payload = None


class Clock:
    """An injected clock: the tests move time, nothing waits for it."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


class FakeTimer:
    """One scheduled callback, with the instant it was scheduled for."""

    def __init__(self, action: Callable[[datetime], None], when: datetime) -> None:
        self.action = action
        self.when = when
        self.cancelled = False
        self.fired = False

    def fire(self) -> None:
        self.fired = True
        self.action(self.when)


class FakeScheduler:
    """The scheduler seam: records instead of waiting, and fires on demand."""

    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(
        self, _hass: HomeAssistant, action: Callable[[datetime], None], when: datetime
    ) -> Callable[[], None]:
        timer = FakeTimer(action, when)
        self.timers.append(timer)

        def cancel() -> None:
            timer.cancelled = True

        return cancel

    @property
    def pending(self) -> list[FakeTimer]:
        return [timer for timer in self.timers if not timer.cancelled and not timer.fired]

    def fire_all(self) -> None:
        """Fire every timer that is currently live, in the order they were set."""
        for timer in list(self.pending):
            timer.fire()


SE4 = "SE4"


DE_LU = "DE-LU"


#: The zone each area the fixtures publish is in. The checked-in day document belongs to
#: SE4, so a document served for another area has to state that area's own zone: the
#: planner refuses a document whose zone disagrees with the catalogue, on purpose.
AREA_TZ = {SE4: "Europe/Stockholm", DE_LU: "Europe/Berlin", "DK1": "Europe/Copenhagen"}


def index_listing(area: str, days: list[str]) -> str:
    """An index document listing exactly these days for one area."""
    document = json.loads(fixture("index.json"))
    document["areas"] = {area: {"days": sorted(days), "res": 15}}
    return json.dumps(document)


def flat_day(
    area: str,
    when: Any,
    *,
    price: float = 0.10,
    fx: dict[str, float] | None = None,
    flat: bool = True,
) -> str:
    """A day document for one area, optionally with every price equal.

    Equal prices are what let a test build two plans with the *same* cost and the same
    slot count but different hours: nothing but the window differs, which is exactly the
    case a change-detector keyed on cost alone would wrongly suppress.
    """
    document = json.loads(day_body(area, when))
    document["tz"] = AREA_TZ.get(area, document["tz"])
    if flat:
        document["prices"] = [price] * len(document["prices"])
    if fx is not None:
        document["fx"] = fx
    return json.dumps(document)


def rising_day(area: str, when: Any, *, price: float = 0.10, step: float = 0.000001) -> str:
    """A day whose every quarter costs a little more than the one before (and a day more than the
    day before), so the cheapest slots are always the earliest ones: the plan starts at the first
    usable slot, whatever the departure.

    Equal prices go to the latest slots, so a flat day no longer plans from the clock.
    """
    document = json.loads(day_body(area, when))
    document["tz"] = AREA_TZ.get(area, document["tz"])
    later = (when - TODAY).days
    document["prices"] = [round(price + later * 0.001 + index * step, 7) for index in range(len(document["prices"]))]
    return json.dumps(document)


def cheap_night_day(area: str, when: Any, *, cheap_hours: int = 6, rising: bool = False) -> str:
    """A day whose night is cheap and whose day is expensive, by wall clock.

    This is what makes a *partly estimated* plan possible on purpose: the clock says 08:00,
    so today's cheap hours are already behind us, and the cheapest window that is still
    available is the one that runs through tonight into tomorrow's nights -- which are not
    published, and which the planner's fallback fills from today's own wall clocks.
    """
    document = json.loads(day_body(area, when))
    document["tz"] = AREA_TZ.get(area, document["tz"])
    cut = cheap_hours * 4
    rest = len(document["prices"]) - cut
    # `rising`: the expensive hours get dearer by the quarter, so the earliest of them are the cheapest
    # (equal prices would go to the latest slots).
    document["prices"] = [0.01] * cut + [round(0.50 + (index * 0.000001 if rising else 0.0), 7) for index in range(rest)]
    return json.dumps(document)


def cheap_midday_day(area: str, when: Any, *, cheap_hour: int = 12, cheap_hours: int = 4) -> str:
    """A day that is expensive everywhere except one cheap block in the middle.

    The block is what makes two *different* plans possible at the same total cost: one
    contiguous window that must buy its way across the expensive hours, or several windows
    that lean on the cheap block. Same money, same slot count, different hours — which is
    exactly the pair a change detector keyed on cost and count would confuse.
    """
    document = json.loads(day_body(area, when))
    document["tz"] = AREA_TZ.get(area, document["tz"])
    prices = [0.50] * len(document["prices"])
    for index in range(cheap_hour * 4, (cheap_hour + cheap_hours) * 4):
        prices[index] = 0.01
    document["prices"] = prices
    return json.dumps(document)


def serve_index(transport: StubTransport, listing: dict[str, list[Any]]) -> None:
    """An index listing these days for each of these areas, and nothing else."""
    document = json.loads(fixture("index.json"))
    document["areas"] = {
        area: {"days": sorted(day.isoformat() for day in days), "res": 15}
        for area, days in listing.items()
    }
    transport.serve("/v1/index.json", 200, json.dumps(document))


def serve(
    transport: StubTransport,
    area: str = SE4,
    *,
    days: tuple[Any, ...] = (TODAY, TOMORROW),
    listed: tuple[Any, ...] | None = None,
    flat: bool = False,
    rising: bool = False,
    fx: dict[str, float] | None = None,
) -> None:
    """Serve the catalogue, an index listing [listed] and every day in [days].

    `listed` defaults to `days`, so the ordinary case is one call: the relay says these
    days exist and serves them. Everything else is a deliberate disagreement between what
    the index says and what the relay answers, which is how the state tests are built.
    """
    transport.serve_area(area)
    for day in days:
        body = rising_day(area, day) if rising else flat_day(area, day, fx=fx, flat=flat)
        transport.serve(transport.day_path(area, day), 200, body)
    listing = days if listed is None else listed
    transport.serve("/v1/index.json", 200, index_listing(area, [day.isoformat() for day in listing]))


def profile_path(area: str = SE4) -> str:
    return f"/v1/{area}/profile.json"


def profile_document(
    area: str = SE4,
    *,
    generated: str = "2026-10-01T14:05:00+02:00",
    to: str = "2026-10-01",
    weeks: int = 4,
    median: float = 0.10,
    std: float = 0.01,
    n: int = 16,
    cheap: dict[tuple[int, int], float] | None = None,
    omit: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """A history profile as the relay publishes it (`/v1/<area>/profile.json`, contract v1).

    Every weekday-hour has the same `median` and `std`, except the ones named in `cheap` (a median of
    its own, same spread) and the ones in `omit` (left out, as the relay does for fewer than 8 samples).
    The window is the 28 local dates ending with `to`, the generation date.
    """
    last = date.fromisoformat(to)
    hours = []
    for weekday in range(1, 8):
        for hour in range(24):
            key = (weekday, hour)
            if omit is not None and key in omit:
                continue
            value = median if cheap is None or key not in cheap else cheap[key]
            hours.append({"weekday": weekday, "hour": hour, "median": value, "std": std, "n": n})
    return {
        "v": 1,
        "area": area,
        "tz": AREA_TZ.get(area, "Europe/Stockholm"),
        "unit": "EUR/kWh",
        "generated": generated,
        "from": (last - timedelta(days=27)).isoformat(),
        "to": to,
        "weeks": weeks,
        "hours": hours,
    }


def serve_profile(transport: StubTransport, area: str = SE4, **kwargs: Any) -> None:
    """Publish a history profile for one area (see [profile_document])."""
    transport.serve(profile_path(area), 200, json.dumps(profile_document(area, **kwargs)))
