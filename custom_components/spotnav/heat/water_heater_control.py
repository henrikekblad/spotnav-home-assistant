"""SpotNav switching a plain water heater (plan `plans/v3_zones_water_heater.md` section 3, the owner's decisions of
2026-11-20): an immersion element on its own thermostat behind a `switch`. Owned by the site controller beside the
heat pump's control and the zones'; kept in the heat control's store (key `water_heater`). The rules are pure
(`heat/water_heater.py`, legionella first); this is the shell that reads and writes.

**Watched, then controlled.** The switch chosen alone is watched: full heat is looked for and the windows SpotNav would
allow are shown. "Control the water heater" (`water_heater.control`) lets SpotNav switch it. Nothing is written
without it.

**Each look** (`LOOK_S`, at once after a heat settings write), never raises:

1. **The fail-safe is on.** The switch unreadable: nothing written, and what SpotNav holds off is turned on at its
   first readable look. Held off at the last stop (a stop that could not turn it on): on before anything else. Every
   exit turns it on: the opt-in off, a pause, a block, the check failed, the give-up, unload and Home Assistant's stop.
2. **Every write is read back** within `READ_BACK`; an off not seen is followed by an on, an on not seen is written
   again every `RETRY_ON` while SpotNav may hold the switch off. A failed write pauses control to the next local day
   (`no_read_back`); during the check it fails the check.
3. **The person's on is never cut.** Turned on by anyone but SpotNav while it holds it off: left on, control paused to
   the next local day. Turned off by someone else while SpotNav allows it: left off (their choice), paused to the next
   day, then the plan (and the hard rules) resume. A change counts when it has read for `FOREIGN_CONFIRM`.
4. **Blocks**: an enabled automation whose actions reference the switch (`heat/automations.py`, one note).
5. **The check**, once per switch (again after the opt-in is turned off and on): the first off and the following on
   read back; where the element drew when the off was written, the power must fall under `CUT_W` within `CUT_WITHIN`
   (else the switch does not cut the element: `check_failed`, on).
6. **Control**: `water_heater.Rules.want` on the windows (`water_heater.plan_windows`, made again every look from the
   prices with their fees and the heat plan's tight hours; a window that has begun is kept as it is). Without prices
   for the running hour the switch is on (`no_plan`).
7. **The ceiling** (`site/shed_order.py`): where the site's shed order pauses the water heater for the ceiling, a
   cheap window is paused until the period's end (`ceiling`), after every hard rule, and only where the window left
   after the pause still covers the heater's need (its energy at the element's power, less the time it was seen
   drawing in it: `_until_max`). What it could give is offered at each look (`shed_offer`): what its trusted meter
   reads it drawing (never a learnt figure; read again at every ask), whether the rules would take the pause now
   (`Rules.would_shed`, with the day's switchings and the longest off stretch) or hold it, and the latest it may last.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..core.parse import aware_instant, finite_number, number_from_state
from ..const import DOMAIN
from ..site.shed_order import LOAD_WATER_HEATER, Offer
from .automations import SteeringAutomation, steering_automations
from .config import HeatConfig
from .water_heater import (
    FULL_DRAW_W,
    HISTORY_DAYS,
    MODE_UNMETERED,
    WINDOW_CHEAP,
    WANT_OFF,
    WANT_SHED,
    Kept,
    Rules,
    Want,
    daily_kwh,
    element_kw,
    forecast_delta,
    mode_of,
    plan_windows,
    sizes,
)

if TYPE_CHECKING:
    from .control import HeatControl
    from .runtime import HeatShadow

_LOGGER = logging.getLogger(__name__)

LOOK_S: Final = 30.0
READ_BACK: Final = timedelta(seconds=90)
RETRY_ON: Final = timedelta(minutes=5)
FOREIGN_CONFIRM: Final = timedelta(seconds=60)
#: The check: an off that cuts the element brings its power under this within this long.
CUT_W: Final = 100.0
CUT_WITHIN: Final = timedelta(minutes=2)
CALL_TIMEOUT_S: Final = 10.0
SHUTDOWN_WAIT_S: Final = 5.0
LOG_SIZE: Final = 100
#: The history (daily kWh) is read again this often.
HISTORY_REFRESH: Final = timedelta(hours=6)
#: Readings kept for the element's power.
DRAW_SAMPLES: Final = 200

STATE_OFF: Final = "off"
STATE_VERIFYING: Final = "verifying"
STATE_RUNNING: Final = "running"
STATE_WAITING: Final = "waiting"

WAIT_UNAVAILABLE: Final = "unavailable"
WAIT_AUTOMATION: Final = "automation"
WAIT_FOREIGN_ON: Final = "foreign_on"
WAIT_FOREIGN_OFF: Final = "foreign_off"
WAIT_NO_READ_BACK: Final = "no_read_back"
WAIT_CHECK_FAILED: Final = "check_failed"
WAIT_NOT_FULL: Final = "not_full"

CHECK_PASSED: Final = "passed"
CHECK_FAILED: Final = "failed"

#: The note shown while control has stopped for want of full heat.
NOTE_NOT_FULL: Final = "water_heater_not_full"


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


class WaterHeaterControl:
    """The site's water heater (see the module). Never raises into the site."""

    def __init__(self, hass: HomeAssistant, shadow: HeatShadow, control: HeatControl) -> None:
        self.hass = hass
        self.shadow = shadow
        self.control = control
        self.site = shadow.site
        self.rules = Rules()
        # Kept.
        #: The switch SpotNav may hold, and whether it holds it off.
        self.entity: str | None = None
        self.held_off = False
        #: The check's verdict per switch: `{entity, result, at, why}`.
        self.check: dict[str, Any] | None = None
        self.paused_until: datetime | None = None
        self.paused_reason: str | None = None
        self.kw: float | None = None
        # Seen.
        #: The check under way: `{phase: off | on, drew, at}`.
        self.trial: dict[str, Any] | None = None
        self.pending: tuple[bool, datetime, bool] | None = None
        self._retry_on_at: datetime | None = None
        self._foreign: tuple[str, datetime] | None = None
        #: The switch read on while control ran (an off then is someone else's).
        self._seen_on = False
        self._restore_on = False
        self._was_enabled: bool | None = None
        self.state = STATE_OFF
        self.reason: str | None = None
        self.want: Want | None = None
        self.metered = False
        self.power_sensor: str | None = None
        #: The meter that has been seen at `FULL_DRAW_W` or more once, `{entity, sensor}`: a meter on a contactor's coil
        #: never is, and then the water heater is no more metered than one without a meter.
        self.meter_seen: dict[str, str] | None = None
        #: A meter is there but has not been seen drawing the element's power yet.
        self.meter_waiting = False
        self.daily: list[float] = []
        self._history_at: datetime | None = None
        self._draws: deque[float] = deque(maxlen=DRAW_SAMPLES)
        self.automation: SteeringAutomation | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self._closed = False
        self._started = False
        self._looking = False
        self._again = False
        self._task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        #: What a pause for the ceiling would give and whether the rules allow it, as of the last look.
        self.offer: Offer | None = None
        #: The look's mode and plan freshness (the offer asks the rules as the look would), and the element's time seen
        #: drawing per window (`_until_max`, memory only: a restart counts it from nothing, which only sheds less).
        self._look_mode: str | None = None
        self._look_fresh = False
        self._drawn_s: dict[str, float] = {}
        self._drawn_at: datetime | None = None
        control.parts["water_heater"] = self
        shadow.extra_deltas.append(self.forecast_delta)

    # ---- kept state

    def stored(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "held_off": self.held_off,
            "check": None if self.check is None else dict(self.check),
            "paused_until": _iso(self.paused_until),
            "paused_reason": self.paused_reason,
            "kw": self.kw,
            "meter_seen": None if self.meter_seen is None else dict(self.meter_seen),
            "rules": self.rules.kept.as_dict(),
        }

    def restore(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        self.entity = raw.get("entity") if isinstance(raw.get("entity"), str) else None
        self.held_off = raw.get("held_off") is True and self.entity is not None
        self.check = raw.get("check") if isinstance(raw.get("check"), dict) else None
        self.paused_until = aware_instant(raw.get("paused_until"))
        self.paused_reason = raw.get("paused_reason") if isinstance(raw.get("paused_reason"), str) else None
        self.kw = finite_number(raw.get("kw"))
        seen = raw.get("meter_seen")
        self.meter_seen = (
            {"entity": seen["entity"], "sensor": seen["sensor"]}
            if isinstance(seen, dict) and isinstance(seen.get("entity"), str) and isinstance(seen.get("sensor"), str)
            else None
        )
        self.rules = Rules(Kept.from_dict(raw.get("rules")))
        if self.held_off:
            # Home Assistant stopped while SpotNav held the switch off: on before anything else.
            self._restore_on = True
            _LOGGER.info("SpotNav water heater: %s was held off at the last stop; turning it on", self.entity)

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
        """Turn on the switch SpotNav holds off (bounded); one that does not take is turned on at the next start."""
        self.halt()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    await asyncio.wait({self._task})
            except TimeoutError:
                _LOGGER.warning("SpotNav water heater: a look did not end at the stop")
        if not self._started or not self.held_off or self.entity is None:
            return
        try:
            await self._async_call(self.entity, True)
            async with asyncio.timeout(SHUTDOWN_WAIT_S):
                while self._read(self.entity) is not True:
                    await asyncio.sleep(0.2)
            self.held_off = False
            self.rules.switched(dt_util.utcnow(), True, day=self._day(dt_util.utcnow()), planned=False)
        except Exception:  # noqa: BLE001 - kept: turned on at the next start
            _LOGGER.warning("SpotNav water heater: %s could not be turned on at the stop", self.entity, exc_info=True)

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
        self._task = self.hass.async_create_background_task(self._async_looks(), f"{DOMAIN} water heater {self.site.entry_id}")

    async def _async_looks(self) -> None:
        try:
            while True:
                self._again = False
                await self.async_look(dt_util.utcnow())
                if not self._again or self._closed:
                    break
        finally:
            self._looking = False

    # ---- reads

    def _read(self, entity_id: str) -> bool | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state not in ("on", "off"):
            return None
        return state.state == "on"

    def _power(self, entity_id: str) -> tuple[str | None, float | None]:
        """The power sensor on the switch's device and its reading (W); `(None, None)` without one."""
        from ..site.heat_meter import KIND_POWER  # noqa: PLC0415
        from ..site.heat_zones import device_sensors, pick_zone_sensor  # noqa: PLC0415

        try:
            _device, sensors = device_sensors(self.hass, entity_id)
        except Exception:  # noqa: BLE001 - unmetered
            return None, None
        picked = pick_zone_sensor(sensors)
        if picked is None or picked[1] != KIND_POWER:
            return None, None
        state = self.hass.states.get(picked[0])
        value = None if state is None or state.state in ("unavailable", "unknown") else number_from_state(state.state)
        if value is None:
            return picked[0], None
        unit = state.attributes.get("unit_of_measurement")  # type: ignore[union-attr]
        return picked[0], value * 1000.0 if unit == "kW" else value

    def _tank(self, sensor: str | None) -> float | None:
        if sensor is None:
            return None
        from .runtime import temperature_c  # noqa: PLC0415 - the runtime imports this module's peers

        return temperature_c(self.hass, sensor)

    def _prices(self, now: datetime) -> dict[datetime, float]:
        from .view import hour_prices_of  # noqa: PLC0415

        slots = self.site.battery_shadow.plan.buy_slots(now)
        return hour_prices_of(tuple((slot.start, slot.end, slot.buy) for slot in slots))

    # ---- the look

    async def async_look(self, now: datetime) -> None:
        """One look (see the module). Never raises."""
        try:
            await self._async_look(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again; the switch is turned on where it can be
            _LOGGER.warning("SpotNav water heater: a look failed", exc_info=True)
            self.state, self.reason = STATE_WAITING, "error"
            try:
                await self._async_release(now, "error")
            except Exception:  # noqa: BLE001 - tried again at the next look
                _LOGGER.warning("SpotNav water heater: the switch could not be turned on", exc_info=True)
        try:
            await self._async_history(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the windows' sizes as without history
            _LOGGER.debug("SpotNav water heater: the history could not be read", exc_info=True)
        self.rules.trim(now, self._day(now - timedelta(days=3)))
        try:
            self.offer = self._offer(now)
        except Exception:  # noqa: BLE001 - nothing offered: the shed order skips the water heater
            _LOGGER.debug("SpotNav water heater: its offer to the shed order could not be made", exc_info=True)
            self.offer = None
        self.control.keep_soon()

    async def _async_look(self, now: datetime) -> None:
        config = self.shadow.config()
        heater = config.water_heater
        switch = None if heater is None else heater.entity_id
        enabled = heater is not None and heater.control

        if self.entity is not None and switch != self.entity:
            # Another switch (or none): the old one is turned on first where SpotNav holds it off.
            if not await self._async_release(now, "switch_changed"):
                return
            self.entity, self.pending, self.trial, self._foreign, self._seen_on = None, None, None, None, False
            self.meter_seen = None
            self.rules = Rules(Kept(last_full=None))
        if switch is None:
            self.state, self.reason, self.want = STATE_OFF, None, None
            return
        self.entity = switch

        read = self._read(switch)
        self.power_sensor, power = self._power(switch)
        if power is not None and power >= FULL_DRAW_W and self.power_sensor is not None:
            self.meter_seen = {"entity": switch, "sensor": self.power_sensor}
        elif self.meter_seen is not None and (self.meter_seen["entity"] != switch or self.meter_seen["sensor"] != self.power_sensor):
            # Another meter: it has to show the element's draw itself.
            self.meter_seen = None
        self.metered = self.power_sensor is not None and self.meter_seen is not None
        self.meter_waiting = self.power_sensor is not None and not self.metered
        tank_sensor = None if heater is None else heater.tank_sensor
        tank_c = self._tank(tank_sensor)
        if power is not None and power >= FULL_DRAW_W:
            self._draws.append(power)
            self.kw = element_kw(self._draws)
        if self.rules.observe(now, on=read, power_w=power, metered=self.metered, tank_c=tank_c, tank=tank_sensor is not None):
            self._log(now, "full_heat")
        mode = mode_of(self.metered, tank_sensor is not None)
        self._count_draw(now, read, power)

        if enabled and self._was_enabled is False:
            # Turned on again: a failed check, a pause and the give-up are tried afresh.
            if self.check is not None and self.check.get("result") == CHECK_FAILED:
                self.check = None
            self.paused_until = self.paused_reason = None
            self.rules.kept.stopped = False
            self.rules.kept.started = None
        self._was_enabled = enabled
        if self.paused_until is not None and now >= self.paused_until:
            self.paused_until = self.paused_reason = None

        # The windows, watched or controlled.
        prices = self._prices(now)
        current = now.replace(minute=0, second=0, microsecond=0)
        size = sizes(mode, self.daily, self.kw)
        windows = plan_windows(
            now,
            time_zones.require_zone(self.shadow.tz),
            prices,
            size,
            tight=self.shadow.tight,
            kept=[window for window in self.rules.kept.windows if window.start <= now],
            off_since=self.rules.kept.off_since,
        )
        self.rules.kept.windows = windows
        fresh = current in prices
        self._look_mode, self._look_fresh = mode, fresh

        # A write is read back first.
        if self.pending is not None:
            on, at, planned = self.pending
            if read is on:
                self._confirm(now, on, planned, power)
            elif now - at > READ_BACK:
                self.pending = None
                self._log(now, "not_read_back", on=on)
                if read is None:
                    # Not seen because the switch does not read: no pause; on at its first readable look.
                    self._restore_on = self.held_off
                    return
                await self._async_failed(now, on)
                return
            else:
                return

        if read is None:
            self.state, self.reason = (STATE_WAITING, WAIT_UNAVAILABLE) if enabled else (STATE_OFF, None)
            if self.held_off:
                self._restore_on = True
            return
        if self._restore_on:
            self._restore_on = False
            if self.held_off and read is False:
                await self._async_exit(now, "restart")
                return
            self.held_off = False

        if self.held_off and read is False and self._retry_on_at is not None and now >= self._retry_on_at:
            # An on that was not seen: tried again while SpotNav may hold the switch off.
            await self._async_write(True, now, planned=False, why="retry_on")
            return

        # Never against another writer.
        foreign = None
        if self.held_off and read is True:
            foreign = WAIT_FOREIGN_ON
        elif not self.held_off and read is False and self._seen_on:
            foreign = WAIT_FOREIGN_OFF
        if foreign is None:
            self._foreign = None
        elif self._foreign is None or self._foreign[0] != foreign:
            self._foreign = (foreign, now)
            self.state, self.reason = STATE_WAITING, foreign
            return
        elif now - self._foreign[1] < FOREIGN_CONFIRM:
            return
        else:
            self._foreign = None
            self._log(now, foreign)
            _LOGGER.info("SpotNav water heater: %s was switched by someone else; left as it is until tomorrow", switch)
            self.held_off = False
            self._seen_on = False
            self.rules.stop()
            self.trial = None
            self._pause(now, foreign)

        if not enabled:
            await self._async_exit(now, "off")
            self.rules.stop()
            self._seen_on = False
            self.state, self.reason = STATE_OFF, None
            self.want = None
            return
        if self.paused_until is not None:
            await self._async_exit(now, self.paused_reason or "paused")
            self.rules.stop()
            self._seen_on = False
            self.state, self.reason = STATE_WAITING, self.paused_reason
            return
        self.automation = next(iter(steering_automations(self.hass, [switch])), None)
        if self.automation is not None:
            await self._async_exit(now, WAIT_AUTOMATION)
            self.rules.stop()
            self._seen_on = False
            self.state, self.reason = STATE_WAITING, WAIT_AUTOMATION
            return
        if self.check is not None and self.check.get("entity") == switch and self.check.get("result") == CHECK_FAILED:
            await self._async_exit(now, WAIT_CHECK_FAILED)
            self.rules.stop()
            self._seen_on = False
            self.state, self.reason = STATE_WAITING, WAIT_CHECK_FAILED
            return
        self.rules.start(now)
        if self.rules.gave_up(now, mode):
            if self.reason != WAIT_NOT_FULL:
                self._log(now, "not_full")
                _LOGGER.warning("SpotNav water heater: %s has not reached its thermostat for two days; control stopped", switch)
            await self._async_exit(now, WAIT_NOT_FULL)
            self.state, self.reason = STATE_WAITING, WAIT_NOT_FULL
            return

        if self.trial is not None and self.trial.get("phase") == "cut":
            # The check's off read back while the element drew: its power must fall.
            if power is not None and power < CUT_W:
                self.trial = {"phase": "on"}
                self._log(now, "check_cut_seen")
            elif now - self.trial["at"] > CUT_WITHIN:
                self._fail_check(now, "no_cut")
                await self._async_exit(now, WAIT_CHECK_FAILED)
                self.state, self.reason = STATE_WAITING, WAIT_CHECK_FAILED
                return
            else:
                # Held off the check's two minutes at most, whatever comes: then judged, and on if it failed.
                self.state, self.reason = STATE_VERIFYING, "check"
                return

        # Seen on while control runs: an off from here on that SpotNav did not write is someone else's.
        self._seen_on = read is True and not self.held_off
        shed_until = self._shed_until(now)
        if shed_until is not None:
            # Only where the window left after the pause still covers the heater's need.
            latest = self._until_max(now)
            if latest is None or latest < shed_until:
                shed_until = None
        want = self.want = self.rules.want(
            now, mode=mode, windows=windows, plan_fresh=fresh, day=self._day(now), shed_until=shed_until
        )
        verifying = self.check is None or self.check.get("entity") != switch
        self.state, self.reason = (STATE_VERIFYING if verifying else STATE_RUNNING), want.reason
        if want.on == read:
            return
        await self._async_write(want.on, now, planned=(not want.on) or want.reason == WINDOW_CHEAP, why=want.reason, power=power)

    # ---- the shed order (`site/shed_order.py`)

    def _shed_until(self, now: datetime) -> datetime | None:
        order = getattr(self.site, "shed_order", None)
        return None if order is None else order.until(LOAD_WATER_HEATER, now)

    def _give_w(self, read: bool | None) -> float:
        """What a pause gives now (W): what a trusted meter reads the element drawing now; nothing while the switch
        reads off, without a trusted meter or without a reading (never a learnt figure: the hold counts it as freed)."""
        if read is not True or not self.metered or self.power_sensor is None:
            return 0.0
        state = self.hass.states.get(self.power_sensor)
        if state is None or state.state in ("unavailable", "unknown"):
            return 0.0
        value = number_from_state(state.state)
        if value is None:
            return 0.0
        return max(0.0, value * 1000.0 if state.attributes.get("unit_of_measurement") == "kW" else value)

    def _until_max(self, now: datetime) -> datetime | None:
        """The latest a pause may last and still leave the cheap window running now the time its remaining need takes
        (`shed_order.Offer.until_max`): the window's end less its energy at the element's power, less what the element
        was seen drawing in it. `now` (no pause) where that is not known (no window energy or power learnt yet)."""
        window = next((item for item in self.rules.kept.windows if item.covers(now)), None)
        if window is None or window.kwh is None or self.kw is None or self.kw <= 0:
            return now
        need_s = max(0.0, window.kwh / self.kw * 3600.0 - self._drawn_s.get(window.key, 0.0))
        return window.end - timedelta(seconds=need_s)

    def _count_draw(self, now: datetime, read: bool | None, power: float | None) -> None:
        """The time the element was seen drawing in the window running now (`_until_max`), since the last look."""
        last, self._drawn_at = self._drawn_at, now
        window = next((item for item in self.rules.kept.windows if item.covers(now)), None)
        keys = {item.key for item in self.rules.kept.windows}
        self._drawn_s = {key: seconds for key, seconds in self._drawn_s.items() if key in keys}
        if window is None or last is None or read is not True or power is None or power < FULL_DRAW_W:
            return
        self._drawn_s[window.key] = self._drawn_s.get(window.key, 0.0) + min(max(0.0, (now - last).total_seconds()), 2 * LOOK_S)

    def _offer(self, now: datetime) -> Offer | None:
        """The water heater's offer to the shed order (`shed_order.Offer`), `None` without a switch chosen. Controlled:
        ready while running in a cheap window (or already paused for the ceiling), its hard rules not holding it on.
        Watched: what it would give in a cheap window, `would` only."""
        heater = self.shadow.config().water_heater
        if heater is None or self.entity != heater.entity_id:
            return None
        read = self._read(heater.entity_id)
        give = self._give_w(read)
        if not heater.control:
            window = next((item for item in self.rules.kept.windows if item.covers(now)), None)
            ready = read is True and window is not None and window.reason == WINDOW_CHEAP
            return Offer(live=False, ready=ready, give_w=give, drawing=read is True)
        reason = None if self.want is None else self.want.reason
        running = self.state == STATE_RUNNING
        if not running:
            return Offer(live=True, ready=False, give_w=give, drawing=read is True, why=self.reason)
        held = self.held_off and reason == WANT_SHED
        # The rules asked as the next look would (the write cap, the longest off stretch, every hard rule).
        ready = held or (
            reason == WINDOW_CHEAP
            and self._look_mode is not None
            and self.rules.would_shed(
                now, mode=self._look_mode, windows=self.rules.kept.windows, plan_fresh=self._look_fresh, day=self._day(now)
            )
        )
        return Offer(
            live=True,
            ready=ready,
            give_w=give,
            drawing=read is True,
            why=None if ready else reason,
            held=held,
            until_max=self._until_max(now),
        )

    def shed_offer(self) -> Offer | None:
        """The offer as of the last look, with the switch and the meter read now (whether it still draws, and what)."""
        offer = self.offer
        if offer is None or self.entity is None:
            return offer
        read = self._read(self.entity)
        return replace(offer, drawing=read is True, give_w=self._give_w(read))

    # ---- writes

    async def _async_call(self, entity_id: str, on: bool) -> None:
        domain = entity_id.split(".", 1)[0]
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self.hass.services.async_call(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True)

    async def _async_write(self, on: bool, now: datetime, *, planned: bool, why: str, power: float | None = None) -> None:
        """Switch the water heater; an off is kept in the store before it is written (an exit must find it)."""
        if self.entity is None:
            return
        if not on:
            self.held_off = True
            if not await self.control.async_keep():
                self.held_off = False
                return
            if self.check is None or self.check.get("entity") != self.entity:
                self.trial = {"phase": "off", "drew": power is not None and power >= FULL_DRAW_W}
        self.pending = (on, now, planned)
        self._retry_on_at = None
        self._log(now, "write", on=on, why=why)
        try:
            await self._async_call(self.entity, on)
        except Exception as err:  # noqa: BLE001 - not read back: the look after `READ_BACK` gives it up
            _LOGGER.warning("SpotNav water heater: switching %s %s failed: %s", self.entity, "on" if on else "off", err)

    def _confirm(self, now: datetime, on: bool, planned: bool, power: float | None) -> None:
        self.pending = None
        self.rules.switched(now, on, day=self._day(now), planned=planned)
        self.held_off = not on
        self._retry_on_at = None
        trial = self.trial
        if trial is None:
            return
        if not on and trial.get("phase") == "off":
            if trial.get("drew") and not (power is not None and power < CUT_W):
                self.trial = {"phase": "cut", "at": now}
            else:
                self.trial = {"phase": "on"}
        elif on and trial.get("phase") == "on":
            self.trial = None
            self.check = {"entity": self.entity, "result": CHECK_PASSED, "at": now.isoformat()}
            self._log(now, "check_passed")

    async def _async_failed(self, now: datetime, on: bool) -> None:
        """A write not read back: during the check, its failure; else a pause to the next day. An off is followed by an
        on (the fail-safe), an on is tried again while SpotNav may hold the switch off."""
        if self.check is None or self.check.get("entity") != self.entity:
            self._fail_check(now, "no_read_back")
        else:
            self._pause(now, WAIT_NO_READ_BACK)
        self.state, self.reason = STATE_WAITING, WAIT_CHECK_FAILED if self._check_failed() else WAIT_NO_READ_BACK
        if on:
            self._retry_on_at = now + RETRY_ON
        else:
            await self._async_write(True, now, planned=False, why="not_read_back")

    def _fail_check(self, now: datetime, why: str) -> None:
        self.trial = None
        self.check = {"entity": self.entity, "result": CHECK_FAILED, "at": now.isoformat(), "why": why}
        self._log(now, "check_failed", why=why)
        _LOGGER.info("SpotNav water heater: the check of %s failed (%s); control stopped", self.entity, why)

    def _check_failed(self) -> bool:
        return self.check is not None and self.check.get("result") == CHECK_FAILED

    async def _async_exit(self, now: datetime, why: str) -> None:
        """Turn on the switch SpotNav holds off (nothing otherwise). One that does not read is turned on at its first
        readable look."""
        self.trial = None
        if not self.held_off or self.entity is None or self.pending is not None:
            return
        read = self._read(self.entity)
        if read is None:
            self._restore_on = True
            return
        if read is True:
            self.held_off = False
            return
        self._log(now, "exit", why=why)
        await self._async_write(True, now, planned=False, why=why)

    async def _async_release(self, now: datetime, why: str) -> bool:
        """Turn on the switch SpotNav holds off where the look's own read-back does not run (another switch was chosen
        in the settings, a look failed): an on that has not shown within `READ_BACK` is written again, an off still
        awaited is overruled. `True` once nothing is held off."""
        if not self.held_off or self.entity is None:
            return True
        if self._read(self.entity) is True:
            self.held_off, self.pending, self._restore_on = False, None, False
            return True
        if self.pending is not None:
            on, at, _planned = self.pending
            if on and now - at <= READ_BACK:
                return False
            self.pending = None
        await self._async_exit(now, why)
        return not self.held_off

    def _pause(self, now: datetime, reason: str) -> None:
        if self.paused_until is not None:
            return
        tz = time_zones.require_zone(self.shadow.tz)
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        self.paused_until = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz).astimezone(now.tzinfo)
        self.paused_reason = reason

    # ---- the history and the forecast

    async def _async_history(self, now: datetime) -> None:
        """The last days' kWh of the switch's power sensor (hourly means), read every `HISTORY_REFRESH`."""
        if self.power_sensor is None:
            self.daily = []
            return
        if self._history_at is not None and now - self._history_at < HISTORY_REFRESH:
            return
        self._history_at = now
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        sensor = self.power_sensor
        start = now - timedelta(days=HISTORY_DAYS + 1)

        def read() -> dict[str, list[Any]]:
            return dict(statistics_during_period(self.hass, start, now, {sensor}, "hour", {"power": "kW"}, {"mean"}))

        rows = await get_instance(self.hass).async_add_executor_job(read)
        hourly: dict[datetime, float] = {}
        for row in rows.get(sensor) or []:
            moment, mean = row.get("start"), row.get("mean")
            if isinstance(moment, (int, float)) and not isinstance(moment, bool):
                moment = datetime.fromtimestamp(float(moment), dt_util.UTC)
            if isinstance(moment, datetime) and isinstance(mean, (int, float)):
                hourly[moment] = float(mean)
        self.daily = daily_kwh(hourly, time_zones.require_zone(self.shadow.tz), now)

    def forecast_delta(self, now: datetime) -> dict[datetime, float]:
        """The windows' energy moved into them while control runs (`water_heater.forecast_delta`)."""
        if self.state != STATE_RUNNING:
            return {}
        return forecast_delta(self.rules.kept.windows, now, time_zones.require_zone(self.shadow.tz))

    # ---- helpers and what is shown

    def _day(self, now: datetime) -> str:
        return now.astimezone(time_zones.require_zone(self.shadow.tz)).date().isoformat()

    def _log(self, now: datetime, what: str, **extra: Any) -> None:
        self.log.append({"at": now.isoformat(), "what": what, **extra})

    def mode(self, config: HeatConfig) -> str:
        heater = config.water_heater
        return mode_of(self.metered, heater is not None and heater.tank_sensor is not None)

    def not_full(self) -> bool:
        """Control stopped for want of full heat (the note), while the opt-in is on."""
        config = self.shadow.config()
        return config.water_heater is not None and config.water_heater.control and self.rules.kept.stopped

    def status(self, config: HeatConfig | None = None) -> dict[str, Any] | None:
        """The card's `water_heater` block (see `heat/view.py`); `None` without a switch chosen."""
        config = config or self.shadow.config()
        heater = config.water_heater
        if heater is None:
            return None
        state = self.hass.states.get(heater.entity_id)
        now = dt_util.utcnow()
        live = heater.control and self.state in (STATE_RUNNING, STATE_VERIFYING)
        return {
            "entity_id": heater.entity_id,
            "name": heater.entity_id if state is None else state.name,
            "on": self._read(heater.entity_id),
            "metered": self.metered,
            "meter_waiting": self.meter_waiting,
            "mode": self.mode(config),
            "kw": None if self.kw is None else round(self.kw, 2),
            "state": self.state if heater.control else STATE_OFF,
            "reason": self.reason,
            "check": None if self.check is None or self.check.get("entity") != heater.entity_id else self.check.get("result"),
            "last_full": _iso(self.rules.kept.last_full),
            "until": None if self.want is None or self.want.until is None or not live else self.want.until.isoformat(),
            "windows": [
                {"start": window.start.isoformat(), "end": window.end.isoformat(), "reason": window.reason}
                for window in self.rules.kept.windows
                if window.end > now
            ],
            "live": live,
            "automation": None
            if self.automation is None or self.reason != WAIT_AUTOMATION
            else {"entity_id": self.automation.entity_id, "name": self.automation.name},
            "paused_until": _iso(self.paused_until),
        }

    def diagnostics(self) -> dict[str, Any]:
        config = self.shadow.config()
        mode = self.mode(config)
        size = sizes(mode, self.daily, self.kw)
        return {
            "kept": self.stored(),
            "mode": mode,
            "power_sensor": self.power_sensor,
            "meter_waiting": self.meter_waiting,
            "kw": self.kw,
            "need_kwh": None if mode == MODE_UNMETERED else (None if size.night_kwh is None else round((size.night_kwh or 0) + (size.day_kwh or 0), 3)),
            "daily_kwh": [round(kwh, 3) for kwh in self.daily],
            "sizes": {"night_hours": size.night_hours, "day_hours": size.day_hours, "night_end": size.night_end, "hard_night": size.hard_night},
            "state": self.state,
            "reason": self.reason,
            "want": None if self.want is None else {"on": self.want.on, "reason": self.want.reason, "until": _iso(self.want.until)},
            "hard_rule": None if self.want is None or self.want.reason in (WANT_OFF, WINDOW_CHEAP) else self.want.reason,
            "pending": None if self.pending is None else {"on": self.pending[0], "at": self.pending[1].isoformat()},
            "trial": self.trial,
            "log": list(self.log),
        }
