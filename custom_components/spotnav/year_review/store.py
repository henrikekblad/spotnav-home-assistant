"""What the year's review keeps per site (`spotnav.year_review.<site entry id>`), written off the event loop
(`debug_day.DayStore`, `off_loop`):

    {schema, year, computed_at, through, review, day_parts: {date: {battery, car, car_est, heat, fuse, price}},
     fuse_since, users: {user_id: {year, viewed, dismissed}}, forced_until}

* **The review** last worked out (`year`, `through`, `computed_at`, `review`: `year_review/compute.py`'s shape), read
  by the cards and the service from memory, never worked out on a read.
* **The day parts** (`compute.DayParts`): what SpotNav saved per local day on the battery, the car and the heat, and
  the car's day-average estimate before 3.0, merged in every night (`merge_parts`), so a part whose source keeps
  fewer days (the heat's review 35 days) is still there at the year's end; a merge never takes a day or a part away.
  Trimmed to `KEPT_DAYS` back. Each day's `fuse` is the overload episodes counted that day (`add_fuse`), from
  `fuse_since`, the day the counter started on this site. Each day's `price` (`compute.PriceDay`, about 500 bytes
  for a day of quarters) is what the review reads of that day's prices, worked out once the day is over with that
  day's fees (`runtime.SiteYearReview._async_merge_prices`).
* **Each person's seen state** (`users`, by Home Assistant user id): whether they opened the review of `year`
  (`viewed`) or closed its banner (`dismissed`); a review of another year starts unseen. `reset_users` forgets all.
* **`forced_until`**: the service's showing (`spotnav.show_year_review`): the banner and the Support row are offered
  until then whatever the date.

A store that cannot be read starts empty. Never raises into the site.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
import logging
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..debug_day import DayStore
from .compute import DayParts

_LOGGER = logging.getLogger(__name__)

SCHEMA: Final = 1
#: The local days of parts kept (two years and a margin: last year's whole and this one's).
KEPT_DAYS: Final = 800
ACTION_VIEWED: Final = "viewed"
ACTION_DISMISSED: Final = "dismissed"
ACTIONS: Final = (ACTION_VIEWED, ACTION_DISMISSED)


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.year_review.{entry_id}"


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _year(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 2000 <= value <= 2200 else None


class YearReviewStore:
    """One site's kept review, day parts, fuse episodes and seen states (see the module)."""

    def __init__(self, hass: HomeAssistant, entry_id: str | None) -> None:
        self.year: int | None = None
        self.through: date | None = None
        self.computed_at: datetime | None = None
        self.review: dict[str, Any] | None = None
        self.day_parts: dict[date, DayParts] = {}
        self.fuse_since: date | None = None
        self.users: dict[str, dict[str, Any]] = {}
        self.forced_until: datetime | None = None
        self._store: DayStore | None = (
            None if entry_id is None else DayStore(hass, store_key(entry_id), self._snapshot, off_loop=True)
        )

    # ------------------------------------------------------------------------------------------------ kept

    def _snapshot(self) -> dict[str, Any]:
        """A fresh snapshot (written off the loop). The review is replaced whole, never changed in place."""
        return {
            "schema": SCHEMA,
            "year": self.year,
            "through": None if self.through is None else self.through.isoformat(),
            "computed_at": None if self.computed_at is None else self.computed_at.isoformat(),
            "review": self.review,
            "day_parts": {day.isoformat(): parts.as_dict() for day, parts in sorted(self.day_parts.items())},
            "fuse_since": None if self.fuse_since is None else self.fuse_since.isoformat(),
            "users": {user: dict(state) for user, state in self.users.items()},
            "forced_until": None if self.forced_until is None else self.forced_until.isoformat(),
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
            return
        self.year = _year(raw.get("year"))
        self.through = _date(raw.get("through"))
        computed = raw.get("computed_at")
        self.computed_at = dt_util.parse_datetime(computed) if isinstance(computed, str) else None
        review = raw.get("review")
        self.review = review if isinstance(review, dict) and self.year is not None else None
        parts = raw.get("day_parts")
        for key, item in parts.items() if isinstance(parts, Mapping) else ():
            day = _date(key)
            found = DayParts.from_dict(item)
            if day is not None and found is not None:
                self.day_parts[day] = found
        self.fuse_since = _date(raw.get("fuse_since"))
        users = raw.get("users")
        for user, state in users.items() if isinstance(users, Mapping) else ():
            year = _year(state.get("year")) if isinstance(state, Mapping) else None
            if isinstance(user, str) and year is not None:
                self.users[user] = {
                    "year": year,
                    ACTION_VIEWED: state.get(ACTION_VIEWED) is True,
                    ACTION_DISMISSED: state.get(ACTION_DISMISSED) is True,
                }
        forced = raw.get("forced_until")
        self.forced_until = dt_util.parse_datetime(forced) if isinstance(forced, str) else None

    async def async_load(self, today: date) -> None:
        """Read what is kept; the fuse's counter starts today where it had not."""
        if self._store is not None:
            raw = await self._store.async_load()
            try:
                self.restore(raw)
            except Exception:  # noqa: BLE001 - a store that cannot be read starts again
                _LOGGER.debug("SpotNav year review: the kept review could not be read", exc_info=True)
        if self.fuse_since is None:
            self.fuse_since = today
            self._changed()

    async def async_close(self) -> None:
        if self._store is not None:
            await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The site is gone for good: forget its review."""
        await DayStore.async_remove(hass, store_key(entry_id))

    def _changed(self) -> None:
        if self._store is not None:
            self._store.changed()

    # ------------------------------------------------------------------------------------------------ changes

    @callback
    def merge_parts(self, parts: Mapping[date, DayParts], today: date) -> None:
        """Lay `parts` over the kept days (`DayParts.merged`: nothing is taken away) and trim to `KEPT_DAYS`."""
        for day, newer in parts.items():
            held = self.day_parts.get(day)
            self.day_parts[day] = newer if held is None else held.merged(newer)
        oldest = today - timedelta(days=KEPT_DAYS)
        for day in [day for day in self.day_parts if day < oldest]:
            del self.day_parts[day]
        self._changed()

    @callback
    def add_fuse(self, day: date) -> None:
        """One overload episode began on `day`."""
        held = self.day_parts.get(day, DayParts())
        self.day_parts[day] = replace(held, fuse=held.fuse + 1)
        if self.fuse_since is None:
            self.fuse_since = day
        self._changed()

    @callback
    def set_review(self, year: int, through: date, review: dict[str, Any], computed_at: datetime) -> None:
        self.year, self.through, self.review, self.computed_at = year, through, review, computed_at
        self._changed()

    @callback
    def force(self, until: datetime) -> None:
        self.forced_until = until
        self._changed()

    def forced(self, now: datetime) -> bool:
        return self.forced_until is not None and now < self.forced_until

    def seen(self, user_id: str | None, year: int) -> tuple[bool, bool]:
        """Whether the person opened the review of `year`, and whether they closed its banner."""
        state = self.users.get(user_id or "")
        if state is None or state.get("year") != year:
            return False, False
        return bool(state.get(ACTION_VIEWED)), bool(state.get(ACTION_DISMISSED))

    @callback
    def mark(self, user_id: str, year: int, action: str) -> None:
        """The person opened the review of `year` (`viewed`) or closed its banner (`dismissed`)."""
        if action not in ACTIONS:
            raise ValueError(action)
        state = self.users.get(user_id)
        if state is None or state.get("year") != year:
            state = {"year": year, ACTION_VIEWED: False, ACTION_DISMISSED: False}
        self.users[user_id] = {**state, action: True}
        self._changed()

    @callback
    def reset_users(self) -> None:
        self.users = {}
        self._changed()
