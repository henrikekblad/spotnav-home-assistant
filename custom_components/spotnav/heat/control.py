"""SpotNav's heat control: the heat pump's relay switched by SpotNav itself, behind the person's opt-in (plan
`plans/v3_heating_plan.md` step 5, the owner's decisions of 2026-11-16). One per site, owned by its
`SiteCapacityController`, beside the heat shadow (`heat/runtime.py`) whose plan it carries out.

**The opt-in**: the heat settings' `control_enabled` ("Styr värmen", off by default), on the relay path or SG Ready's
first contact only. Nothing is written without it. The rules and the check are pure (`heat/relay_control.py`).

**Each look** (`LOOK_S`, and at once after a heat settings write), never raises:

1. A relay entity that changed is given back first (the old one opened if SpotNav holds it), and checked again.
2. Off, no relay path, a pause or a block: SpotNav's closing is given back (opened) and nothing else is written.
   Blocks (nothing written until they go, each a line under the switch): an enabled automation of the household that
   references the relay (`automation`, one actionable note), another heat optimiser set up (`other_controller`),
   the pump's own price control on (`pump_price_control`), the relay unreadable (`relay_unavailable`), the relay
   closed by someone else (`relay_closed`).
3. **A write is read back**: the relay must show it within `READ_BACK`; a write that raised or was not seen pauses
   control for the day (`no_read_back`) after an opening (tried again every `RETRY_OPEN` while SpotNav may hold it).
4. **Never fight another writer**: a change SpotNav did not make (the relay opened while SpotNav held it, or closed
   while it was open) gives the relay up at once (nothing written) and pauses control until the next local day
   (`foreign_change`).
5. **The check** before control runs, at the first start and after the relay entity changes (kept per entity): the
   meter's power now and its levels (read again every hour from its 5-minute rows) or the pump's calculated supply
   (`heat/facts.py`); without either nothing can be judged (`no_signal`). A failed check stops control
   (`check_failed`) until the switch is turned off and on again.
6. **Control**: `relay_control.decide` on the heat shadow's plan, with SpotNav's lowerings of the day, its writes and
   the safety reasons: no fresh plan or no outdoor reading (`stale`), outdoors at or below -15 °C, the coldest
   counted room (its offset counted) below the band, SG Ready's second contact on or unreadable.

**Every exit opens the relay** SpotNav holds (open = the pump's normal curve): the switch off, a block, a pause, a
safety reason, a changed relay entity, unload and Home Assistant's stop (`async_shutdown`, bounded); what could not be
opened then is opened at the next start before anything else. Kept in `spotnav.heat_control.<entry>`: whether SpotNav
holds the relay closed, the lowerings and writes of the last days, the checks per relay, the pause.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import DOMAIN
from .automations import SteeringAutomation, steering_automations
from .config import BAND_C, PATH_SG_READY, HeatConfig
from .meter_split import Levels, levels, state_now
from .relay_control import (
    FAILED,
    GOOD_FOR,
    MAX_RUN,
    NO_LOWER_AT_C,
    PASSED,
    SAFETY_ROOM_COLD,
    SAFETY_SETTLING,
    SAFETY_SG_READY_B,
    SAFETY_STALE,
    SAFETY_TOO_COLD,
    Check,
    Decision,
    Sample,
    Situation,
    decide,
    lowered_within,
    planned_run_end,
    verdict,
)
from .shadow_rule import ACTION_LOWER

if TYPE_CHECKING:
    from .runtime import HeatShadow

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
LOOK_S: Final = 30.0
#: A write not seen on the relay within this long is a failure.
READ_BACK: Final = timedelta(seconds=90)
#: After a failed opening, how often it is tried again while SpotNav may hold the relay closed.
RETRY_OPEN: Final = timedelta(minutes=5)
#: How long a service call may take.
CALL_TIMEOUT_S: Final = 10.0
#: How long the shutdown waits for the opening to be read back (s).
SHUTDOWN_WAIT_S: Final = 5.0
#: The plan must have been made within this long to be acted on.
FRESH_PLAN: Final = timedelta(hours=2)
#: Lowerings and writes kept (days).
DAYS_KEPT: Final = 3
LOG_SIZE: Final = 100

STATE_OFF: Final = "off"
STATE_VERIFYING: Final = "verifying"
STATE_RUNNING: Final = "running"
STATE_WAITING: Final = "waiting"

WAIT_NO_RELAY: Final = "no_relay"
WAIT_RELAY_UNAVAILABLE: Final = "relay_unavailable"
WAIT_RELAY_CLOSED: Final = "relay_closed"
WAIT_AUTOMATION: Final = "automation"
WAIT_OTHER_CONTROLLER: Final = "other_controller"
WAIT_PUMP_PRICE_CONTROL: Final = "pump_price_control"
WAIT_NO_SIGNAL: Final = "no_signal"
WAIT_CHECK_FAILED: Final = "check_failed"
WAIT_FOREIGN_CHANGE: Final = "foreign_change"
WAIT_NO_READ_BACK: Final = "no_read_back"

#: The relay's states that read as closed.
CLOSED: Final = frozenset({"on"})


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.heat_control.{entry_id}"


def _instant(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class Pending:
    closed: bool
    at: datetime


class HeatControl:
    """The site's heat control (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, shadow: HeatShadow) -> None:
        self.hass = hass
        self.shadow = shadow
        self.site = shadow.site
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(self.site.entry_id))
        # Kept.
        self.relay: str | None = None
        #: SpotNav closed the relay and has not seen it open since.
        self.held = False
        self.held_since: datetime | None = None
        self.last_end: datetime | None = None
        #: SpotNav's lowerings `[start, end | None]`, the last `DAYS_KEPT` days.
        self.lowerings: list[list[datetime | None]] = []
        #: Writes per local day (ISO date).
        self.writes: dict[str, int] = {}
        #: The check's result per relay entity: `{result, outcome, at}`.
        self.checks: dict[str, dict[str, Any]] = {}
        self.check = Check()
        self.paused_until: datetime | None = None
        self.paused_reason: str | None = None
        # Seen.
        self.pending: Pending | None = None
        self.state = STATE_OFF
        self.reason: str | None = None
        self.decision: Decision | None = None
        self.automation: SteeringAutomation | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self._was_enabled: bool | None = None
        #: Held at the last stop: opened at the first look before anything else.
        self._reopen = False
        self._expect: bool | None = None
        self._good_since: datetime | None = None
        self._retry_open_at: datetime | None = None
        self._levels: Levels | None = None
        #: Whether anything can show the check an effect: a power meter or a calculated supply the pump reports.
        self.signal = False
        self._levels_hour: datetime | None = None
        self._loaded = False
        self._closed = False
        self._looking = False
        self._again = False
        self._task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None

    # ---- life cycle

    async def async_start(self) -> None:
        try:
            self._restore(await self._store.async_load())
        except Exception:  # noqa: BLE001 - a store that cannot be read starts again (the relay is read as it is)
            _LOGGER.warning("SpotNav heat control: the kept state could not be read; starting a new one", exc_info=True)
        self._loaded = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=LOOK_S))
        self.request_look()

    def _restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.relay = raw.get("relay") if isinstance(raw.get("relay"), str) else None
        self.held = raw.get("held") is True and self.relay is not None
        self.held_since = _instant(raw.get("held_since"))
        self.last_end = _instant(raw.get("last_end"))
        for item in raw.get("lowerings") or []:
            if isinstance(item, list) and len(item) == 2 and (start := _instant(item[0])) is not None:
                self.lowerings.append([start, _instant(item[1])])
        writes = raw.get("writes")
        if isinstance(writes, dict):
            self.writes = {day: count for day, count in writes.items() if isinstance(count, int)}
        checks = raw.get("checks")
        if isinstance(checks, dict):
            self.checks = {key: value for key, value in checks.items() if isinstance(value, dict)}
        self.check = Check.from_dict(raw.get("check"))
        self.paused_until = _instant(raw.get("paused_until"))
        self.paused_reason = raw.get("paused_reason") if isinstance(raw.get("paused_reason"), str) else None
        if self.held:
            # Home Assistant stopped while SpotNav held the relay: it is opened at the first look, a new lowering
            # starts only after the rest that follows any lowering.
            self._reopen = True
            _LOGGER.info("SpotNav heat control: the relay %s was held at the last stop; opening it", self.relay)

    def _data(self) -> dict[str, Any]:
        return {
            "relay": self.relay,
            "held": self.held,
            "held_since": None if self.held_since is None else self.held_since.isoformat(),
            "last_end": None if self.last_end is None else self.last_end.isoformat(),
            "lowerings": [[start.isoformat(), None if end is None else end.isoformat()] for start, end in self.lowerings],  # type: ignore[union-attr]
            "writes": self.writes,
            "checks": self.checks,
            "check": self.check.as_dict(),
            "paused_until": None if self.paused_until is None else self.paused_until.isoformat(),
            "paused_reason": self.paused_reason,
        }

    def _save(self) -> None:
        if self._loaded:
            self._store.async_delay_save(self._data, 1.0)

    async def async_shutdown(self) -> None:
        """Open the relay SpotNav holds (bounded) and keep the state: a relay still held is opened at the next start."""
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if not self._loaded:
            return
        if self.held and self.relay is not None:
            try:
                await self._async_call(self.relay, False)
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    while self._read(self.relay) is True:
                        await asyncio.sleep(0.2)
                if self._read(self.relay) is False:
                    self._end_lowering(dt_util.utcnow())
            except Exception:  # noqa: BLE001 - kept held: opened at the next start
                _LOGGER.warning("SpotNav heat control: the relay could not be opened at the stop", exc_info=True)
        await self._store.async_save(self._data())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        await Store(hass, STORE_VERSION, store_key(entry_id)).async_remove()

    # ---- looks

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.request_look()

    @callback
    def request_look(self) -> None:
        """Look now (in the background); a look asked for while one runs follows it."""
        if self._closed or not self._loaded:
            return
        if self._looking:
            self._again = True
            return
        self._looking = True
        self._task = self.hass.async_create_background_task(self._async_looks(), f"{DOMAIN} heat control {self.site.entry_id}")

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
        """One look (see the module). Never raises."""
        try:
            await self._async_look(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again; the relay is opened where it can be
            _LOGGER.warning("SpotNav heat control: a look failed", exc_info=True)
            try:
                await self._async_give_back(now, "error")
            except Exception:  # noqa: BLE001 - tried again at the next look
                _LOGGER.warning("SpotNav heat control: the relay could not be given back", exc_info=True)
        self._trim(now)
        self._save()

    async def _async_look(self, now: datetime) -> None:
        config = self.shadow.config()
        from .runtime import relay_entity  # noqa: PLC0415 - the runtime imports this module

        relay = relay_entity(config)
        if self.relay is not None and relay != self.relay:
            # The old relay is given back first: opened where SpotNav holds it, read back at the next looks.
            if self.pending is not None and self._read(self.relay) == self.pending.closed:
                self._confirm(self.pending, now)
            if self.held:
                if self.pending is None or now - self.pending.at > READ_BACK:
                    self.pending = None
                    await self._async_give_back(now, "relay_changed")
                if self.held:
                    return
            self.pending, self._expect = None, None
        if relay != self.relay:
            self.relay = relay
            self.check = Check()
        enabled = config.control_enabled
        if enabled and self._was_enabled is False:
            # Turned on again: a failed check and a pause are tried afresh.
            if relay is not None and verdict(self.checks.get(relay, {}).get("result")) == FAILED:
                self.checks.pop(relay, None)
                self.check = Check()
            self.paused_until = self.paused_reason = None
        self._was_enabled = enabled
        if self.paused_until is not None and now >= self.paused_until:
            self.paused_until = self.paused_reason = None
        # A write is read back (or given up) first, also when control was just turned off: a closing still pending
        # is opened all the same.
        read = None if relay is None else self._read(relay)
        if self.pending is not None:
            if read == self.pending.closed:
                self._confirm(self.pending, now)
            elif now - self.pending.at > READ_BACK:
                await self._async_failed(now, "not_read_back")
                return
            else:
                if not enabled:
                    self._set(STATE_OFF, None)
                return
        if not enabled or relay is None:
            await self._async_give_back(now, "off")
            self._set(STATE_OFF if not enabled else STATE_WAITING, None if not enabled else WAIT_NO_RELAY)
            self._expect = None
            return

        if read is None:
            self._set(STATE_WAITING, WAIT_RELAY_UNAVAILABLE)
            if self.held:
                await self._async_give_back(now, "relay_unavailable")
            return
        if self._reopen:
            # Held at the last stop: opened before anything else (a relay found open was reset meanwhile).
            if self.held and read is True:
                await self._async_give_back(now, "restart")
                return
            self._end_lowering(now)
            self._reopen = False
        # Never fight another writer.
        if self.held and read is False:
            self._log(now, "foreign_open")
            self._end_lowering(now)
            self._pause(now, WAIT_FOREIGN_CHANGE)
        elif not self.held and read is True and self._expect is False:
            self._log(now, "foreign_close")
            self._pause(now, WAIT_FOREIGN_CHANGE)
        if self.paused_until is not None:
            await self._async_give_back(now, self.paused_reason or "paused")
            self._expect = None
            self._set(STATE_WAITING, self.paused_reason)
            return
        if self.held and self.held_since is None:
            self.held_since = now
        blocker = self._blocker(config, relay)
        if blocker is None and not self.held and read is True:
            blocker = WAIT_RELAY_CLOSED
        if blocker is not None:
            await self._async_give_back(now, blocker)
            self._expect = None
            self.check.abort(blocker)
            self._set(STATE_WAITING, blocker)
            return
        self._expect = read
        safety = self._safety(config, now)

        result = self.checks.get(relay)
        if result is None or result.get("result") != PASSED:
            if result is not None and result.get("result") == FAILED:
                await self._async_give_back(now, WAIT_CHECK_FAILED)
                self._set(STATE_WAITING, WAIT_CHECK_FAILED)
                return
            await self._async_verify(now, relay, read, safety)
            return

        self.decision = decide(
            Situation(
                now=now,
                plan_until=planned_run_end(self.shadow.plan, now),
                held_since=self.held_since if self.held else None,
                last_end=self.last_end,
                lowered_today=self.lowered_today(now),
                writes_today=self.writes.get(self._day(now), 0),
                safety=safety,
            )
        )
        self._set(STATE_RUNNING, self.decision.reason)
        if self.decision.closed != self.held:
            await self._async_write(self.decision.closed, now, why=self.decision.reason)

    async def _async_verify(self, now: datetime, relay: str, read: bool, safety: str | None) -> None:
        sample = await self.async_sample(now)
        if not self.signal:
            self.check.abort(WAIT_NO_SIGNAL)
            await self._async_give_back(now, WAIT_NO_SIGNAL)
            self._set(STATE_WAITING, WAIT_NO_SIGNAL)
            return
        blocked = None if safety in (None, SAFETY_SETTLING) else safety
        step = self.check.step(now, sample, read, blocked)
        if step.outcome is not None:
            self._log(now, "check", outcome=step.outcome)
            _LOGGER.info("SpotNav heat control: the check of %s ended: %s", relay, step.outcome)
            if verdict(step.outcome) in (PASSED, FAILED):
                self.checks[relay] = {"result": verdict(step.outcome), "outcome": step.outcome, "at": now.isoformat()}
        self._set(STATE_VERIFYING, self.check.phase)
        if step.closed != self.held:
            await self._async_write(step.closed, now, why="check")

    # ---- reads

    def _read(self, entity_id: str) -> bool | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        return state.state in CLOSED

    def _blocker(self, config: HeatConfig, relay: str) -> str | None:
        from .detect import other_controllers, read_heat_pumps  # noqa: PLC0415 - read only when it matters

        watched = [relay] + ([config.control_entity_b] if config.control_entity_b else [])
        self.automation = next(iter(steering_automations(self.hass, watched)), None)
        if self.automation is not None:
            return WAIT_AUTOMATION
        if other_controllers(self.hass):
            return WAIT_OTHER_CONTROLLER
        pumps = read_heat_pumps(self.hass)
        if pumps and pumps[0].price_control.state == "active":
            return WAIT_PUMP_PRICE_CONTROL
        return None

    def outdoor_now(self, config: HeatConfig) -> float | None:
        from ..site.house_forecast import to_celsius  # noqa: PLC0415 - the site imports this module

        if config.outdoor_sensor is None:
            return self.site.house_forecast.outdoor_c()
        state = self.hass.states.get(config.outdoor_sensor)
        if state is None:
            return None
        if config.outdoor_sensor.startswith("weather."):
            return to_celsius(state.attributes.get("temperature"), state.attributes.get("temperature_unit"))
        return to_celsius(state.state, state.attributes.get("unit_of_measurement"))

    def _safety(self, config: HeatConfig, now: datetime) -> str | None:
        """The safety reason now (see `relay_control`), `settling` while good data is younger than `GOOD_FOR`."""
        outdoor = self.outdoor_now(config)
        made = self.shadow.made_at
        if outdoor is None or made is None or now - made > FRESH_PLAN or not self.shadow.plan:
            self._good_since = None
            return SAFETY_STALE
        if self._good_since is None:
            self._good_since = now
        if outdoor <= NO_LOWER_AT_C:
            return SAFETY_TOO_COLD
        coldest = self.shadow.coldest_now(config)
        if coldest is not None and coldest < config.target_c - BAND_C:
            return SAFETY_ROOM_COLD
        if config.control_path == PATH_SG_READY and config.control_entity_b is not None:
            if self._read(config.control_entity_b) is not False:
                return SAFETY_SG_READY_B
        if now - self._good_since < GOOD_FOR:
            return SAFETY_SETTLING
        return None

    async def async_sample(self, now: datetime) -> Sample:
        """The check's readings now: the meter's power and what the pump does by it, the calculated supply."""
        from ..site.heat_meter import KIND_POWER, async_heat_hours  # noqa: PLC0415 - the site imports this module
        from .detect import read_heat_pumps  # noqa: PLC0415

        meter = self.site.house_forecast.heat.meter
        kw = None
        power_meter = meter.found and meter.kind == KIND_POWER and meter.entity_id is not None
        if power_meter and meter.entity_id is not None:
            state = self.hass.states.get(meter.entity_id)
            try:
                value = None if state is None else float(state.state)
            except ValueError:
                value = None
            if value is not None and state is not None:
                unit = state.attributes.get("unit_of_measurement")
                kw = value / 1000.0 if unit == "W" else value * 1000.0 if unit == "MW" else value
        hour = now.replace(minute=0, second=0, microsecond=0)
        if kw is not None and self._levels_hour != hour:
            self._levels_hour = hour
            try:
                rows = await async_heat_hours(self.hass, meter, now - timedelta(hours=24), now, "5minute")
                self._levels = levels({start: kwh * 12.0 for start, kwh in rows.items()})
            except Exception:  # noqa: BLE001 - no levels: the power is not judged this hour
                _LOGGER.debug("SpotNav heat control: the meter's rows could not be read", exc_info=True)
                self._levels = None
        pumps = read_heat_pumps(self.hass)
        supply = pumps[0].calc_supply_temp_c if pumps else None
        # A meter without its levels yet, or a supply that reads unavailable for a while, is no reason to say none.
        self.signal = power_meter or (bool(pumps) and "calc_supply_temp" in pumps[0].sources) or supply is not None
        state_now_ = state_now(kw, self._levels, kw) if kw is not None else None
        return Sample(kw if state_now_ is not None else None, state_now_, supply)

    # ---- writes

    async def _async_call(self, entity_id: str, closed: bool) -> None:
        domain = entity_id.split(".", 1)[0]
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self.hass.services.async_call(
                domain, "turn_on" if closed else "turn_off", {"entity_id": entity_id}, blocking=True
            )

    async def _async_write(self, closed: bool, now: datetime, *, why: str) -> None:
        """Switch the relay and wait for the read-back at the next looks. An opening is never refused."""
        relay = self.relay
        if relay is None:
            return
        if not closed and self._retry_open_at is not None and now < self._retry_open_at:
            return
        self.pending = Pending(closed, now)
        day = self._day(now)
        self.writes[day] = self.writes.get(day, 0) + 1
        if closed:
            self.held, self.held_since = True, now
            self.lowerings.append([now, None])
        self._log(now, "close" if closed else "open", why=why)
        self._save()
        try:
            await self._async_call(relay, closed)
        except Exception as err:  # noqa: BLE001 - any error is a write not taken
            _LOGGER.warning("SpotNav heat control: switching %s %s failed: %s", relay, "on" if closed else "off", err)
            await self._async_failed(now, "write_failed")
            return
        if self._read(relay) == closed:
            self._confirm(self.pending, now)

    def _confirm(self, pending: Pending, now: datetime) -> None:
        self.pending = None
        self._expect = pending.closed
        if not pending.closed:
            self._end_lowering(pending.at)
            self._retry_open_at = None

    def _end_lowering(self, at: datetime) -> None:
        if self.held:
            self.last_end = at
        self.held, self.held_since = False, None
        for item in self.lowerings:
            if item[1] is None:
                item[1] = at

    async def _async_failed(self, now: datetime, why: str) -> None:
        """A write that raised or was not read back: control pauses for the day; an opening is tried again while
        SpotNav may hold the relay."""
        pending = self.pending
        self.pending = None
        self._log(now, "failed", why=why)
        self._pause(now, WAIT_NO_READ_BACK)
        self._set(STATE_WAITING, self.paused_reason)
        if pending is not None and not pending.closed:
            self._retry_open_at = now + RETRY_OPEN
        elif self.held and self.relay is not None:
            # A closing not seen may still take: opened all the same (the fail-safe); one that reads open is owed
            # nothing more.
            await self._async_write(False, now, why=why)
            if self.held and self._read(self.relay) is False:
                self.pending, self._retry_open_at = None, None
                self._end_lowering(now)

    async def _async_give_back(self, now: datetime, why: str) -> None:
        """Open the relay SpotNav holds (no write otherwise), and end a running check. A write not read back within
        `READ_BACK` is given up: the opening is written all the same."""
        self.check.abort(why)
        if self.pending is not None and now - self.pending.at > READ_BACK:
            self.pending = None
        if self.held and self.relay is not None and self.pending is None:
            await self._async_write(False, now, why=why)

    def _pause(self, now: datetime, reason: str) -> None:
        if self.paused_until is not None and self.paused_reason is not None:
            return
        zone = time_zones.require_zone(self.shadow.tz)
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        self.paused_until = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone).astimezone(now.tzinfo)
        self.paused_reason = reason
        self.check.abort(reason)
        _LOGGER.info("SpotNav heat control: paused until %s (%s)", self.paused_until.isoformat(), reason)

    # ---- figures

    def _day(self, now: datetime) -> str:
        return now.astimezone(time_zones.require_zone(self.shadow.tz)).date().isoformat()

    def _midnight(self, day: date) -> datetime:
        zone = time_zones.require_zone(self.shadow.tz)
        return datetime(day.year, day.month, day.day, tzinfo=zone)

    def lowered_today(self, now: datetime) -> timedelta:
        start = self._midnight(now.astimezone(time_zones.require_zone(self.shadow.tz)).date())
        return lowered_within([(item[0], item[1]) for item in self.lowerings], start, now)  # type: ignore[misc]

    def executed_minutes(self, hours: list[datetime], now: datetime) -> dict[datetime, int]:
        """Per hour: the minutes SpotNav held the relay closed (up to now)."""
        intervals = [(item[0], item[1]) for item in self.lowerings]
        out: dict[datetime, int] = {}
        for hour in hours:
            minutes = lowered_within(intervals, hour, min(now, hour + timedelta(hours=1))) if hour < now else timedelta(0)  # type: ignore[arg-type]
            out[hour] = round(minutes.total_seconds() / 60)
        return out

    def closings_ahead(self, now: datetime) -> list[tuple[datetime, datetime]] | None:
        """The relay's closings expected from now on while control runs (the plan's lowered runs, each at most
        `MAX_RUN`; the running one to its decided end); `None` while it does not run."""
        if self.state != STATE_RUNNING:
            return None
        out: list[tuple[datetime, datetime]] = []
        if self.held and self.decision is not None and self.decision.closed and self.decision.until is not None:
            out.append((now, self.decision.until))
        run: list[datetime] = []
        for hour in sorted(self.shadow.plan, key=lambda item: item.start):
            if hour.action == ACTION_LOWER and hour.start >= now and (not out or hour.start >= out[-1][1]):
                if run and hour.start != run[-1] + timedelta(hours=1):
                    out.append((run[0], min(run[-1] + timedelta(hours=1), run[0] + MAX_RUN)))
                    run = []
                run.append(hour.start)
        if run:
            out.append((run[0], min(run[-1] + timedelta(hours=1), run[0] + MAX_RUN)))
        return out

    def _set(self, state: str, reason: str | None) -> None:
        if (state, reason) != (self.state, self.reason):
            _LOGGER.debug("SpotNav heat control: %s (%s)", state, reason)
        self.state, self.reason = state, reason

    def _log(self, now: datetime, what: str, **extra: Any) -> None:
        self.log.append({"at": now.isoformat(), "what": what, **extra})

    def _trim(self, now: datetime) -> None:
        oldest = now - timedelta(days=DAYS_KEPT)
        self.lowerings = [item for item in self.lowerings if item[1] is None or item[1] >= oldest]  # type: ignore[operator]
        first = self._day(oldest)
        self.writes = {day: count for day, count in self.writes.items() if day >= first}

    # ---- what is shown

    def status(self) -> dict[str, Any]:
        """The card's `control` block (see `heat/view.py`)."""
        result = None if self.relay is None else self.checks.get(self.relay)
        return {
            "enabled": self.shadow.config().control_enabled,
            "state": self.state,
            "reason": self.reason,
            "automation": None if self.automation is None or self.reason != WAIT_AUTOMATION else {"entity_id": self.automation.entity_id, "name": self.automation.name},
            "check": None if result is None else result.get("result"),
            "lowered": self.held,
            "since": None if not self.held or self.held_since is None else self.held_since.isoformat(),
            "until": None
            if not self.held or self.decision is None or self.decision.until is None or self.state != STATE_RUNNING
            else self.decision.until.isoformat(),
            "paused_until": None if self.paused_until is None else self.paused_until.isoformat(),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status(),
            "kept": self._data(),
            "pending": None if self.pending is None else {"closed": self.pending.closed, "at": self.pending.at.isoformat()},
            "levels": None if self._levels is None else {"running_kw": self._levels.running_kw, "baseline_kw": self._levels.baseline_kw},
            "log": list(self.log),
        }
