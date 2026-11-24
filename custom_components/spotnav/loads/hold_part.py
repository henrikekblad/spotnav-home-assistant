"""One low-risk load's control state in Home Assistant (the pool's circulation and heat pump, the spa, the sauna):
the settings SpotNav holds on it (`loads/hold.Held`), its budgets, its pause and its counted failures, and the write
that every part makes the same way: kept first, then called, read back at the next looks (`loads/hold.py`).

A part is in one of these states (the card's line under the load):

* `off`: the opt-in is off: SpotNav only says what it would do (`would`).
* `controlled`: SpotNav sets it by the plan and the hard rules.
* `paused`: controlled but waiting, with `reason` and `paused_until` where it waits to the next local day: a write not
  read back (`no_read_back`, counted in `failures`), the day's write or start budget (`write_cap`, `starts_cap`), a
  fault (`fault`), an automation that steers it (`automation`), unreadable (`unavailable`), a mode SpotNav never
  writes (`foreign_mode`), its own scheduler (`own_scheduler`).
* `handed_back`: a change SpotNav did not make was kept: the load is the person's to the next local day
  (`foreign_change`).
* `season_closed`: the pool's season is closed: what SpotNav held is handed back, then nothing is written at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant

from .. import time_zones
from ..core.parse import aware_instant
from .hold import (
    OBSERVED_FAILED,
    OBSERVED_FOREIGN,
    Budget,
    Held,
    Value,
    same,
)

_LOGGER = logging.getLogger(__name__)

CALL_TIMEOUT_S: Final = 10.0
#: A write of the person's value that did not show is tried again this often while the entity reads SpotNav's value.
HOME_RETRY: Final = timedelta(minutes=10)

STATE_OFF: Final = "off"
STATE_CONTROLLED: Final = "controlled"
STATE_PAUSED: Final = "paused"
STATE_HANDED_BACK: Final = "handed_back"
STATE_SEASON_CLOSED: Final = "season_closed"

REASON_NO_READ_BACK: Final = "no_read_back"
REASON_WRITE_CAP: Final = "write_cap"
REASON_STARTS_CAP: Final = "starts_cap"
REASON_FOREIGN_CHANGE: Final = "foreign_change"
REASON_FOREIGN_MODE: Final = "foreign_mode"
REASON_FAULT: Final = "fault"
REASON_AUTOMATION: Final = "automation"
REASON_UNAVAILABLE: Final = "unavailable"
REASON_CLOUD: Final = "cloud_slow"

#: A service call: `(domain, service, data)`.
Call = tuple[str, str, dict[str, Any]]


class HoldPart:
    """One load's control state (see the module). `tz` gives the site's time zone, `log` records a line."""

    def __init__(self, hass: HomeAssistant, name: str, tz: Callable[[], str], log: Callable[..., None]) -> None:
        self.hass = hass
        self.name = name
        self._tz = tz
        self._log = log
        self.helds: dict[str, Held] = {}
        self.budget = Budget()
        self.paused_until: datetime | None = None
        self.paused_reason: str | None = None
        #: Writes not read back (or refused), ever and per local day.
        self.failures = 0
        self.failures_day: dict[str, int] = {}
        self.state = STATE_OFF
        self.reason: str | None = None
        self.automation: Any = None
        #: Held at the last stop (or a crash): the person's values are written back before anything else.
        self.restart_restore = False

    # ---- kept

    def stored(self) -> dict[str, Any]:
        return {
            "helds": {key: held.stored() for key, held in self.helds.items()},
            "budget": self.budget.stored(),
            "paused_until": None if self.paused_until is None else self.paused_until.isoformat(),
            "paused_reason": self.paused_reason,
            "failures": self.failures,
            "failures_day": dict(self.failures_day),
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        helds = raw.get("helds")
        if isinstance(helds, Mapping):
            self.helds = {key: Held.from_stored(key, item) for key, item in helds.items() if isinstance(key, str)}
        self.budget = Budget.from_stored(raw.get("budget"))
        self.paused_until = aware_instant(raw.get("paused_until"))
        reason = raw.get("paused_reason")
        self.paused_reason = reason if isinstance(reason, str) else None
        failures = raw.get("failures")
        self.failures = failures if isinstance(failures, int) and not isinstance(failures, bool) else 0
        days = raw.get("failures_day")
        if isinstance(days, Mapping):
            self.failures_day = {d: n for d, n in days.items() if isinstance(d, str) and isinstance(n, int)}
        self.restart_restore = self.holds()
        if self.restart_restore:
            _LOGGER.info("SpotNav %s: held at the last stop; the person's values are written back first", self.name)

    # ---- helpers

    def held(self, key: str) -> Held:
        found = self.helds.get(key)
        if found is None:
            found = self.helds[key] = Held(key)
        return found

    def holds(self) -> bool:
        return any(held.holds() for held in self.helds.values())

    def day(self, now: datetime) -> str:
        return now.astimezone(time_zones.require_zone(self._tz())).date().isoformat()

    def next_day(self, now: datetime) -> datetime:
        zone = time_zones.require_zone(self._tz())
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone).astimezone(now.tzinfo)

    def pause(self, now: datetime, reason: str) -> None:
        """Wait to the next local day (`reason`); a pause already standing keeps its reason."""
        if self.paused_until is not None and self.paused_until > now:
            return
        self.paused_until = self.next_day(now)
        self.paused_reason = reason
        self._log(now, self.name, "paused", why=reason)

    def paused(self, now: datetime) -> bool:
        if self.paused_until is not None and self.paused_until <= now:
            self.paused_until = self.paused_reason = None
        return self.paused_until is not None

    def count_failure(self, now: datetime) -> None:
        self.failures += 1
        day = self.day(now)
        self.failures_day[day] = self.failures_day.get(day, 0) + 1

    def trim(self, now: datetime) -> None:
        keep = {self.day(now), self.day(now - timedelta(days=1))}
        self.budget.trim(keep, now)
        self.failures_day = {d: n for d, n in self.failures_day.items() if d in keep}

    # ---- reads and writes

    def observe(self, key: str, read: Value | None, now: datetime, read_back: timedelta) -> str | None:
        """Judge one setting's reading; a failure is counted and pauses the load, a foreign change hands it back."""
        held = self.held(key)
        found = held.observe(read, now, read_back)
        if found == OBSERVED_FAILED:
            self.count_failure(now)
            self._log(now, self.name, "not_read_back", key=key)
            _LOGGER.info("SpotNav %s: a write to %s did not read back; paused to the next day", self.name, key)
            self.pause(now, REASON_NO_READ_BACK)
        elif found == OBSERVED_FOREIGN:
            self._log(now, self.name, "handed_back", key=key, read=read)
            _LOGGER.info("SpotNav %s: %s was changed by someone else; handed back to the next day", self.name, key)
            self.paused_until, self.paused_reason = None, None
            self.pause(now, REASON_FOREIGN_CHANGE)
        return found

    async def async_write(
        self,
        key: str,
        call: Call,
        value: Value,
        now: datetime,
        *,
        kind: str,
        limit: int | None,
        home: bool = False,
    ) -> bool:
        """Write one setting: within the day's budget (`limit`, `None`: exempt, a hard rule's or an exit's write, which
        is not counted either), noted as pending, then called. `False` when the budget refused it."""
        day = self.day(now)
        if limit is not None:
            if not self.budget.may_write(day, key, kind, limit):
                return False
            # Only a write the budget limits spends it: a hard rule's or an exit's write never uses up another's.
            self.budget.note_write(day, key, kind)
        held = self.held(key)
        held.wrote(value, now, home=home)
        self._log(now, self.name, "write", key=key, value=value, home=home)
        domain, service, data = call
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                await self.hass.services.async_call(domain, service, data, blocking=True)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - refused: counted at once, the load paused to the next day
            _LOGGER.warning("SpotNav %s: %s.%s on %s failed: %s", self.name, domain, service, data.get("entity_id"), err)
            held.pending = None
            if not home:
                held.prior, held.written = held.written, value
            else:
                held.home_failed_at = now
            self.count_failure(now)
            self.pause(now, REASON_NO_READ_BACK)
        return True

    async def async_give_back(self, key: str, read: Value | None, call: Callable[[Value], Call | None], now: datetime) -> bool:
        """Write the person's value back where the entity still reads SpotNav's; someone else's value is left and
        becomes the person's. `True` when nothing of SpotNav's is left on the setting."""
        held = self.helds.get(key)
        if held is None or not held.holds():
            return True
        if held.pending is not None:
            return False
        if read is None:
            # Unreadable: written back at its first readable look.
            return False
        if not held.ours(read) or held.person is None or same(read, held.person):
            held.drop(read if not held.ours(read) else held.person)
            return True
        if held.home_failed_at is not None and now - held.home_failed_at < HOME_RETRY:
            return False
        made = call(held.person)
        if made is None:
            # Never written back (a value SpotNav never writes): left as SpotNav's last write.
            held.drop(read)
            return True
        await self.async_write(key, made, held.person, now, kind="home", limit=None, home=True)
        return False

    def status(self, now: datetime, *, enabled: bool, live: bool) -> dict[str, Any]:
        paused = self.paused(now)
        return {
            "state": self.state if enabled or self.holds() else STATE_OFF,
            "reason": self.reason,
            "paused_until": None if not paused or self.paused_until is None else self.paused_until.isoformat(),
            "failures": self.failures,
            "live": live and enabled and self.state == STATE_CONTROLLED,
        }

