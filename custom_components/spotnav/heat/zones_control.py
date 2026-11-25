"""SpotNav setting the heating zones' thermostats (plan `plans/v3_zones_water_heater.md` section 2, the owner's
decisions of 2026-11-20), beside the heat pump's control and owned by the same site controller; the pump's control is
untouched. The rules are pure (`heat/zone_control.py`); this is the shell that reads and writes.

**The opt-in** is per zone (`zones[].control`, off by default): a zone chosen without it is watched only. Nothing is
written without it.

**Each look** (`LOOK_S`, at once after a heat settings write), per zone, never raises:

1. **A write is read back** within `READ_BACK` (the setpoint within one step of the value written). Not seen: the
   person's value is written back and the zone paused to the next local day (`no_read_back`); during the check it is
   the check's failure.
2. **The person's value** (`home_c`) is the setpoint read while SpotNav holds nothing, taken again on every change it
   did not make. **Someone else's change wins**: the setpoint reading other than SpotNav's last write for
   `FOREIGN_CONFIRM` while it holds one gives the zone up: nothing written back, the value read becomes the person's,
   the zone paused to the next local day (`foreign_change`). The mode leaving `heat` (or the setpoint no longer
   shown) while the thermostat still reads SpotNav's value pauses the zone the same way and writes nothing in the
   other mode, but SpotNav's value never becomes the person's: it is written back at the first look in `heat` mode
   that still reads it.
3. **Exits** (the opt-in off, a pause, a block, the zone no longer qualifying, the day's write cap, unload, Home
   Assistant's stop, and at the next start after a stop that could not): the person's value is written back where the
   thermostat still reads SpotNav's; a thermostat that does not read is written back at its first readable look. A
   value someone else set is left. A write of the person's value that does not show is tried again (`HOME_TRIES` at
   once, then every `HOME_RETRY`) for as long as the thermostat reads SpotNav's value: it is never let go there.
4. **Blocks**: an enabled automation whose actions reference the thermostat (`heat/automations.py`, the heat card's
   one note `automation_steers` with `blocks`) keeps SpotNav off it; SpotNav never turns an automation off.
5. **The check**, once per thermostat (again after the opt-in is turned off and on): the first write is read back;
   where it is a lowering and the zone heats (its power sensor draws, or `hvac_action` says `heating`) while it reads at
   or above the lowered setpoint, the effect must show within `EFFECT_GRACE` (the power down to `EFFECT_SHARE` or the
   action idle). Not seen: `check_failed`, the person's value written back, a line under the zone's switch.
6. **Control**: `zone_control.decide` on the heat plan as first made (not the hot water's level A), the zone's own
   reading, its lowerings of the last day and its writes; `stale` while the plan is older than `FRESH_PLAN` or no
   outdoor reading, `too_cold` at -15 °C or colder, `settling` until good data has held `GOOD_FOR`.

7. **The ceiling** (`site/shed_order.py`): where the site's shed order lowers the heat for the ceiling, an hour the
   plan leaves normal is lowered 1 °C until the shed's end (`ceiling`), by the same rules as the plan's lowering (the
   zone cold, the rest, the day's lowered time and writes). What each zone could give (its measured power) is offered
   at each look (`offers`).

Kept per zone in the heat control's store (`spotnav.heat_control.<entry>`, key `zones`): the person's value, what
SpotNav wrote and the value before it, since when and why, the day's writes, the lowerings, the pause and the check.

**Forecast**: a lowered metered zone takes its last day's mean power off the house forecast over its lowerings (the one
running and those the plan has ahead) and gives the same energy back over the two hours after (`forecast_delta`).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..util import error_text
from .. import time_zones
from ..core.parse import aware_instant, finite_number
from ..const import DOMAIN
from ..site.shed_order import LOAD_HEAT, REASON_CEILING, Offer
from .automations import SteeringAutomation, steering_automations
from .config import HeatConfig
from .relay_control import (
    GOOD_FOR,
    MIN_LOWER,
    NO_LOWER_AT_C,
    REASON_PLANNED,
    SAFETY_SETTLING,
    SAFETY_STALE,
    SAFETY_TOO_COLD,
    Situation,
    closings_ahead,
    decide as relay_decide,
    lowered_within,
    planned_run_end,
)
from .shadow_rule import ACTION_LOWER, ACTION_NORMAL, BandHour, would_do
from .zone_control import (
    COMFORT_C,
    MAX_WRITES_DAY,
    RAISES,
    REASON_ZONE_COLD,
    REASON_ZONE_WARM,
    WHY_NOT_HEAT,
    WHY_UNAVAILABLE,
    Limits,
    ZoneDecision,
    ZoneSituation,
    decide,
    forecast_delta,
    qualifies,
    same,
    taken,
)

if TYPE_CHECKING:
    from .control import HeatControl
    from .runtime import HeatShadow

_LOGGER = logging.getLogger(__name__)

LOOK_S: Final = 30.0
READ_BACK: Final = timedelta(seconds=90)
FOREIGN_CONFIRM: Final = timedelta(seconds=60)
FRESH_PLAN: Final = timedelta(hours=2)
EFFECT_GRACE: Final = timedelta(minutes=10)
EFFECT_SHARE: Final = 0.3
#: The effect (or its absence past the grace) must be seen on this many looks in a row.
EFFECT_LOOKS: Final = 2
#: A zone's power that counts as drawing (W).
DRAWING_W: Final = 50.0
CALL_TIMEOUT_S: Final = 10.0
SHUTDOWN_WAIT_S: Final = 5.0
DAYS_KEPT: Final = 3
LOG_SIZE: Final = 100
#: The zones' mean power is read again this often.
POWER_REFRESH: Final = timedelta(hours=1)
#: Writes of the person's value that may go unread in a row before the next ones are spaced `HOME_RETRY` apart.
HOME_TRIES: Final = 3
HOME_RETRY: Final = timedelta(minutes=30)

STATE_OFF: Final = "off"
STATE_VERIFYING: Final = "verifying"
STATE_RUNNING: Final = "running"
STATE_WAITING: Final = "waiting"

WAIT_UNAVAILABLE: Final = "unavailable"
WAIT_NOT_QUALIFIED: Final = "not_qualified"
WAIT_AUTOMATION: Final = "automation"
WAIT_FOREIGN_CHANGE: Final = "foreign_change"
WAIT_NO_READ_BACK: Final = "no_read_back"
WAIT_CHECK_FAILED: Final = "check_failed"
WAIT_WRITE_CAP: Final = "write_cap"

CHECK_PASSED: Final = "passed"
CHECK_FAILED: Final = "failed"


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


@dataclass(slots=True)
class Pending:
    value: float
    at: datetime
    #: Writing the person's own value back (an exit), not a value SpotNav holds.
    home: bool
    action: str
    #: What the thermostat showed for SpotNav before this write (`None`: not known): still showing it is no read-back.
    before: float | None = None


class ZoneState:
    """One zone's kept state and what was seen at the last look."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        # Kept.
        self.home_c: float | None = None
        #: The value SpotNav holds on the thermostat (`None`: nothing), and the one it held before (a write that may
        #: not have taken leaves that one).
        self.written_c: float | None = None
        self.prior_c: float | None = None
        self.action: str | None = None
        self.since: datetime | None = None
        self.writes: dict[str, int] = {}
        self.lowerings: list[list[datetime | None]] = []
        self.last_end: datetime | None = None
        self.raise_ended: datetime | None = None
        self.paused_until: datetime | None = None
        self.paused_reason: str | None = None
        #: `{result, at}` once the check has a verdict.
        self.check: dict[str, Any] | None = None
        # Seen.
        self.pending: Pending | None = None
        self.foreign_since: datetime | None = None
        self.effect: dict[str, Any] | None = None
        self.state = STATE_OFF
        self.reason: str | None = None
        self.why: str | None = None
        self.decision: ZoneDecision | None = None
        self.automation: SteeringAutomation | None = None
        self.was_enabled: bool | None = None
        #: Held at the last stop: written back at the first readable look before anything else.
        self.restore = False
        #: Writes of the person's value in a row that were not read back.
        self.home_failures = 0
        #: The next try to write the person's value back after `HOME_TRIES` that did not show.
        self.home_retry_at: datetime | None = None

    def stored(self) -> dict[str, Any]:
        return {
            "home_c": self.home_c,
            "written_c": self.written_c,
            "prior_c": self.prior_c,
            "action": self.action,
            "since": _iso(self.since),
            "writes": dict(self.writes),
            "lowerings": [[_iso(start), _iso(end)] for start, end in self.lowerings],
            "last_end": _iso(self.last_end),
            "raise_ended": _iso(self.raise_ended),
            "paused_until": _iso(self.paused_until),
            "paused_reason": self.paused_reason,
            "check": None if self.check is None else dict(self.check),
        }

    @classmethod
    def from_stored(cls, entity_id: str, raw: Any) -> ZoneState:
        zone = cls(entity_id)
        if not isinstance(raw, dict):
            return zone
        zone.home_c = finite_number(raw.get("home_c"))
        zone.written_c = finite_number(raw.get("written_c"))
        zone.prior_c = finite_number(raw.get("prior_c"))
        zone.action = raw.get("action") if isinstance(raw.get("action"), str) else None
        zone.since = aware_instant(raw.get("since"))
        writes = raw.get("writes")
        if isinstance(writes, dict):
            zone.writes = {day: count for day, count in writes.items() if isinstance(day, str) and isinstance(count, int)}
        for item in raw.get("lowerings") or []:
            if isinstance(item, list) and len(item) == 2 and (start := aware_instant(item[0])) is not None:
                zone.lowerings.append([start, aware_instant(item[1])])
        zone.last_end = aware_instant(raw.get("last_end"))
        zone.raise_ended = aware_instant(raw.get("raise_ended"))
        zone.paused_until = aware_instant(raw.get("paused_until"))
        zone.paused_reason = raw.get("paused_reason") if isinstance(raw.get("paused_reason"), str) else None
        zone.check = raw.get("check") if isinstance(raw.get("check"), dict) else None
        zone.restore = zone.written_c is not None
        return zone

    def holds(self) -> bool:
        return self.written_c is not None or (self.pending is not None and not self.pending.home)

    def held_lower(self) -> bool:
        return self.written_c is not None and self.action == ACTION_LOWER

    def ours(self, read: float | None, limits: Limits) -> bool:
        """Whether the setpoint read is SpotNav's (what it wrote, or the value before a write that may not have taken)."""
        return same(read, self.written_c, limits) or same(read, self.prior_c, limits)


class ZonesControl:
    """The site's zones' control (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, shadow: HeatShadow, control: HeatControl) -> None:
        self.hass = hass
        self.shadow = shadow
        self.control = control
        self.site = shadow.site
        self.zones: dict[str, ZoneState] = {}
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        #: Each metered zone's mean power over its last day (kW), read hourly.
        self.kw: dict[str, float] = {}
        self._kw_at: datetime | None = None
        self._good_since: datetime | None = None
        self._closed = False
        self._started = False
        self._looking = False
        self._again = False
        self._task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        #: Per zone chosen: what a lowering for the ceiling would give and whether its rules allow it (last look).
        self.offers: dict[str, Offer] = {}
        control.parts["zones"] = self
        shadow.extra_deltas.append(self.forecast_delta)

    # ---- kept state (in the heat control's store)

    def stored(self) -> dict[str, Any]:
        return {"zones": {entity_id: zone.stored() for entity_id, zone in self.zones.items()}}

    def restore(self, raw: Any) -> None:
        zones = raw.get("zones") if isinstance(raw, dict) else None
        self.zones = (
            {entity_id: ZoneState.from_stored(entity_id, item) for entity_id, item in zones.items() if isinstance(entity_id, str)}
            if isinstance(zones, dict)
            else {}
        )
        for zone in self.zones.values():
            if zone.restore:
                _LOGGER.info("SpotNav zones: %s was held at %s at the last stop; setting it back", zone.entity_id, zone.written_c)

    # ---- life cycle

    async def async_start(self) -> None:
        self._started = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=LOOK_S))
        self.request_look()

    @callback
    def halt(self) -> None:
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None

    async def async_shutdown(self) -> None:
        """Write the person's value back on every thermostat SpotNav holds (bounded); one that does not take stays kept
        and is written back at the next start."""
        self.halt()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    await asyncio.wait({self._task})
            except TimeoutError:
                _LOGGER.warning("SpotNav zones: a look did not end at the stop")
        if not self._started:
            return
        now = dt_util.utcnow()
        # All zones at once: one slow thermostat must not use up the stop's time for the others.
        await asyncio.gather(*(self._async_set_back_at_stop(zone, now) for zone in list(self.zones.values())))

    async def _async_set_back_at_stop(self, zone: ZoneState, now: datetime) -> None:
        """One zone's set-back at the stop, bounded on its own; a failure keeps it held for the next start."""
        if zone.written_c is None or zone.home_c is None:
            return
        state = self.hass.states.get(zone.entity_id)
        attributes = {} if state is None else state.attributes
        limits = Limits.of(attributes)
        read = finite_number(attributes.get("temperature"))
        if read is None or not zone.ours(read, limits) or state is None or state.state != "heat":
            # Someone else's value, or not in heat mode: nothing written; kept, and judged at the next start.
            return
        try:
            await self._async_set(zone.entity_id, zone.home_c)
            async with asyncio.timeout(SHUTDOWN_WAIT_S):
                while not taken(self._setpoint(zone.entity_id), zone.home_c, read, limits):
                    await asyncio.sleep(0.2)
            self._handed_back(zone, now)
        except Exception:  # noqa: BLE001 - kept held: written back at the next start
            _LOGGER.warning("SpotNav zones: %s could not be set back at the stop", zone.entity_id, exc_info=True)

    # ---- looks

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.request_look()

    @callback
    def request_look(self) -> None:
        if self._closed or not self._started:
            return
        if self._looking:
            self._again = True
            return
        self._looking = True
        self._task = self.hass.async_create_background_task(self._async_looks(), f"{DOMAIN} zones {self.site.entry_id}")

    async def _async_looks(self) -> None:
        try:
            while True:
                self._again = False
                await self.async_look(dt_util.utcnow())
                if not self._again or self._closed:
                    break
        finally:
            self._looking = False

    async def async_look(self, now: datetime) -> None:
        """One look over every zone (see the module). Never raises."""
        try:
            config = self.shadow.config()
        except Exception:  # noqa: BLE001 - tried again at the next look
            _LOGGER.warning("SpotNav zones: the settings could not be read", exc_info=True)
            return
        chosen = {zone.entity_id: zone for zone in config.zones}
        safety = self._safety(config, now)
        for entity_id in list(dict.fromkeys([*chosen, *self.zones])):
            zone = self.zones.get(entity_id)
            enabled = entity_id in chosen and chosen[entity_id].control
            if zone is None:
                if not enabled:
                    continue
                zone = self.zones[entity_id] = ZoneState(entity_id)
            try:
                await self._async_zone(zone, config, enabled, safety, now)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next look tries again; the person's value is written back where it can be
                _LOGGER.warning("SpotNav zones: a look at %s failed", entity_id, exc_info=True)
                zone.state, zone.reason = STATE_WAITING, "error"
                try:
                    await self._async_exit(zone, now, "error")
                except Exception:  # noqa: BLE001 - tried again at the next look
                    _LOGGER.warning("SpotNav zones: %s could not be set back", entity_id, exc_info=True)
            if entity_id not in chosen and not zone.holds() and zone.pending is None:
                # No longer a zone and nothing of SpotNav's on it: forgotten.
                del self.zones[entity_id]
        self._trim(now)
        await self._async_power(now)
        try:
            self.offers = {zone.entity_id: self._offer(zone, config, safety, now) for zone in config.zones}
        except Exception:  # noqa: BLE001 - nothing offered: the shed order skips the zones
            _LOGGER.debug("SpotNav zones: their offer to the shed order could not be made", exc_info=True)
            self.offers = {}
        self.control.keep_soon()

    # ---- the shed order (`site/shed_order.py`)

    def _shed_until(self, now: datetime) -> datetime | None:
        order = getattr(self.site, "shed_order", None)
        return None if order is None else order.until(LOAD_HEAT, now)

    def _offer(self, chosen: Any, config: HeatConfig, safety: str | None, now: datetime) -> Offer:
        """One zone's offer to the shed order (`shed_order.Offer`): its measured power, and whether the zone's rules
        would lower it (or keep it lowered) for the ceiling now. A watched zone: `would` only."""
        give = max(0.0, self.kw.get(chosen.entity_id) or 0.0) * 1000.0
        plan = self.shadow.first_plan or self.shadow.plan
        hour = self._plan_hour(plan, now)
        action = None if hour is None else hour.action
        zone = self.zones.get(chosen.entity_id)
        if not chosen.control or zone is None:
            ready = action == ACTION_NORMAL and safety in (None, SAFETY_SETTLING)
            return Offer(live=False, ready=ready, give_w=give, why=None if ready else (safety or action))
        if zone.state != STATE_RUNNING:
            return Offer(live=True, ready=False, give_w=give, why=zone.reason or zone.state)
        if zone.held_lower():
            if self._shed_until(now) is not None and zone.decision is not None and zone.decision.action == ACTION_LOWER:
                # Held once its setpoint has read back; until then it is still being taken.
                read_back = zone.pending is None
                return Offer(live=True, ready=True, give_w=give, held=read_back, pending=not read_back)
            # Lowered by the plan already: one step is all the shed takes.
            return Offer(live=True, ready=False, give_w=0.0, why=REASON_PLANNED)
        if self._shed_until(now) is not None and zone.decision is not None and zone.decision.action == ACTION_LOWER:
            # Decided and written, not read back yet: still being taken.
            return Offer(live=True, ready=True, give_w=give, pending=True)
        if action != ACTION_NORMAL:
            return Offer(live=True, ready=False, give_w=give, why=action or SAFETY_STALE)
        state = self.hass.states.get(chosen.entity_id)
        current = None if state is None else finite_number(state.attributes.get("current_temperature"))
        if current is not None and zone.home_c is not None and current < zone.home_c - COMFORT_C:
            return Offer(live=True, ready=False, give_w=give, why=REASON_ZONE_COLD)
        trial = relay_decide(
            Situation(
                now=now,
                plan_until=now + MIN_LOWER,
                held_since=None,
                last_end=zone.last_end,
                lowered_24h=self._lowered_24h(zone, now),
                writes_today=zone.writes.get(self._day(now), 0),
                safety=safety,
                max_writes=MAX_WRITES_DAY,
            )
        )
        return Offer(live=True, ready=trial.closed, give_w=give, why=None if trial.closed else trial.reason)

    def _setpoint(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        return None if state is None else finite_number(state.attributes.get("temperature"))

    async def _async_zone(self, zone: ZoneState, config: HeatConfig, enabled: bool, safety: str | None, now: datetime) -> None:
        state = self.hass.states.get(zone.entity_id)
        attributes: dict[str, Any] = {} if state is None else dict(state.attributes)
        mode = None if state is None else state.state
        limits = Limits.of(attributes)
        read = finite_number(attributes.get("temperature"))
        available = mode is not None and mode not in ("unavailable", "unknown")
        zone.why = qualifies(mode, attributes)

        if enabled and zone.was_enabled is False:
            # Turned on again: a failed check and a pause are tried afresh.
            if zone.check is not None and zone.check.get("result") == CHECK_FAILED:
                zone.check = None
            zone.paused_until = zone.paused_reason = None
        zone.was_enabled = enabled
        if zone.paused_until is not None and now >= zone.paused_until:
            zone.paused_until = zone.paused_reason = None

        # A write is read back first.
        if zone.pending is not None:
            pending = zone.pending
            if available and taken(read, pending.value, pending.before, limits):
                self._confirm(zone, pending, now)
            elif now - pending.at > READ_BACK:
                zone.pending = None
                self._log(now, zone, "not_read_back", value=pending.value)
                if not pending.home:
                    # A value of SpotNav's that did not show: the person's value back, and no more for the day (the
                    # check's failure on the first write). The person's value that did not show is tried again at the
                    # next look: it is still SpotNav's to give back.
                    if zone.check is None:
                        zone.check = {"result": CHECK_FAILED, "at": now.isoformat(), "why": "no_read_back"}
                    else:
                        self._pause(zone, now, WAIT_NO_READ_BACK)
                    await self._async_exit(zone, now, "not_read_back")
                    zone.state, zone.reason = STATE_WAITING, WAIT_CHECK_FAILED if self._check_failed(zone) else WAIT_NO_READ_BACK
                else:
                    zone.home_failures += 1
                    if zone.home_failures >= HOME_TRIES:
                        # The thermostat takes no write now: tried again every `HOME_RETRY` while it reads SpotNav's
                        # value, never let go there (SpotNav's value must not pass for the person's).
                        if zone.home_failures == HOME_TRIES:
                            _LOGGER.warning(
                                "SpotNav zones: %s does not take its own value %s back; trying again every %s",
                                zone.entity_id,
                                pending.value,
                                HOME_RETRY,
                            )
                        self._log(now, zone, "home_not_taken", tries=zone.home_failures)
                        zone.home_retry_at = now + HOME_RETRY
                    self._pause(zone, now, WAIT_NO_READ_BACK)
                    zone.state, zone.reason = STATE_WAITING, WAIT_NO_READ_BACK
                return
            else:
                return

        if not available:
            # Nothing written to a thermostat that does not read: what SpotNav holds is written back at its first
            # readable look.
            zone.state, zone.reason = (STATE_WAITING, WAIT_UNAVAILABLE) if enabled else (STATE_OFF, None)
            zone.foreign_since = None
            return

        if zone.restore:
            # Held at the last stop: written back before anything else (only where it still reads SpotNav's value).
            zone.restore = False
            await self._async_exit(zone, now, "restart")
            if zone.pending is not None:
                return

        # Someone else's change wins.
        if zone.written_c is not None:
            if read is None or (mode != "heat" and zone.ours(read, limits)):
                # The mode left `heat` (or no setpoint is shown) while the thermostat holds SpotNav's value: nothing
                # is written in another mode, and SpotNav's value is not the person's. Kept, paused for the day, and
                # written back at the first look in heat mode that still reads it.
                if zone.foreign_since is None and zone.reason != WAIT_FOREIGN_CHANGE:
                    self._log(now, zone, "mode_left_heat", read=read, mode=mode)
                    _LOGGER.info("SpotNav zones: %s left heat mode while held; set back when it heats again", zone.entity_id)
                self._end(zone, now)
                zone.effect = None
                zone.foreign_since = None
                self._pause(zone, now, WAIT_FOREIGN_CHANGE)
                zone.state, zone.reason = STATE_WAITING, WAIT_FOREIGN_CHANGE
                return
            if not zone.ours(read, limits):
                zone.foreign_since = zone.foreign_since or now
                if now - zone.foreign_since >= FOREIGN_CONFIRM:
                    self._log(now, zone, "foreign_change", read=read, mode=mode)
                    _LOGGER.info("SpotNav zones: %s was changed by someone else; given up for the day", zone.entity_id)
                    self._end(zone, now)
                    zone.written_c = zone.prior_c = zone.action = None
                    if read is not None:
                        zone.home_c = read
                    zone.foreign_since = None
                    self._pause(zone, now, WAIT_FOREIGN_CHANGE)
                    zone.state, zone.reason = STATE_WAITING, WAIT_FOREIGN_CHANGE
                    return
                zone.state, zone.reason = STATE_WAITING, WAIT_FOREIGN_CHANGE
                return
            zone.foreign_since = None
        elif read is not None and zone.pending is None:
            # Holding nothing: what the thermostat reads is the person's value.
            if zone.home_c is None or not same(read, zone.home_c, limits):
                if zone.home_c is not None:
                    self._log(now, zone, "home", value=read)
                zone.home_c = read

        if not enabled:
            await self._async_exit(zone, now, "off")
            zone.state, zone.reason = STATE_OFF, None
            return
        if zone.paused_until is not None:
            await self._async_exit(zone, now, zone.paused_reason or "paused")
            zone.state, zone.reason = STATE_WAITING, zone.paused_reason
            return
        if zone.why is not None:
            await self._async_exit(zone, now, zone.why)
            zone.state, zone.reason = STATE_WAITING, WAIT_NOT_QUALIFIED
            return
        zone.automation = next(iter(steering_automations(self.hass, [zone.entity_id])), None)
        if zone.automation is not None:
            await self._async_exit(zone, now, WAIT_AUTOMATION)
            zone.state, zone.reason = STATE_WAITING, WAIT_AUTOMATION
            return
        if self._check_failed(zone):
            await self._async_exit(zone, now, WAIT_CHECK_FAILED)
            zone.state, zone.reason = STATE_WAITING, WAIT_CHECK_FAILED
            return
        day = self._day(now)
        if zone.writes.get(day, 0) >= MAX_WRITES_DAY:
            self._pause(zone, now, WAIT_WRITE_CAP)
            await self._async_exit(zone, now, WAIT_WRITE_CAP)
            zone.state, zone.reason = STATE_WAITING, WAIT_WRITE_CAP
            return
        if zone.home_c is None:
            zone.state, zone.reason = STATE_WAITING, WAIT_UNAVAILABLE
            return

        if zone.effect is not None:
            await self._async_effect(zone, attributes, limits, now)
            if self._check_failed(zone):
                return

        plan = self.shadow.first_plan or self.shadow.plan
        hour = self._plan_hour(plan, now)
        would = would_do(plan, now) if plan else None
        action = None if hour is None else hour.action
        plan_until = planned_run_end(plan, now)
        shed = self._shed_until(now)
        for_ceiling = False
        if shed is not None and action in (ACTION_NORMAL, ACTION_LOWER):
            # Lowered for the ceiling: an hour the plan leaves normal, or a lowering the plan ends sooner.
            for_ceiling = action == ACTION_NORMAL or plan_until is None or plan_until < shed
            action, plan_until = ACTION_LOWER, shed if plan_until is None else max(plan_until, shed)
        decision = zone.decision = decide(
            ZoneSituation(
                now=now,
                action=action,
                plan_until=plan_until,
                action_until=None if would is None else would.until,
                home_c=zone.home_c,
                current_c=finite_number(attributes.get("current_temperature")),
                held=zone.action if zone.written_c is not None else None,
                held_since=zone.since if zone.written_c is not None else None,
                last_end=zone.last_end,
                lowered_24h=self._lowered_24h(zone, now),
                writes_today=zone.writes.get(day, 0),
                raise_ended=zone.raise_ended,
                safety=safety,
                night_delta_c=config.night.delta_c,
                away_delta_c=config.away_delta_c,
                limits=limits,
            )
        )
        if for_ceiling and decision.action == ACTION_LOWER and decision.reason == REASON_PLANNED:
            decision = zone.decision = ZoneDecision(ACTION_LOWER, decision.setpoint_c, REASON_CEILING, decision.until)
        if zone.effect is not None:
            zone.state, zone.reason = STATE_VERIFYING, "effect"
        else:
            zone.state, zone.reason = STATE_RUNNING, decision.reason
        if decision.reason == REASON_ZONE_WARM and zone.written_c is not None and zone.action in RAISES:
            zone.raise_ended = now
        if decision.setpoint_c is None:
            if zone.written_c is not None and (zone.home_retry_at is None or now >= zone.home_retry_at):
                await self._async_write(zone, zone.home_c, now, home=True, action=ACTION_NORMAL, limits=limits)
            return
        if zone.written_c is not None and same(zone.written_c, decision.setpoint_c, limits) and zone.action == decision.action:
            return
        if zone.written_c is None and same(read, decision.setpoint_c, limits):
            # It already reads the value asked for (the person's own is that value): nothing to write.
            return
        await self._async_write(zone, decision.setpoint_c, now, home=False, action=decision.action, limits=limits, read=read)

    @staticmethod
    def _plan_hour(plan: list[BandHour], now: datetime) -> BandHour | None:
        return next((hour for hour in plan if hour.start <= now < hour.start + timedelta(hours=1)), None)

    def _safety(self, config: HeatConfig, now: datetime) -> str | None:
        """The parts of the heat pump's safety that hold for a zone: a stale plan or outdoor reading, the cold limit,
        and good data younger than `GOOD_FOR`."""
        made = self.shadow.made_at
        outdoor = self.control.outdoor_now(config)
        if outdoor is None or made is None or now - made > FRESH_PLAN or not (self.shadow.first_plan or self.shadow.plan):
            self._good_since = None
            return SAFETY_STALE
        if self._good_since is None:
            self._good_since = now
        coldest = self.control.coldest_outdoor(config, now)
        if coldest is not None and coldest <= NO_LOWER_AT_C:
            return SAFETY_TOO_COLD
        if now - self._good_since < GOOD_FOR:
            return SAFETY_SETTLING
        return None

    # ---- writes

    async def _async_set(self, entity_id: str, value: float) -> None:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self.hass.services.async_call(
                "climate", "set_temperature", {"entity_id": entity_id, "temperature": value}, blocking=True
            )

    async def _async_write(
        self,
        zone: ZoneState,
        value: float,
        now: datetime,
        *,
        home: bool,
        action: str,
        limits: Limits,
        read: float | None = None,
    ) -> None:
        """Write a setpoint, kept first (an exit must find it), read back at the next looks."""
        day = self._day(now)
        zone.writes[day] = zone.writes.get(day, 0) + 1
        if not home:
            zone.prior_c = zone.written_c
            zone.written_c = value
            if not await self.control.async_keep():
                # Not kept: nothing is written that an exit could not undo.
                zone.written_c = zone.prior_c
                zone.writes[day] -= 1
                return
        # What the thermostat shows before this write: still showing it afterwards is no read-back (`taken`).
        zone.pending = Pending(value, now, home, action, self._setpoint(zone.entity_id))
        self._log(now, zone, "write", value=value, action=action, home=home)
        if not home and zone.check is None and action == ACTION_LOWER and zone.effect is None:
            zone.effect = self._effect_start(zone, read, value, now)
        try:
            await self._async_set(zone.entity_id, value)
        except Exception as err:  # noqa: BLE001 - not read back: the look after `READ_BACK` gives it up
            _LOGGER.warning("SpotNav zones: setting %s to %s failed: %s", zone.entity_id, value, error_text(err))

    def _confirm(self, zone: ZoneState, pending: Pending, now: datetime) -> None:
        zone.pending = None
        if pending.home:
            self._handed_back(zone, now)
            return
        was_lower = zone.action == ACTION_LOWER
        if pending.action == ACTION_LOWER and not was_lower:
            zone.lowerings.append([now, None])
        elif was_lower and pending.action != ACTION_LOWER:
            self._end(zone, now)
        if zone.action != pending.action:
            zone.since = now
        zone.action = pending.action
        zone.prior_c = None
        if zone.check is None and zone.effect is None:
            zone.check = {"result": CHECK_PASSED, "at": now.isoformat(), "why": "read_back"}
            self._log(now, zone, "check_passed", why="read_back")
        elif zone.effect is not None and zone.effect.get("since") is None:
            zone.effect["since"] = now.isoformat()

    def _handed_back(self, zone: ZoneState, now: datetime) -> None:
        zone.home_failures = 0
        zone.home_retry_at = None
        self._end(zone, now)
        zone.written_c = zone.prior_c = zone.action = None
        zone.since = None

    def _end(self, zone: ZoneState, at: datetime) -> None:
        for item in zone.lowerings:
            if item[1] is None:
                item[1] = at
                zone.last_end = at

    async def _async_exit(self, zone: ZoneState, now: datetime, why: str) -> None:
        """Write the person's value back where the thermostat still reads SpotNav's; one that does not read is written
        back at its first readable look; one someone else set is left (and its value is the person's)."""
        zone.effect = None
        if zone.written_c is None or zone.pending is not None:
            return
        state = self.hass.states.get(zone.entity_id)
        attributes = {} if state is None else state.attributes
        limits = Limits.of(attributes)
        read = finite_number(attributes.get("temperature"))
        if state is None or state.state in ("unavailable", "unknown") or read is None:
            zone.restore = True
            return
        if zone.home_c is None or not zone.ours(read, limits):
            self._log(now, zone, "given_up", why=why, read=read)
            self._end(zone, now)
            zone.written_c = zone.prior_c = zone.action = None
            zone.home_c = read if zone.home_c is None or not zone.ours(read, limits) else zone.home_c
            zone.home_failures, zone.home_retry_at = 0, None
            return
        if state.state != "heat":
            # Not in heat mode: nothing is written there; written back at the first look in heat mode.
            zone.restore = True
            return
        if zone.home_retry_at is not None and now < zone.home_retry_at:
            return
        self._log(now, zone, "exit", why=why)
        await self._async_write(zone, zone.home_c, now, home=True, action=ACTION_NORMAL, limits=limits)

    def _pause(self, zone: ZoneState, now: datetime, reason: str) -> None:
        if zone.paused_until is not None:
            return
        tz = time_zones.require_zone(self.shadow.tz)
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        zone.paused_until = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz).astimezone(now.tzinfo)
        zone.paused_reason = reason
        self._log(now, zone, "paused", why=reason)

    # ---- the check's effect

    def _zone_power_w(self, entity_id: str) -> float | None:
        """The zone's power now (W) from the power sensor on its device (`heat_zones.pick_power_sensor`, an energy
        sensor beside it notwithstanding; `read_power_w`); `None` without one or a fresh reading."""
        from ..site.heat_zones import device_sensors, pick_power_sensor, read_power_w  # noqa: PLC0415

        try:
            _device, sensors = device_sensors(self.hass, entity_id)
        except Exception:  # noqa: BLE001 - no power to judge by
            return None
        picked = pick_power_sensor(sensors)
        return None if picked is None else read_power_w(self.hass, picked)

    def _effect_start(self, zone: ZoneState, read: float | None, value: float, now: datetime) -> dict[str, Any] | None:
        """What the first lowering's effect is judged by: the zone's power or its `hvac_action`, where it heats now and
        reads at or above the lowered setpoint (else the thermostat may rightly go on heating); `None`: passed on the
        read-back alone."""
        state = self.hass.states.get(zone.entity_id)
        attributes = {} if state is None else state.attributes
        current = finite_number(attributes.get("current_temperature"))
        if current is None or current < value:
            return None
        power = self._zone_power_w(zone.entity_id)
        action = attributes.get("hvac_action")
        if power is not None and power > DRAWING_W:
            return {"by": "power", "baseline": power, "since": None}
        if action == "heating":
            return {"by": "action", "baseline": None, "since": None}
        return None

    async def _async_effect(self, zone: ZoneState, attributes: dict[str, Any], limits: Limits, now: datetime) -> None:
        effect = zone.effect
        if effect is None or effect.get("since") is None:
            return
        since = datetime.fromisoformat(effect["since"])
        action = attributes.get("hvac_action")
        if effect["by"] == "power":
            # The meter on the device may be another channel's: its fall counts only where the thermostat does not
            # say it still heats, and the thermostat saying it stopped counts as well.
            power = self._zone_power_w(zone.entity_id)
            fell = power is not None and power <= EFFECT_SHARE * float(effect["baseline"])
            seen = (fell and action != "heating") or action in ("idle", "off")
        else:
            seen = action in ("idle", "off")
        # Judged on two looks in a row either way: one reading is no proof (a compressor's pause, a meter's blip).
        effect["seen"] = effect.get("seen", 0) + 1 if seen else 0
        effect["missed"] = 0 if seen else effect.get("missed", 0) + 1
        if effect["seen"] >= EFFECT_LOOKS:
            zone.effect = None
            zone.check = {"result": CHECK_PASSED, "at": now.isoformat(), "why": effect["by"]}
            self._log(now, zone, "check_passed", why=effect["by"])
        elif now - since > EFFECT_GRACE and effect["missed"] >= EFFECT_LOOKS:
            zone.effect = None
            zone.check = {"result": CHECK_FAILED, "at": now.isoformat(), "why": "no_effect"}
            self._log(now, zone, "check_failed", why="no_effect")
            _LOGGER.info("SpotNav zones: %s did not stop heating when lowered; control stopped", zone.entity_id)
            await self._async_exit(zone, now, WAIT_CHECK_FAILED)
            zone.state, zone.reason = STATE_WAITING, WAIT_CHECK_FAILED

    @staticmethod
    def _check_failed(zone: ZoneState) -> bool:
        return zone.check is not None and zone.check.get("result") == CHECK_FAILED

    # ---- the forecast

    async def _async_power(self, now: datetime) -> None:
        """Each controlled metered zone's mean power over its last day, read hourly."""
        if self._kw_at is not None and now - self._kw_at < POWER_REFRESH:
            return
        self._kw_at = now
        if not any(zone.was_enabled for zone in self.zones.values()):
            self.kw = {}
            return
        from ..site.heat_zones import async_zone_periods  # noqa: PLC0415

        try:
            sources = self.site.house_forecast.heat.current_zones()
            periods = await async_zone_periods(self.hass, sources, now - timedelta(days=1), now, "hour")
        except Exception:  # noqa: BLE001 - no change to the forecast from the zones
            _LOGGER.debug("SpotNav zones: their power could not be read", exc_info=True)
            return
        kw: dict[str, float] = {}
        for source in sources.sources:
            rows = periods.get(source.sensor_id) or {}
            if rows:
                kw[source.entity_id] = sum(rows.values()) / len(rows)
        self.kw = kw

    def lowerings_ahead(self, zone: ZoneState, now: datetime) -> list[tuple[datetime, datetime]]:
        """The zone's lowering running now (to its decided end) and those the plan has ahead by the rules."""
        if zone.state != STATE_RUNNING:
            return []
        decision = zone.decision
        held_until = decision.until if zone.held_lower() and decision is not None and decision.action == ACTION_LOWER else None
        plan = self.shadow.first_plan or self.shadow.plan
        return closings_ahead(
            plan,
            now,
            held_until=held_until,
            last_end=zone.last_end,
            lowerings=[(item[0], item[1]) for item in zone.lowerings if item[1] is not None],  # type: ignore[misc]
            writes=zone.writes,
            day_of=self._day,
            max_writes=MAX_WRITES_DAY,
        )

    def forecast_delta(self, now: datetime) -> dict[datetime, float]:
        out: dict[datetime, float] = {}
        for entity_id, zone in self.zones.items():
            kw = self.kw.get(entity_id)
            if kw is None:
                continue
            done = [(item[0], item[1]) for item in zone.lowerings if item[1] is not None]
            for hour, value in forecast_delta([*done, *self.lowerings_ahead(zone, now)], kw, now).items():  # type: ignore[list-item]
                out[hour] = out.get(hour, 0.0) + value
        return out

    # ---- helpers

    def _day(self, now: datetime) -> str:
        return now.astimezone(time_zones.require_zone(self.shadow.tz)).date().isoformat()

    def _lowered_24h(self, zone: ZoneState, now: datetime) -> timedelta:
        return lowered_within([(item[0], item[1]) for item in zone.lowerings], now - timedelta(days=1), now)  # type: ignore[misc]

    def _trim(self, now: datetime) -> None:
        oldest = now - timedelta(days=DAYS_KEPT)
        first = self._day(oldest)
        for zone in self.zones.values():
            zone.lowerings = [item for item in zone.lowerings if item[1] is None or item[1] >= oldest]  # type: ignore[operator]
            zone.writes = {day: count for day, count in zone.writes.items() if day >= first}

    def _log(self, now: datetime, zone: ZoneState, what: str, **extra: Any) -> None:
        self.log.append({"at": now.isoformat(), "zone": zone.entity_id, "what": what, **extra})

    # ---- what is shown

    def status(self, entity_id: str, config: HeatConfig | None = None) -> dict[str, Any] | None:
        """The card's `zones[].control` block: `{enabled, state, reason, home_c, held_c, until, check}`; `None` for a
        zone without the opt-in that SpotNav holds nothing on."""
        config = config or self.shadow.config()
        enabled = any(zone.entity_id == entity_id and zone.control for zone in config.zones)
        zone = self.zones.get(entity_id)
        if not enabled and (zone is None or not zone.holds()):
            return None
        if zone is None:
            return {"enabled": enabled, "state": STATE_VERIFYING, "reason": None, "home_c": None, "held_c": None, "until": None, "check": None}
        decision = zone.decision
        held = zone.written_c
        return {
            "enabled": enabled,
            "state": zone.state if enabled else STATE_OFF,
            "reason": zone.reason,
            "home_c": zone.home_c,
            "held_c": held,
            "until": None if held is None or decision is None or decision.until is None or zone.state != STATE_RUNNING else decision.until.isoformat(),
            "check": None if zone.check is None else zone.check.get("result"),
            "action": zone.action if held is not None else None,
            "automation": None
            if zone.automation is None or zone.reason != WAIT_AUTOMATION
            else {"entity_id": zone.automation.entity_id, "name": zone.automation.name},
            "paused_until": _iso(zone.paused_until),
        }

    def holds_any(self) -> bool:
        return any(zone.holds() for zone in self.zones.values())

    def diagnostics(self) -> dict[str, Any]:
        return {
            "zones": {
                entity_id: {
                    "kept": zone.stored(),
                    "qualifies": zone.why is None,
                    "why": zone.why,
                    "state": zone.state,
                    "reason": zone.reason,
                    "pending": None if zone.pending is None else {"value": zone.pending.value, "at": zone.pending.at.isoformat(), "home": zone.pending.home},
                    "decision": None
                    if zone.decision is None
                    else {"action": zone.decision.action, "setpoint_c": zone.decision.setpoint_c, "reason": zone.decision.reason, "until": _iso(zone.decision.until)},
                    "kw": self.kw.get(entity_id),
                }
                for entity_id, zone in self.zones.items()
            },
            "log": list(self.log),
        }


__all__ = ["ZonesControl", "WHY_NOT_HEAT", "WHY_UNAVAILABLE"]
