"""SpotNav's heat control: the heat pump's relay switched by SpotNav itself, behind the person's opt-in (plan
`plans/v3_heating_plan.md` step 5, the owner's decisions of 2026-11-16). One per site, owned by its
`SiteCapacityController`, beside the heat shadow (`heat/runtime.py`) whose plan it carries out.

**The opt-in**: the heat settings' `control_enabled` ("Styr värmen", off by default). Nothing is written without it.
The rules and the check are pure (`heat/relay_control.py`).

**The paths**: the relay and SG Ready's two contacts are switches. The SG Ready API's mode and a curve offset are
read as the same two contacts on one number (`heat/paths.py`, which says what each value means and what is written;
none of them has run on a real heat pump yet). Everything below holds for them as for a relay, "closed" being the
lowering (or the raise) as SpotNav wrote it and "open" the pump's normal state:

* *SG Ready API*: its activation switch is turned on before the first closing (kept first, read back, at most
  `paths.ACTIVATION_WRITES_DAY` a day) and, where SpotNav turned it on, off again once control no longer runs,
  nothing is held and the mode reads normal: mode normal first, then the switch. Where the mode does not go back
  (a stop that cannot wait for it, `OPEN_TRIES` openings that failed, a contact let go), the switch SpotNav turned on
  is turned off all the same: it ends the block or the raise by itself (`_async_fallback_off`).
* *A stored offset* (NIBE): the value found is kept before the lowering is written (`snapshots`) and written back
  at every exit, at Home Assistant's stop and, where that did not take, at the next start. A closing waits for the
  plan's write spacing (`paths.move_waits`, kept in `offset_moves`); an opening never does. A value that is no
  longer the one SpotNav wrote is someone else's: nothing is written back.
* *A volatile offset* (CTC): written again every `paths.RENEW` while held (a renewal, no dead-man: see `paths`; its
  own day count, `renewals`); a renewal that has failed for `paths.EXPIRY` ends the lowering and pauses control for
  the day. Handed back by writing the release; unknown reads as released.

**Each look** (`LOOK_S`, at once after a heat settings write, and within `CONTACT_DEBOUNCE_S` of a contact's change
SpotNav did not make), never raises:

1. A relay entity that changed is given back first (the old one opened if SpotNav holds it), and checked again.
2. Off, no relay path, a pause or a block: SpotNav's closing is given back (opened) and nothing else is written.
   Blocks (nothing written until they go, each a line under the switch): an enabled automation of the household that
   references the relay (`automation`, one actionable note), another heat optimiser set up (`other_controller`),
   the pump's own price control on (`pump_price_control`), the relay unreadable (`relay_unavailable`), the relay
   closed by someone else (`relay_closed`).
3. **A write is read back**: the relay must show it within `READ_BACK`; a write that raised or was not seen pauses
   control for the day (`no_read_back`) after an opening (tried again every `RETRY_OPEN` while SpotNav may hold it).
   A relay that does not read (`relay_unavailable`) is not written and starts no pause: what SpotNav may hold is
   opened at its first readable look.
4. **Never fight another writer**: a change SpotNav did not make (the relay opened while SpotNav held it, or closed
   while it was open), read on every look for `FOREIGN_CONFIRM`, gives the relay up (nothing written) and pauses
   control until the next local day (`foreign_change`). A closed reading within `OWN_WINDOW` of a closing of SpotNav's
   that never read closed (it failed, timed out or was given up) is SpotNav's own: opened once, and only if it stays
   closed after that is it someone else's. SpotNav's own closing is never left unbounded.
5. **The check** before control runs, at the first start and after the relay entity changes (kept per entity): the
   meter's power now and its levels (read again every hour from its 5-minute rows) or the pump's calculated supply
   (`heat/facts.py`); without either nothing can be judged (`no_signal`). A failed check stops control
   (`check_failed`) until the switch is turned off and on again.
6. **Control**: `relay_control.decide` on the heat shadow's plan (its dear and tight hours, and the house too warm), with SpotNav's lowerings of the last 24 hours, its writes and
   the safety reasons: no fresh plan, no outdoor reading or chosen rooms none of which reads (`stale`; a sensor that
   has not reported for `runtime.STALE_AFTER` does not read), outdoors at or below -15 °C (the lowest of the chosen
   sensor, the site's reading and the weather's forecast for the hour), the coldest counted room (its offset counted)
   below the band, SG Ready's second contact on or unreadable, a hot-water boost on where the path blocks the
   compressor (`paths.blocks_hot_water`: a block may stop hot water too, and a boost someone started is never cut off).

7. **SG Ready's raise** (contact B, where SG Ready has both contacts): after the lowering's look,
   `relay_control.decide_raise` on the plan's raise hours (the cheap hour before a lowered run, the warm-up before the
   return) while control runs. **(1,1) never occurs**: B closes only while A has read open for `DEAD_TIME` with
   nothing pending and SpotNav does not hold it (`_a_clear`), and A closes only while B has (`_b_clear`); a lowering
   due while B is closed opens B first and closes A at a later look. Each write is read back before the next, and a
   closing is kept in the store before it is written. B held while A reads anything but open (closed by someone
   else, held or written) is opened at once. No raise through a contact whose state is only assumed
   (`raise_assumed_state`, in the diagnostics only). B is read back, never fought (a change SpotNav did not make sets raising aside for
   the day), and checked on the first raises (`relay_control.RaiseCheck`): no raise seen on the supply sets raising
   aside (`raise_not_observed`, in the diagnostics only) while lowering goes on.

**Every exit opens both contacts** SpotNav holds (B first, then A); the relay path's (open = the pump's normal curve): the switch off, a block, a pause, a
safety reason, a changed relay entity, unload and Home Assistant's stop (`async_shutdown`, bounded); what could not be
opened then is opened at the next start before anything else; a kept state that cannot be read opens both contacts
once (behind the opt-in). Kept in `spotnav.heat_control.<entry>`: whether SpotNav
holds the relay closed, the lowerings and writes of the last days, the checks per relay, the pause.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_HEAT_OFFSET_HOME, DOMAIN
from . import optin, paths
from .automations import SteeringAutomation, steering_automations
from .config import BAND_C, HeatConfig
from .shadow_rule import RETURN_HOURS
from .meter_split import Levels, levels, state_now
from .relay_control import (
    closings_ahead as relay_closings_ahead,
    FAILED,
    GOOD_FOR,
    NO_LOWER_AT_C,
    PASSED,
    RAISE_AWAY,
    RAISE_INTERLOCK,
    RAISE_NIGHT,
    RAISE_NOT_OBSERVED,
    RAISE_SETTLING,
    RAISE_STALE,
    RAISE_WARM,
    BASELINE,
    PHASE_WAIT,
    RAISE_ASSUMED_STATE,
    SAFETY_HOT_WATER,
    SAFETY_ROOM_COLD,
    SAFETY_SETTLING,
    SAFETY_SG_READY_B,
    SAFETY_STALE,
    SAFETY_TOO_COLD,
    Check,
    Decision,
    RaiseCheck,
    RaiseSituation,
    Sample,
    Situation,
    decide,
    decide_raise,
    lowered_within,
    planned_raise_end,
    planned_run_end,
    verdict,
)

if TYPE_CHECKING:
    from .runtime import HeatShadow

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
LOOK_S: Final = 30.0
#: A contact's change SpotNav did not make is looked at this soon (changes within it make one look).
CONTACT_DEBOUNCE_S: Final = 1.0
#: A write not seen on the relay within this long is a failure.
READ_BACK: Final = timedelta(seconds=90)
#: After a failed opening, how often it is tried again while SpotNav may hold the relay closed.
RETRY_OPEN: Final = timedelta(minutes=5)
#: A change SpotNav did not make must read on looks this far apart (and on every look between) to count.
FOREIGN_CONFIRM: Final = timedelta(seconds=60)
#: A closed reading this soon after a closing of SpotNav's that never read closed is that closing, late: opened once.
OWN_WINDOW: Final = timedelta(minutes=5)
#: SG Ready: a contact must have read open this long (since its opening was confirmed, or since it was first seen open)
#: before the other one closes, so a slow relay or a stale state never makes (1,1), not even for an instant.
DEAD_TIME: Final = timedelta(seconds=10)
#: Openings of a relay no longer chosen that were not read back before SpotNav lets it go.
OPEN_TRIES: Final = 3
#: How long a service call may take.
CALL_TIMEOUT_S: Final = 10.0
#: How long the shutdown waits for the opening to be read back (s).
SHUTDOWN_WAIT_S: Final = 5.0
#: How long the SG Ready API's activation switch may take to read on before the mode is written (s).
ACTIVATE_WAIT_S: Final = 5.0
#: The plan must have been made within this long to be acted on.
FRESH_PLAN: Final = timedelta(hours=2)
#: The meter's readings averaged for what the pump does (a compressor start's spike is no hot-water run), and how much
#: of it must have been seen.
POWER_MEAN: Final = timedelta(minutes=5)
POWER_MEAN_SEEN: Final = timedelta(minutes=4)
#: Measured closings (the meter's blocks) that each took at least `MEASURED_SHARE` of the heat away, enough to let a
#: single power-only try pass the check.
MEASURED_BLOCKS: Final = 2
MEASURED_SHARE: Final = 0.3
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
#: The raise's reason while the lowering's control does not run (off, verifying or waiting): B stays open.
RAISE_OFF: Final = "raise_off"

#: The relay's states that read as closed.
CLOSED: Final = frozenset({"on"})


def lower_entity(config: HeatConfig) -> str | None:
    """Contact A (the lowering) of the settings' control path, as its key (`paths.lower_contact`: a switch's entity
    id, or the SG Ready API's mode or an offset read as A); `None` where there is nothing to steer."""
    return paths.lower_contact(config)


def raise_entity(config: HeatConfig) -> str | None:
    """Contact B (the raise), as its key (`paths.raise_contact`): SG Ready's second contact where it has both, the
    SG Ready API's low-price mode, an offset above the value found; `None` where the path cannot raise."""
    return paths.raise_contact(config) or None


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
        #: SG Ready's raise (contact B): the contact SpotNav may hold, whether it holds it closed and since when, the
        #: raises of the last days, the raise check and raising set aside until the next day.
        self.raise_entity: str | None = None
        self.raised = False
        self.raised_since: datetime | None = None
        self.raise_last_end: datetime | None = None
        self.raisings: list[list[datetime | None]] = []
        self.raise_check = RaiseCheck()
        self.raise_paused_until: datetime | None = None
        self.raise_paused_reason: str | None = None
        #: The offsets SpotNav moved, per contact: the value found and the value written (`paths.Snapshot`).
        self.snapshots: dict[str, paths.Snapshot] = {}
        #: The SG Ready API's activation switch while SpotNav turned it on: `{entity, mode}` (the mode's number).
        self.activation: dict[str, str] | None = None
        #: Per local day: the volatile offset's renewals, and the activation switch's turnings on.
        self.renewals: dict[str, int] = {}
        self.activations: dict[str, int] = {}
        #: A stored offset's writes of the last `paths.REVERSAL_SPACING`: `(when, direction)`, for the write spacing.
        self.offset_moves: list[tuple[datetime, int]] = []
        # Seen.
        #: A stored offset's value as last read while SpotNav held nothing on it, since this start: a change between
        #: two such readings is the household's own (`_track_home`).
        self._offset_seen: dict[str, float] = {}
        #: The stored offset reads other than the household's own value while SpotNav holds nothing:
        #: `{entity_id, value, home}`, the card's line asks to put it back or keep it (`async_offset_home`).
        self.offset_home: dict[str, Any] | None = None
        #: Per contact: when its volatile value was last written, and last written with success.
        self._renewed_at: dict[str, datetime] = {}
        self._renew_ok: dict[str, datetime] = {}
        self._deactivate_at: datetime | None = None
        #: Since when control has not run on the SG Ready API's path with nothing held.
        self._idle_since: datetime | None = None
        self.pending_b: Pending | None = None
        self.raise_decision: Decision | None = None
        self._reopen_b = False
        self._b_retry_open_at: datetime | None = None
        self._b_open_failures = 0
        self._b_foreign: datetime | None = None
        self._b_own_reopen_at: datetime | None = None
        #: A closing of B written but never read closed: it may still take, so A waits `OWN_WINDOW` after it.
        self._b_close_unseen_at: datetime | None = None
        #: Per contact: since when it has read open without a break (see `DEAD_TIME`).
        self._open_since: dict[str, datetime] = {}
        self._was_enabled_raise: bool | None = None
        #: The check's readings of the last `BASELINE` while B was open: a raise's baseline.
        self._recent: deque[tuple[datetime, Sample]] = deque()
        self.pending: Pending | None = None
        self.state = STATE_OFF
        self.reason: str | None = None
        self.decision: Decision | None = None
        #: The safety reason at the last look (`relay_control.SAFETY_*`).
        self.safety: str | None = None
        self.automation: SteeringAutomation | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self._was_enabled: bool | None = None
        #: Held at the last stop: opened at the first look before anything else.
        self._reopen = False
        self._expect: bool | None = None
        self._good_since: datetime | None = None
        self._retry_open_at: datetime | None = None
        #: A change SpotNav did not make, as first read: `(open|close, since)`.
        self._foreign: tuple[str, datetime] | None = None
        #: A closing of A written but never read closed (it may still take: B waits `OWN_WINDOW` after it, and a closed
        #: reading within it is SpotNav's own), and when SpotNav opened such a late closing (once per closing).
        self._a_close_unseen_at: datetime | None = None
        self._own_reopen_at: datetime | None = None
        #: The waits logged (`close_waits_for_b`, `raise_waits_for_a`) and why: logged again only when that changes.
        self._waits: dict[str, str] = {}
        #: Openings in a row not read back.
        self._open_failures = 0
        self._levels: Levels | None = None
        #: The meter's power as read at the looks, the last `POWER_MEAN`.
        self._power: deque[tuple[datetime, float]] = deque()
        #: Whether anything can show the check an effect: a power meter or a calculated supply the pump reports.
        self.signal = False
        self._levels_hour: datetime | None = None
        self._loaded = False
        #: The kept data as last saved (or loaded).
        self._saved: dict[str, Any] | None = None
        self._closed = False
        self._looking = False
        self._again = False
        self._task: asyncio.Task[None] | None = None
        self._timer_cancel: Callable[[], None] | None = None
        #: The contacts watched for changes SpotNav did not make, and the look such a change asked for.
        self._watched: tuple[str, ...] = ()
        self._watch_cancel: Callable[[], None] | None = None
        self._debounce_cancel: Callable[[], None] | None = None

    # ---- life cycle

    async def async_start(self) -> None:
        try:
            self._restore(await self._store.async_load())
        except Exception:  # noqa: BLE001 - a store that cannot be read starts again, the contacts opened once
            _LOGGER.warning(
                "SpotNav heat control: the kept state could not be read; starting a new one and opening the contacts",
                exc_info=True,
            )
            self._open_unknown()
        self._saved = self._data()
        self._loaded = True
        self._timer_cancel = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=LOOK_S))
        self._watch_contacts()
        self.request_look()

    def _open_unknown(self) -> None:
        """The kept state is lost, so whether SpotNav left a contact closed is not known: behind the opt-in, both
        contacts are taken as held and opened once at the first look where they read (as after a stop while held; one
        that reads open is left alone). Without it SpotNav's own closing would read as someone else's for good."""
        config = self.shadow.config()
        if not config.control_enabled:
            return
        self.relay, self.raise_entity = lower_entity(config), raise_entity(config)
        if self.relay is not None:
            self.held, self.held_since, self._reopen = True, None, True
        if self.raise_entity is not None:
            self.raised, self.raised_since, self._reopen_b = True, None, True

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
        self.raise_entity = raw.get("raise_entity") if isinstance(raw.get("raise_entity"), str) else None
        self.raised = raw.get("raised") is True and self.raise_entity is not None
        self.raised_since = _instant(raw.get("raised_since"))
        self.raise_last_end = _instant(raw.get("raise_last_end"))
        for item in raw.get("raisings") or []:
            if isinstance(item, list) and len(item) == 2 and (start := _instant(item[0])) is not None:
                self.raisings.append([start, _instant(item[1])])
        self.raise_check = RaiseCheck.from_dict(raw.get("raise_check"))
        self.raise_paused_until = _instant(raw.get("raise_paused_until"))
        self.raise_paused_reason = raw.get("raise_paused_reason") if isinstance(raw.get("raise_paused_reason"), str) else None
        if self.raised:
            # B held at the last stop: opened at the first look, before A may close.
            self._reopen_b = True
            _LOGGER.info("SpotNav heat control: the raise contact %s was held at the last stop; opening it", self.raise_entity)
        snapshots = raw.get("snapshots")
        if isinstance(snapshots, dict):
            for key, item in snapshots.items():
                if isinstance(key, str) and (snapshot := paths.Snapshot.from_dict(item)) is not None:
                    self.snapshots[key] = snapshot
        activation = raw.get("activation")
        if isinstance(activation, dict) and isinstance(activation.get("entity"), str) and isinstance(activation.get("mode"), str):
            self.activation = {"entity": activation["entity"], "mode": activation["mode"]}
        for name in ("renewals", "activations"):
            counts = raw.get(name)
            if isinstance(counts, dict):
                setattr(self, name, {day: count for day, count in counts.items() if isinstance(count, int)})
        for item in raw.get("offset_moves") or []:
            if isinstance(item, list) and len(item) == 2 and (at := _instant(item[0])) is not None and item[1] in (-1, 1):
                self.offset_moves.append((at, item[1]))

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
            "raise_entity": self.raise_entity,
            "raised": self.raised,
            "raised_since": None if self.raised_since is None else self.raised_since.isoformat(),
            "raise_last_end": None if self.raise_last_end is None else self.raise_last_end.isoformat(),
            "raisings": [[start.isoformat(), None if end is None else end.isoformat()] for start, end in self.raisings],  # type: ignore[union-attr]
            "raise_check": self.raise_check.as_dict(),
            "raise_paused_until": None if self.raise_paused_until is None else self.raise_paused_until.isoformat(),
            "raise_paused_reason": self.raise_paused_reason,
            "snapshots": {key: snapshot.as_dict() for key, snapshot in self.snapshots.items()},
            "activation": None if self.activation is None else dict(self.activation),
            "renewals": self.renewals,
            "activations": self.activations,
            "offset_moves": [[at.isoformat(), direction] for at, direction in self.offset_moves],
        }

    def _save(self) -> None:
        """Save (delayed) when the kept data changed since the last save."""
        if not self._loaded:
            return
        data = self._data()
        if data == self._saved:
            return
        self._saved = data
        self._store.async_delay_save(self._data, 1.0)

    async def _async_save_now(self) -> bool:
        """Save at once (before a closing is written); `False` when it could not be kept."""
        data = self._data()
        try:
            await self._store.async_save(data)
        except Exception:  # noqa: BLE001 - not kept: the closing is not written
            _LOGGER.warning("SpotNav heat control: the state could not be kept; nothing is closed", exc_info=True)
            return False
        self._saved = data
        return True

    async def async_shutdown(self) -> None:
        """Open the relay SpotNav holds (bounded) and keep the state: a relay still held is opened at the next start."""
        self._closed = True
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None
        self._unwatch_contacts()
        if self._task is not None and not self._task.done():
            # The running look ends first (a write it was making is over), so the opening below is the last word.
            self._task.cancel()
            try:
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    await asyncio.wait({self._task})
            except TimeoutError:
                _LOGGER.warning("SpotNav heat control: a look did not end at the stop")
        if not self._loaded:
            return
        if self.raised and self.raise_entity is not None:
            # B first: opening never makes (1,1), and the pump is back to normal before A is touched.
            try:
                await self._async_call(self.raise_entity, False)
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    while self._read(self.raise_entity) is True:
                        await asyncio.sleep(0.2)
                if self._read(self.raise_entity) is False:
                    self._end_raise(dt_util.utcnow())
            except Exception:  # noqa: BLE001 - kept held: opened at the next start
                _LOGGER.warning("SpotNav heat control: the raise contact could not be opened at the stop", exc_info=True)
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
        if self.held or self.raised:
            # The SG Ready API's mode did not read normal in time: its activation switch is the second way back.
            await self._async_fallback_off(dt_util.utcnow(), "stop", stopping=True)
        try:
            await self._async_deactivate(dt_util.utcnow(), stopping=True)
        except Exception:  # noqa: BLE001 - kept: turned off at the next start
            _LOGGER.warning("SpotNav heat control: the SG Ready API could not be turned off at the stop", exc_info=True)
        await self._store.async_save(self._data())

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """Forget the kept state when the site is removed. What the hand-back at the unload could not give back is
        the last record of it: logged first, so it can be set back by hand."""
        store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, store_key(entry_id))
        try:
            kept = await store.async_load()
        except Exception:  # noqa: BLE001 - nothing to tell
            kept = None
        if isinstance(kept, dict) and (kept.get("held") or kept.get("raised") or kept.get("snapshots") or kept.get("activation")):
            _LOGGER.warning(
                "SpotNav heat control: removed while a change of its was not handed back; set it back by hand: "
                "lowering %s held %s, raise %s held %s, offset values found %s, SG Ready switch turned on %s",
                kept.get("relay"),
                kept.get("held"),
                kept.get("raise_entity"),
                kept.get("raised"),
                kept.get("snapshots"),
                kept.get("activation"),
            )
        await store.async_remove()

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

    def _watch_contacts(self) -> None:
        """Watch the contacts SpotNav may write (the chosen ones, and those it still holds): a change it did not make
        is looked at within `CONTACT_DEBOUNCE_S`, not at the next look."""
        config = self.shadow.config()
        keys = (lower_entity(config), raise_entity(config), self.relay, self.raise_entity)
        switches = (paths.activation_switch(config), None if self.activation is None else self.activation["entity"])
        wanted = tuple(sorted({paths.entity_of(key) for key in keys if key} | {entity for entity in switches if entity}))
        if wanted == self._watched and (self._watch_cancel is not None or not wanted):
            return
        if self._watch_cancel is not None:
            self._watch_cancel()
            self._watch_cancel = None
        self._watched = wanted
        if wanted:
            self._watch_cancel = async_track_state_change_event(self.hass, list(wanted), self._contact_changed)

    def _unwatch_contacts(self) -> None:
        if self._watch_cancel is not None:
            self._watch_cancel()
            self._watch_cancel = None
        self._watched = ()
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._debounce_cancel = None

    @callback
    def _contact_changed(self, event: Event[EventStateChangedData]) -> None:
        """A watched contact changed: a look soon, unless it is what SpotNav itself just wrote (never fought)."""
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        if old is not None and new is not None and old.state == new.state:
            return
        config = self.shadow.config()
        own = False
        # The state machine holds the new state already: each contact on the entity is read as the looks read it.
        for key in {lower_entity(config), raise_entity(config), self.relay, self.raise_entity}:
            if key is None or paths.entity_of(key) != entity_id:
                continue
            closed = self._read(key)
            if closed is not False:
                self._open_since.pop(key, None)
            if (key == self.relay and self.pending is not None and self.pending.closed == closed) or (
                key == self.raise_entity and self.pending_b is not None and self.pending_b.closed == closed
            ):
                own = True
        if own or self._closed or self._debounce_cancel is not None:
            return
        self._debounce_cancel = async_call_later(self.hass, CONTACT_DEBOUNCE_S, self._contact_look)

    @callback
    def _contact_look(self, _now: datetime) -> None:
        self._debounce_cancel = None
        self.request_look()

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
        self._forget(now)
        self._safe_track_home()
        try:
            await self._async_look(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again; the relay is opened where it can be
            _LOGGER.warning("SpotNav heat control: a look failed", exc_info=True)
            self._set(STATE_WAITING, "error")
            try:
                await self._async_exit(now, "error")
            except Exception:  # noqa: BLE001 - tried again at the next look
                _LOGGER.warning("SpotNav heat control: the relay could not be given back", exc_info=True)
        try:
            await self._async_raise_look(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again; B is opened where it can be
            _LOGGER.warning("SpotNav heat control: the raise's look failed", exc_info=True)
            try:
                await self._async_release_b(now, "error")
            except Exception:  # noqa: BLE001 - tried again at the next look
                _LOGGER.warning("SpotNav heat control: the raise contact could not be opened", exc_info=True)
        try:
            await self._async_renew(now)
            if (self.held and self._open_failures >= OPEN_TRIES) or (self.raised and self._b_open_failures >= OPEN_TRIES):
                # Openings that keep failing: on the SG Ready API the switch ends the block or the raise by itself.
                await self._async_fallback_off(now, "open_failed")
            await self._async_deactivate(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again
            _LOGGER.warning("SpotNav heat control: the path's upkeep failed", exc_info=True)
        self._trim(now)
        # Again after the writes, so the line says what holds now.
        self._safe_track_home()
        self._save()
        if not self._closed:
            self._watch_contacts()

    async def _async_look(self, now: datetime) -> None:
        config = self.shadow.config()
        relay = lower_entity(config)
        for entity_id in {relay, raise_entity(config), self.relay, self.raise_entity}:
            if entity_id is not None:
                self._seen_open(entity_id, now)
        if self.relay is not None and relay != self.relay:
            # The old relay is given back first: opened where SpotNav holds it, read back at the next looks.
            if self.pending is not None and self._read(self.relay) == self.pending.closed:
                self._confirm(self.pending, now)
            if self.held and (self._gone(self.relay) or self._open_failures >= OPEN_TRIES):
                # Removed, or it does not open: let go (logged) rather than stay stuck on it; the new relay is taken
                # up. Nothing more can be written to it.
                gone = self._gone(self.relay)
                _LOGGER.warning(
                    "SpotNav heat control: %s, held closed, %s; let go", self.relay, "is gone" if gone else "did not open"
                )
                self._log(now, "relay_let_go", relay=self.relay)
                self.pending = None
                await self._async_fallback_off(now, "let_go")
                self._end_lowering(now)
            if self.held:
                if self.pending is None or now - self.pending.at > READ_BACK:
                    await self._async_give_back(now, "relay_changed", unreadable_too=True)
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
            elif now - self.pending.at > READ_BACK and read is None:
                # Not seen because the relay does not read (its integration not up yet, a blip): no pause; what
                # SpotNav may hold is opened at the first readable look.
                self._log(now, "not_read_back", why=WAIT_RELAY_UNAVAILABLE)
                self.pending = None
                if self.held:
                    self._reopen = True
            elif now - self.pending.at > READ_BACK:
                own_reopen = self._own_reopen_at is not None and self.pending.at >= self._own_reopen_at
                if not self.pending.closed and own_reopen and read is True:
                    # SpotNav opened what it took for its own late closing once; still closed: someone else's.
                    self.pending = None
                    self._log(now, "foreign_close")
                    self._end_lowering(now)
                    self._pause(now, WAIT_FOREIGN_CHANGE)
                    self._set(STATE_WAITING, self.paused_reason)
                    return
                await self._async_failed(now, "not_read_back")
                return
            else:
                if not enabled:
                    self._set(STATE_OFF, None)
                return
        if not enabled or relay is None:
            await self._async_exit(now, "off")
            self._set(STATE_OFF if not enabled else STATE_WAITING, None if not enabled else WAIT_NO_RELAY)
            self._expect = None
            return

        if read is None:
            # Nothing is written to a relay that does not read, and no pause starts: it is opened at the first
            # readable look where SpotNav may hold it.
            self._set(STATE_WAITING, WAIT_RELAY_UNAVAILABLE)
            self.check.abort(WAIT_RELAY_UNAVAILABLE)
            if self.held:
                self._reopen = True
            return
        if self._reopen:
            # Held at the last stop, or while the relay did not read: opened before anything else (a relay found
            # open was reset meanwhile).
            if self.held and read is True:
                await self._async_exit(now, "restart")
                return
            self._end_lowering(now)
            self._reopen = False
        if not self.held and read is True and self._own_late(now):
            # SpotNav's own closing read late or bounced (its write failed, timed out or was just given up): its
            # own, opened once, never left closed.
            self._log(now, "own_late_close")
            self._own_reopen_at, self._foreign = now, None
            self.held, self.held_since = True, now
            self.lowerings.append([now, None])
            self._set(STATE_WAITING, self.paused_reason or WAIT_RELAY_CLOSED)
            await self._async_write(False, now, why="own_late_close")
            return
        # Never fight another writer, but only on a change that reads on two looks a minute apart.
        foreign = None
        if self.held and read is False:
            foreign = "open"
        elif not self.held and read is True and self._expect is False:
            foreign = "close"
        if foreign is None:
            self._foreign = None
        elif self._foreign is None or self._foreign[0] != foreign:
            self._foreign = (foreign, now)
            self._log(now, f"foreign_{foreign}_seen")
            # Not running while it is confirmed: no raise goes on beside a contact someone else just changed.
            self._set(STATE_WAITING, WAIT_RELAY_CLOSED if foreign == "close" else WAIT_FOREIGN_CHANGE)
            return
        elif now - self._foreign[1] < FOREIGN_CONFIRM:
            self._set(STATE_WAITING, WAIT_RELAY_CLOSED if foreign == "close" else WAIT_FOREIGN_CHANGE)
            return
        else:
            self._foreign = None
            self._log(now, f"foreign_{foreign}")
            if foreign == "open":
                self._end_lowering(now)
            self._pause(now, WAIT_FOREIGN_CHANGE)
        if self.paused_until is not None:
            await self._async_exit(now, self.paused_reason or "paused")
            self._expect = None
            self._set(STATE_WAITING, self.paused_reason)
            return
        if self.held and self.held_since is None:
            self.held_since = now
        blocker = self._blocker(config, relay)
        if blocker is None and not self.held and read is True:
            blocker = WAIT_RELAY_CLOSED
            if self.reason != WAIT_RELAY_CLOSED:
                self._log(now, "found_closed")
                _LOGGER.info("SpotNav heat control: %s is closed by something else; nothing is written", relay)
        if blocker is not None:
            await self._async_exit(now, blocker)
            self._expect = None
            self.check.abort(blocker)
            self._set(STATE_WAITING, blocker)
            return
        self._expect = read
        safety = self.safety = self._safety(config, now)

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
                lowered_24h=self.lowered_24h(now),
                writes_today=self.writes.get(self._day(now), 0),
                safety=safety,
                max_writes=paths.max_writes(relay),
            )
        )
        self._set(STATE_RUNNING, self.decision.reason)
        if self.decision.closed != self.held:
            await self._async_write(self.decision.closed, now, why=self.decision.reason)

    async def _async_verify(self, now: datetime, relay: str, read: bool, safety: str | None) -> None:
        if not self._b_clear(now) and self._b_open(now):
            # B reads open, only not for `DEAD_TIME` yet: the check waits a look.
            self._set(STATE_VERIFYING, self.check.phase)
            return
        if not self._b_clear(now):
            # The check closes A: B opens first (never (1,1)), and the check waits for it.
            self.check.abort(RAISE_INTERLOCK)
            await self._async_release_b(now, "check")
            self._set(STATE_VERIFYING, PHASE_WAIT)
            return
        sample = await self.async_sample(now)
        if not self.signal:
            self.check.abort(WAIT_NO_SIGNAL)
            await self._async_give_back(now, WAIT_NO_SIGNAL)
            self._set(STATE_WAITING, WAIT_NO_SIGNAL)
            return
        blocked = None if safety in (None, SAFETY_SETTLING) else safety
        room = self.writes.get(self._day(now), 0) + 2 <= paths.max_writes(relay)
        step = self.check.step(now, sample, read, blocked, measured=self._measured(), may_start=room)
        if step.outcome is not None:
            self._log(now, "check", outcome=step.outcome)
            _LOGGER.info("SpotNav heat control: the check of %s ended: %s", relay, step.outcome)
            if verdict(step.outcome) in (PASSED, FAILED):
                self.checks[relay] = {"result": verdict(step.outcome), "outcome": step.outcome, "at": now.isoformat()}
        self._set(STATE_VERIFYING, self.check.phase)
        if step.closed != self.held:
            await self._async_write(step.closed, now, why="check")

    # ---- reads

    def _read(self, key: str) -> bool | None:
        """A contact as read now: closed, open, or `None` where it does not read (`paths`: a switch's state, the SG
        Ready API's mode, an offset against what SpotNav wrote)."""
        entity_id, kind, role = paths.parse(key)
        state = self.hass.states.get(entity_id)
        if kind == paths.KIND_SWITCH:
            if state is None or state.state in ("unavailable", "unknown"):
                return None
            return state.state in CLOSED
        if state is None or state.state == "unavailable":
            return None
        if kind == paths.KIND_MODE:
            mode = paths.mode_of(state.state)
            return None if mode is None else paths.mode_closed(mode, role)
        return paths.offset_closed(paths.number_of(state.state), role, self.snapshots.get(key), self._volatile(key))

    def _gone(self, key: str) -> bool:
        """Whether a contact's entity is no longer in Home Assistant."""
        return self.hass.states.get(paths.entity_of(key)) is None

    def _volatile(self, key: str) -> bool:
        """Whether the controller forgets an offset's value by itself: as kept with SpotNav's move, else by what the
        entity is."""
        from .detect import offset_capability  # noqa: PLC0415 - read only when it matters

        snapshot = self.snapshots.get(key)
        if snapshot is not None:
            return snapshot.volatile
        known = offset_capability(self.hass, paths.entity_of(key))
        return known is not None and known[1]

    def _switch_state(self, entity_id: str) -> bool | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        return state.state == "on"

    def _forget(self, now: datetime) -> None:
        """Drop a kept offset move once it is over: nothing held or on its way on its contact, and its closing can
        no longer take late (`OWN_WINDOW`). From then on the value the number holds is the household's own."""
        for key in list(self.snapshots):
            if key == self.relay:
                busy, unseen = self.held or self.pending is not None, self._a_close_unseen_at
            elif key == self.raise_entity:
                busy, unseen = self.raised or self.pending_b is not None, self._b_close_unseen_at
            else:
                busy, unseen = False, None
            if busy or (unseen is not None and now - unseen <= OWN_WINDOW):
                continue
            del self.snapshots[key]
            self._renewed_at.pop(key, None)
            self._renew_ok.pop(key, None)

    def _prepare_close(self, key: str, now: datetime) -> str | None:
        """Before an offset's closing: read the value found, work out the value to write and keep both
        (`paths.Snapshot`, saved before the write goes out). The reason when no closing is possible, else `None`;
        nothing to prepare on the other paths."""
        from .detect import offset_capability  # noqa: PLC0415 - read only when it matters

        entity_id, kind, role = paths.parse(key)
        if kind != paths.KIND_OFFSET:
            return None
        state = self.hass.states.get(entity_id)
        known = offset_capability(self.hass, entity_id)
        volatile = known is not None and known[1]
        found = None if state is None else paths.number_of(state.state)
        if state is None or (found is None and not (volatile and state.state == "unknown")):
            return "offset_unreadable"

        def limit(name: str) -> float | None:
            value = state.attributes.get(name)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        coldest = self.coldest_outdoor(self.shadow.config(), now)
        target = paths.offset_target(
            role,
            found,
            paths.depth_of(None if known is None else known[0]),
            cold=coldest is not None and coldest < paths.COLD_BELOW_C,
            minimum=limit("min"),
            maximum=limit("max"),
            step=limit("step"),
            # A stored offset never moves further than `MAX_SHIFT` from the household's own value, whatever it reads.
            home=None if volatile else self.home_value(entity_id),
        )
        if target is None:
            return "offset_no_room"
        self.snapshots[key] = paths.Snapshot(found, target, volatile, now)
        self._renewed_at.pop(key, None)
        self._renew_ok.pop(key, None)
        return None

    # ---- the stored offset's own value

    def _stored_offset(self, config: HeatConfig) -> str | None:
        """The stored offset number the settings steer; `None` on any other path and for a volatile offset."""
        key = lower_entity(config)
        if key is None or paths.parse(key)[1] != paths.KIND_OFFSET or self._volatile(key):
            return None
        return paths.entity_of(key)

    def home_value(self, entity_id: str) -> float | None:
        """The household's own value of a stored offset, as kept in the site's entry (`CONF_HEAT_OFFSET_HOME`)."""
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        kept = None if entry is None else entry.data.get(CONF_HEAT_OFFSET_HOME)
        value = kept.get(entity_id) if isinstance(kept, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return float(value)

    def _set_home(self, entity_id: str, value: float) -> None:
        entry = self.hass.config_entries.async_get_entry(self.site.entry_id)
        if entry is None:
            return
        _LOGGER.info("SpotNav heat control: %s's own value is now %s", entity_id, value)
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HEAT_OFFSET_HOME: {entity_id: value}})

    def _offset_busy(self, entity_id: str) -> bool:
        """Whether SpotNav holds a move on the offset, writes one, or may still own one (a kept move, a reopening)."""
        for key, held, pending, reopen in (
            (self.relay, self.held, self.pending, self._reopen),
            (self.raise_entity, self.raised, self.pending_b, self._reopen_b),
        ):
            if key is not None and paths.entity_of(key) == entity_id and (held or pending is not None or reopen):
                return True
        return any(paths.entity_of(key) == entity_id for key in self.snapshots)

    def _safe_track_home(self) -> None:
        try:
            self._track_home(self.shadow.config())
        except Exception:  # noqa: BLE001 - the bound falls back to the plan's -3..+3
            _LOGGER.warning("SpotNav heat control: the offset's own value could not be kept", exc_info=True)

    def _track_home(self, config: HeatConfig) -> None:
        """Keep the stored offset's own value (the household's, in the site's entry, so a lost or old store still
        knows it): taken when the path is first seen, and again only when the offset changes between two readings
        while SpotNav holds nothing on it. A value that differs from it without such a change (after a lost store
        left a lowering standing, or a change while Home Assistant was down) is never taken as the household's and
        never written back by itself: the card's line asks (`offset_home`), and clears once the two agree."""
        entity_id = self._stored_offset(config)
        if entity_id is None:
            self.offset_home = None
            self._offset_seen.clear()
            return
        if self._offset_busy(entity_id):
            # What it reads now is SpotNav's (or may be): no change of the household's can be told from it.
            self.offset_home = None
            self._offset_seen.pop(entity_id, None)
            return
        state = self.hass.states.get(entity_id)
        value = None if state is None or state.state == "unavailable" else paths.number_of(state.state)
        if value is None:
            return
        seen = self._offset_seen.get(entity_id)
        self._offset_seen = {entity_id: value}
        home = self.home_value(entity_id)
        if home is None or (seen is not None and not paths.same(seen, value)):
            if home is None or not paths.same(home, value):
                self._set_home(entity_id, value)
            self.offset_home = None
            return
        self.offset_home = None if paths.same(value, home) else {"entity_id": entity_id, "value": value, "home": home}

    async def async_offset_home(self, choice: str) -> str | None:
        """The person's answer to the offset's line: `restore` writes the household's own value back, `keep` takes
        the value the offset reads as the household's. An error code, `None` when done."""
        if choice not in ("restore", "keep"):
            return "invalid_choice"
        line = self.offset_home
        if line is None:
            return "nothing_to_do"
        entity_id = line["entity_id"]
        if self._offset_busy(entity_id):
            return "busy"
        if choice == "keep":
            self._set_home(entity_id, line["value"])
            self._offset_seen[entity_id] = line["value"]
            self.offset_home = None
            return None
        now = dt_util.utcnow()
        day = self._day(now)
        self.writes[day] = self.writes.get(day, 0) + 1
        self._log(now, "offset_home_restore", entity=entity_id, value=line["home"])
        self.offset_moves.append((now, 1 if line["home"] > line["value"] else -1))
        try:
            await self._async_set_number(entity_id, line["home"])
        except Exception as err:  # noqa: BLE001 - the line stays; the person may try again
            _LOGGER.warning("SpotNav heat control: putting %s back failed: %s", entity_id, err)
            return "write_failed"
        finally:
            self._save()
        # Read again at the next look: the line clears once the offset reads its own value.
        self._offset_seen.pop(entity_id, None)
        self._track_home(self.shadow.config())
        return None

    def _blocker(self, config: HeatConfig, relay: str) -> str | None:
        from .detect import cloud_entity, other_controllers, read_heat_pumps, sg_ready_api_fits  # noqa: PLC0415 - read only when it matters

        watched = list(dict.fromkeys([paths.entity_of(relay)] + ([config.control_entity_b] if config.control_entity_b else [])))
        if any(cloud_entity(self.hass, entity_id) for entity_id in watched):
            # An entity behind the maker's cloud is read only (plan section 4), on every path: nothing to steer.
            return WAIT_NO_RELAY
        if paths.parse(relay)[1] == paths.KIND_MODE and not (
            sg_ready_api_fits(self.hass, paths.entity_of(relay), "number")
            and config.control_entity_b is not None
            and sg_ready_api_fits(self.hass, config.control_entity_b, "switch")
        ):
            # The SG Ready API writes 0..2 to its mode: never to a number that is not the pump's own SG Ready mode.
            return WAIT_NO_RELAY
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
        """The outdoor temperature now: the chosen sensor (a frozen one does not read), else the site's."""
        from .runtime import temperature_c  # noqa: PLC0415 - the runtime imports this module

        if config.outdoor_sensor is None:
            return self.site.house_forecast.outdoor_c()
        return temperature_c(self.hass, config.outdoor_sensor)

    def coldest_outdoor(self, config: HeatConfig, now: datetime) -> float | None:
        """The lowest of the outdoor readings now: the chosen sensor, the site's (the pump's sensor or the weather),
        and the weather's forecast for the running hour. What the cold limit is compared with."""
        forecast = self.site.house_forecast
        hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        readings = [self.outdoor_now(config), forecast.outdoor_c(), forecast.temperatures().get(hour)]
        values = [value for value in readings if value is not None]
        return min(values) if values else None

    def _safety(self, config: HeatConfig, now: datetime) -> str | None:
        """The safety reason now (see `relay_control`), `settling` while good data is younger than `GOOD_FOR`."""
        outdoor = self.outdoor_now(config)
        made = self.shadow.made_at
        if outdoor is None or made is None or now - made > FRESH_PLAN or not self.shadow.plan:
            self._good_since = None
            return SAFETY_STALE
        if self._good_since is None:
            self._good_since = now
        coldest_out = self.coldest_outdoor(config, now)
        if coldest_out is not None and coldest_out <= NO_LOWER_AT_C:
            return SAFETY_TOO_COLD
        coldest = self.shadow.coldest_now(config)
        if coldest is None and config.indoor_sensors:
            # Rooms chosen but none reads (or none plausibly): not known to be warm enough.
            self._good_since = None
            return SAFETY_STALE
        if coldest is not None and coldest < config.target_c - BAND_C:
            return SAFETY_ROOM_COLD
        if paths.blocks_hot_water(config) and self._boost_on(config):
            # A block may stop hot water too: a boost someone started is never cut off, and no block starts on it.
            return SAFETY_HOT_WATER
        b = raise_entity(config)
        if b is not None:
            # B closed by SpotNav's own raise is no safety reason: the interlock opens it before A closes.
            own = b == self.raise_entity and (self.raised or self.pending_b is not None)
            if self._read(b) is not False and not own:
                return SAFETY_SG_READY_B
        if now - self._good_since < GOOD_FOR:
            return SAFETY_SETTLING
        return None

    def _boost_on(self, config: HeatConfig) -> bool:
        """Whether a hot-water boost runs: the settings' hot-water entity, or the pump's own boost (its adapter's
        `hot_water_boost` entities), reads on (`paths.boost_on`)."""
        from .detect import read_heat_pumps  # noqa: PLC0415 - read only when it matters

        entities = [config.hot_water_entity] if config.hot_water_entity else []
        for pump in read_heat_pumps(self.hass):
            entities.extend(item.entity_id for item in pump.capabilities if item.kind == "hot_water_boost")
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is not None and paths.boost_on(entity_id, state.state):
                return True
        return False

    async def async_sample(self, now: datetime) -> Sample:
        """The check's readings now: the meter's power and what the pump does by it, the calculated supply."""
        from ..site.heat_meter import KIND_POWER, async_heat_hours  # noqa: PLC0415 - the site imports this module
        from .detect import read_heat_pumps  # noqa: PLC0415
        from .runtime import fresh_state  # noqa: PLC0415

        meter = self.site.house_forecast.heat.meter
        kw = None
        power_meter = meter.found and meter.kind == KIND_POWER and meter.entity_id is not None
        if power_meter and meter.entity_id is not None:
            state = fresh_state(self.hass, meter.entity_id)
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
        state_now_ = state_now(kw, self._levels, self._power_mean(now, kw)) if kw is not None else None
        return Sample(kw if state_now_ is not None else None, state_now_, supply)

    def _power_mean(self, now: datetime, kw: float) -> float | None:
        """The meter's mean over the last `POWER_MEAN` (with `kw` now), `None` before `POWER_MEAN_SEEN` of it."""
        self._power.append((now, kw))
        while self._power and now - self._power[0][0] > POWER_MEAN:
            self._power.popleft()
        if now - self._power[0][0] < POWER_MEAN_SEEN:
            return None
        return sum(value for _moment, value in self._power) / len(self._power)

    def _measured(self) -> bool:
        """Whether closings already measured on the meter show the relay's effect (see `MEASURED_BLOCKS`)."""
        blocks = getattr(self.shadow, "blocks", None) or []
        return sum(1 for block in blocks if (share := block.share()) is not None and share >= MEASURED_SHARE) >= MEASURED_BLOCKS

    # ---- writes

    async def _async_call(self, key: str, closed: bool) -> None:
        """Write a contact (`paths`): a switch on or off; the SG Ready API's mode (blocked or the raise for a
        closing, with its activation switch on first; normal for every opening, never the forced raise); an offset
        as kept (`_prepare_close`): the value written for a closing, the value found, or the release, for an
        opening."""
        entity_id, kind, role = paths.parse(key)
        if kind == paths.KIND_SWITCH:
            await self._async_switch(entity_id, closed)
            return
        if kind == paths.KIND_MODE:
            if closed:
                await self._async_activate(key, dt_util.utcnow())
            mode = paths.mode_to_write(role, closed)
            assert mode != paths.MODE_FORCED
            await self._async_set_number(entity_id, float(mode))
            return
        snapshot = self.snapshots.get(key)
        if closed:
            value = None if snapshot is None else snapshot.written
        elif snapshot is not None:
            value = snapshot.restore()
        else:
            # Nothing kept: a volatile value is let go; of a stored one SpotNav knows nothing to put back.
            value = paths.RELEASE if self._volatile(key) else None
        if value is None:
            raise RuntimeError(f"no value kept to write to {entity_id}")
        await self._async_set_number(entity_id, value)

    async def _async_switch(self, entity_id: str, on: bool) -> None:
        domain = entity_id.split(".", 1)[0]
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self.hass.services.async_call(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True)

    async def _async_set_number(self, entity_id: str, value: float) -> None:
        domain = entity_id.split(".", 1)[0]
        async with asyncio.timeout(CALL_TIMEOUT_S):
            await self.hass.services.async_call(domain, "set_value", {"entity_id": entity_id, "value": value}, blocking=True)

    async def _async_activate(self, key: str, now: datetime) -> None:
        """The SG Ready API's activation switch on before a mode is written: left alone where it is on already (then
        it is not SpotNav's to turn off); else kept as SpotNav's, turned on and read back. Raises where it cannot be
        (the closing is then a write not taken)."""
        switch = paths.activation_switch(self.shadow.config())
        if switch is None:
            raise RuntimeError("the SG Ready API has no activation switch chosen")
        state = self._switch_state(switch)
        if state is True:
            return
        if state is None:
            raise RuntimeError(f"{switch} does not read")
        day = self._day(now)
        if self.activations.get(day, 0) >= paths.ACTIVATION_WRITES_DAY:
            raise RuntimeError(f"{switch} was turned on {paths.ACTIVATION_WRITES_DAY} times today already")
        self.activations[day] = self.activations.get(day, 0) + 1
        self.activation = {"entity": switch, "mode": paths.entity_of(key)}
        self._deactivate_at = None
        if not await self._async_save_now():
            self.activation = None
            raise RuntimeError("the activation could not be kept")
        self._log(now, "activate", entity=switch)
        await self._async_switch(switch, True)
        async with asyncio.timeout(ACTIVATE_WAIT_S):
            while self._switch_state(switch) is not True:
                await asyncio.sleep(0.2)

    async def _async_deactivate(self, now: datetime, *, stopping: bool = False) -> None:
        """The SG Ready API handed back: the activation switch SpotNav turned on is turned off once control no
        longer runs on that path, nothing is held or on its way and the mode reads normal (mode first, then the
        switch). A mode that is not normal while SpotNav holds nothing is someone else's: left as it is."""
        held = self.activation
        if held is None:
            return
        switch = held["entity"]
        config = self.shadow.config()
        chosen = paths.activation_switch(config) == switch
        running = self.state in (STATE_RUNNING, STATE_VERIFYING) and chosen and not stopping
        if running or self.held or self.raised or self.pending is not None or self.pending_b is not None:
            self._idle_since = None
            return
        if self._idle_since is None:
            self._idle_since = now
        if chosen and config.control_enabled and not stopping and now - self._idle_since < RETRY_OPEN:
            # A short wait (a reading gone for a moment, a change being confirmed) is no hand-back yet.
            return
        state = self._switch_state(switch)
        if state is False or self.hass.states.get(switch) is None:
            # Off already, or gone: no longer SpotNav's.
            self.activation, self._deactivate_at = None, None
            return
        mode = self.hass.states.get(held["mode"])
        if state is None or mode is None or paths.mode_of(mode.state) != paths.MODE_NORMAL:
            return
        if self._deactivate_at is not None and now < self._deactivate_at:
            return
        self._deactivate_at = now + RETRY_OPEN
        self._log(now, "deactivate", entity=switch)
        await self._async_switch(switch, False)
        if stopping:
            try:
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    while self._switch_state(switch) is True:
                        await asyncio.sleep(0.2)
            except TimeoutError:
                return
        if self._switch_state(switch) is False:
            self.activation, self._deactivate_at = None, None

    async def _async_fallback_off(self, now: datetime, why: str, *, stopping: bool = False) -> None:
        """The SG Ready API's second way back: where SpotNav turned the activation switch on and the mode does not read
        normal (a stop that could not wait for it, openings that keep failing, a contact let go), the switch is
        turned off, which ends the block or the raise by itself through another register. The mode still reads as
        SpotNav's closing and is put back to normal at a later look or the next start. Never raises."""
        held = self.activation
        if held is None or self._switch_state(held["entity"]) is not True:
            return
        mode = self.hass.states.get(held["mode"])
        if mode is not None and paths.mode_of(mode.state) == paths.MODE_NORMAL:
            return
        switch = held["entity"]
        self._log(now, "deactivate_fallback", entity=switch, why=why)
        _LOGGER.warning("SpotNav heat control: the SG Ready mode did not go back to normal (%s); turning %s off", why, switch)
        try:
            await self._async_switch(switch, False)
            if stopping:
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    while self._switch_state(switch) is True:
                        await asyncio.sleep(0.2)
        except Exception:  # noqa: BLE001 - tried again at the next look, or the next start
            _LOGGER.warning("SpotNav heat control: %s could not be turned off either", switch, exc_info=True)
            return
        if self._switch_state(switch) is False:
            self.activation, self._deactivate_at = None, None

    async def _async_renew(self, now: datetime) -> None:
        """The renewal of a volatile offset: the value SpotNav holds is written again every `paths.RENEW`, well
        inside the controller's five minutes. A renewal that has failed for `paths.EXPIRY` (the controller has
        forgotten the value by then), or a day's `paths.RENEWALS_DAY` reached, ends the lowering as a write not
        taken (released, control paused for the day)."""
        for key, held, pending, failed in (
            (self.relay, self.held, self.pending, self._async_failed),
            (self.raise_entity, self.raised, self.pending_b, self._async_failed_b),
        ):
            snapshot = None if key is None else self.snapshots.get(key)
            if key is None or snapshot is None or not snapshot.volatile or not held or pending is not None:
                continue
            if self._read(key) is not True or now - self._renewed_at.get(key, snapshot.at) < paths.RENEW:
                continue
            day = self._day(now)
            if self.renewals.get(day, 0) >= paths.RENEWALS_DAY:
                await failed(now, "renew_cap")
                continue
            self.renewals[day] = self.renewals.get(day, 0) + 1
            try:
                await self._async_set_number(paths.entity_of(key), snapshot.written)
            except Exception as err:  # noqa: BLE001 - tried again at the next look, until the value has expired
                _LOGGER.warning("SpotNav heat control: renewing %s failed: %s", paths.entity_of(key), err)
                if now - self._renew_ok.get(key, snapshot.at) >= paths.EXPIRY:
                    await failed(now, "renew_failed")
                continue
            self._renewed_at[key] = self._renew_ok[key] = now

    async def _async_write(self, closed: bool, now: datetime, *, why: str) -> None:
        """Switch the relay and wait for the read-back at the next looks. An opening is never refused."""
        relay = self.relay
        if relay is None:
            return
        if closed and not self._b_clear(now):
            # Never (1,1): B opens first, and A closes only once B reads open (in this look where it reads back at
            # once, else at a later one).
            await self._async_release_b(now, RAISE_INTERLOCK)
            if not self._b_clear(now):
                self._log_wait(now, "close_waits_for_b", why)
                return
        if not closed and self._retry_open_at is not None and now < self._retry_open_at:
            return
        if closed and self._spaced(relay, closed, now):
            # A stored offset moved within the write spacing: the closing waits for a later look.
            self._log_wait(now, "close_waits_for_spacing", why)
            return
        if closed and (blocked := self._prepare_close(relay, now)) is not None:
            # An offset that does not read, or has no room left to move: nothing is written.
            self._log_wait(now, "close_not_possible", blocked)
            return
        self.pending = Pending(closed, now)
        day = self._day(now)
        self.writes[day] = self.writes.get(day, 0) + 1
        if closed:
            self.held, self.held_since = True, now
            self.lowerings.append([now, None])
            # Kept before the closing goes out: a crash right after it still knows SpotNav holds A (opened at the
            # next start, an offset put back to the value found); B is read again after the wait.
            if not await self._async_save_now() or not self._b_clear(now):
                self.pending = None
                self.writes[day] -= 1
                self.held, self.held_since = False, None
                self.lowerings.pop()
                self.snapshots.pop(relay, None)
                self._log(now, "close_not_written", why=why)
                self._save()
                return
        self._waits.pop("close_waits_for_b", None)
        self._waits.pop("close_waits_for_spacing", None)
        if closed:
            self._own_reopen_at = None
            self._a_close_unseen_at = now
        self._log(now, "close" if closed else "open", why=why)
        self._moved(relay, closed, now)
        self._save()
        try:
            await self._async_call(relay, closed)
        except Exception as err:  # noqa: BLE001 - any error is a write not taken
            _LOGGER.warning("SpotNav heat control: switching %s %s failed: %s", relay, "on" if closed else "off", err)
            await self._async_failed(now, "write_failed")
            return
        if self._read(relay) == closed:
            self._confirm(self.pending, now)

    def _is_stored_offset(self, key: str) -> bool:
        return paths.parse(key)[1] == paths.KIND_OFFSET and not self._volatile(key)

    def _spaced(self, key: str, closed: bool, now: datetime) -> bool:
        """Whether a closing of a stored offset waits for the plan's write spacing (`paths.move_waits`). An opening
        never waits: a hand-back (a room gone cold, any safety reason or exit) goes at once."""
        if not closed or not self._is_stored_offset(key):
            return False
        return paths.move_waits(self.offset_moves, paths.move_direction(paths.parse(key)[2], closed), now)

    def _moved(self, key: str, closed: bool, now: datetime) -> None:
        """Keep a stored offset's write for the spacing (before it goes out: a write that fails may still take)."""
        if self._is_stored_offset(key):
            self.offset_moves.append((now, paths.move_direction(paths.parse(key)[2], closed)))

    def _own_late(self, now: datetime) -> bool:
        """Whether a closed reading now is SpotNav's own closing read late (see `OWN_WINDOW`), not opened yet."""
        return (
            self._own_reopen_at is None
            and self._a_close_unseen_at is not None
            and now - self._a_close_unseen_at <= OWN_WINDOW
        )

    def _confirm(self, pending: Pending, now: datetime) -> None:
        self.pending = None
        self._expect = pending.closed
        if pending.closed:
            self._a_close_unseen_at = None
        if not pending.closed:
            self._end_lowering(pending.at)
            self._retry_open_at = None
            self._open_failures = 0
            if self.relay is not None:
                self._open_since[self.relay] = now

    def _end_lowering(self, at: datetime) -> None:
        if self.held:
            self.last_end = at
        self._open_failures = 0
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
            self._open_failures += 1
            self._retry_open_at = now + RETRY_OPEN
        elif self.held and self.relay is not None:
            # A closing not seen may still take: opened all the same (the fail-safe); one that reads open is owed
            # nothing more.
            await self._async_write(False, now, why=why)
            if self.held and self._read(self.relay) is False:
                self.pending, self._retry_open_at = None, None
                self._end_lowering(now)

    async def _async_exit(self, now: datetime, why: str) -> None:
        """An exit: B opened first (back to (0,0) through (1,0) at worst, never through (1,1)), then A given back. A B
        that reads open with nothing on its way is left to the raise's look (no write)."""
        b = self.raise_entity
        try:
            if b is not None and (self.pending_b is not None or self._read(b) is not False):
                await self._async_release_b(now, why)
        finally:
            await self._async_give_back(now, why)

    async def _async_give_back(self, now: datetime, why: str, *, unreadable_too: bool = False) -> None:
        """Open the relay SpotNav holds (no write otherwise), and end a running check. A write not read back within
        `READ_BACK` is given up: the opening is written all the same. A relay that does not read is not written
        (opened at its first readable look) unless `unreadable_too` (one no longer chosen)."""
        self.check.abort(why)
        if not unreadable_too and self.held and self.relay is not None and self._read(self.relay) is None:
            self._reopen = True
            return
        if self.pending is not None and now - self.pending.at > READ_BACK:
            if not self.pending.closed:
                self._open_failures += 1
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

    # ---- SG Ready's raise (contact B)

    def _a_busy(self) -> bool:
        """Whether A is anything but open and left alone: SpotNav holds it, a write is on its way, or it does not read
        open. B never stays closed beside it."""
        return self.relay is None or self.held or self.pending is not None or self._read(self.relay) is not False

    def _seen_open(self, entity_id: str, now: datetime) -> datetime | None:
        """Since when `entity_id` has read open without a break (from now if this is the first such reading), `None`
        while it does not read open."""
        if self._read(entity_id) is not False:
            self._open_since.pop(entity_id, None)
            return None
        return self._open_since.setdefault(entity_id, now)

    def _open_long_enough(self, entity_id: str, now: datetime) -> bool:
        since = self._seen_open(entity_id, now)
        return since is not None and now - since >= DEAD_TIME

    def _a_clear(self, now: datetime) -> bool:
        """Whether B may close: A has read open for `DEAD_TIME`, SpotNav does not hold it, nothing is pending on it,
        and no closing of A that never read closed was written within `OWN_WINDOW` (it could still take late)."""
        if self._a_close_unseen_at is not None:
            if now - self._a_close_unseen_at <= OWN_WINDOW:
                return False
            self._a_close_unseen_at = None
        return self.relay is not None and not self.held and self.pending is None and self._open_long_enough(self.relay, now)

    def _b_clear(self, now: datetime) -> bool:
        """Whether A may close: B is open (`_b_open`) and has read open for `DEAD_TIME`."""
        if not self._b_open(now):
            return False
        b = raise_entity(self.shadow.config())
        return b is None or self._open_long_enough(b, now)

    def _b_open(self, now: datetime) -> bool:
        """SpotNav holds no B and nothing is pending on it, the chosen B (if any) reads open, and no closing of B that
        never read closed was written within `OWN_WINDOW` (it could still take late)."""
        if self.raised or self.pending_b is not None:
            return False
        if self._b_close_unseen_at is not None:
            if now - self._b_close_unseen_at <= OWN_WINDOW:
                return False
            self._b_close_unseen_at = None
        b = raise_entity(self.shadow.config())
        return b is None or self._read(b) is False

    async def _async_raise_look(self, now: datetime) -> None:
        """B's look, after A's (see the module, 7)."""
        config = self.shadow.config()
        b = raise_entity(config)
        if self.raise_entity is not None and b != self.raise_entity:
            # The old B is given back first, as the old A is.
            old = self.raise_entity
            if self.pending_b is not None and self._read(old) == self.pending_b.closed:
                self._confirm_b(self.pending_b, now)
            if self.raised and (self._gone(old) or self._b_open_failures >= OPEN_TRIES):
                _LOGGER.warning("SpotNav heat control: the raise contact %s, held closed, could not be opened; let go", old)
                self._log(now, "raise_let_go", relay=old)
                self.pending_b = None
                await self._async_fallback_off(now, "let_go")
                self._end_raise(now)
            if self.raised:
                if self.pending_b is None or now - self.pending_b.at > READ_BACK or self.pending_b.closed:
                    if self.pending_b is not None and not self.pending_b.closed:
                        self._b_open_failures += 1
                    self.pending_b = None
                    await self._async_write_b(False, now, why="relay_changed")
                if self.raised:
                    self.raise_decision = Decision(False, "relay_changed")
                    return
            self.pending_b = None
        if b != self.raise_entity:
            self.raise_entity = b
            self.raise_check = RaiseCheck()
            self._recent.clear()
        enabled = config.control_enabled
        if enabled and self._was_enabled_raise is False:
            # Turned on again: a raise check that saw nothing, and raising set aside, are tried afresh.
            if self.raise_check.result == FAILED:
                self.raise_check = RaiseCheck()
            self.raise_paused_until = self.raise_paused_reason = None
        self._was_enabled_raise = enabled
        if b is None:
            self.raise_decision = None
            return
        read = self._read(b)
        if self.pending_b is not None:
            if read == self.pending_b.closed:
                self._confirm_b(self.pending_b, now)
            elif now - self.pending_b.at > READ_BACK and read is None:
                self._log(now, "raise_not_read_back", why=WAIT_RELAY_UNAVAILABLE)
                self.pending_b = None
                if self.raised:
                    self._reopen_b = True
            elif now - self.pending_b.at > READ_BACK:
                await self._async_failed_b(now, "not_read_back")
                return
            elif not self.pending_b.closed:
                # An opening on its way: read back at the next looks.
                return
            # A closing on its way goes on below: an exit opens it at once (opening is always safe).
        if read is None:
            # Nothing is written to a B that does not read: opened at its first readable look.
            if self.raised:
                self._reopen_b = True
            self.raise_check.abort(WAIT_RELAY_UNAVAILABLE)
            self.raise_decision = Decision(False, WAIT_RELAY_UNAVAILABLE)
            return
        if self._reopen_b:
            if self.raised and read is True:
                await self._async_write_b(False, now, why="restart")
                self.raise_decision = Decision(False, "restart")
                return
            self._end_raise(now)
            self._reopen_b = False
        # Never fight another writer: B opened by someone else while SpotNav held it (read on looks a minute apart).
        if self.raised and self.pending_b is None and read is False:
            if self._b_foreign is None:
                self._b_foreign = now
                self._log(now, "foreign_open_b_seen")
                return
            if now - self._b_foreign < FOREIGN_CONFIRM:
                return
            self._b_foreign = None
            self._log(now, "foreign_open_b")
            self._end_raise(now)
            self._pause_raise(now, WAIT_FOREIGN_CHANGE)
        else:
            self._b_foreign = None
        if self.raised and self._a_busy():
            # A reads closed (by hand, a script, another writer), is held or has a write on its way while SpotNav holds
            # B: B opens at once, never (1,1) (an opening is always safe).
            self.raise_decision = Decision(False, RAISE_INTERLOCK)
            await self._async_release_b(now, RAISE_INTERLOCK)
            return
        if not self.raised and self.pending_b is None and read is True:
            unseen = self._b_close_unseen_at
            if self._b_own_reopen_at is None and unseen is not None and now - unseen <= OWN_WINDOW:
                # SpotNav's own closing read late or bounced: its own, opened once.
                self._log(now, "own_late_close_b")
                self._b_own_reopen_at = now
                self.raised, self.raised_since = True, now
                self.raisings.append([now, None])
                await self._async_write_b(False, now, why="own_late_close")
                self.raise_decision = Decision(False, "own_late_close")
                return
            # Closed by someone else: never taken over (A's safety reason `sg_ready_b` keeps A open).
            self.raise_decision = Decision(False, SAFETY_SG_READY_B)
            return
        decision = self._raise_decision(config, now)
        self.raise_decision = decision
        if decision.closed and not self.raised:
            if not self._a_clear(now):
                # A is closed, held or pending: B waits (lowering wins; never (1,1)).
                self.raise_decision = Decision(False, RAISE_INTERLOCK)
            else:
                await self._async_write_b(True, now, why=decision.reason)
        elif not decision.closed and self.raised:
            await self._async_write_b(False, now, why=decision.reason)
        await self._async_raise_watch(now)

    def _raise_decision(self, config: HeatConfig, now: datetime) -> Decision:
        if not config.control_enabled or self.state != STATE_RUNNING:
            return Decision(False, RAISE_OFF)
        return decide_raise(
            RaiseSituation(
                now=now,
                plan_until=planned_raise_end(self.shadow.plan, now),
                held_since=(self.raised_since or now) if self.raised else None,
                last_end=self.raise_last_end,
                raised_24h=self.raised_24h(now),
                writes_today=self.writes.get(self._day(now), 0),
                block=self._raise_block(config, now),
                max_writes=paths.max_writes(self.relay),
            )
        )

    def _raise_block(self, config: HeatConfig, now: datetime) -> str | None:
        """A reason that ends any raise and starts none (`relay_control.RAISE_*`), `None` when raising may go on."""
        inputs = {paths.entity_of(key) for key in (self.relay, self.raise_entity) if key is not None}
        if (switch := paths.activation_switch(config)) is not None:
            inputs.add(switch)
        for entity_id in sorted(inputs):
            state = self.hass.states.get(entity_id)
            if state is not None and state.attributes.get("assumed_state") is True:
                # Its state only says what was last sent, never that it opened: the interlock cannot be trusted.
                return RAISE_ASSUMED_STATE
        if self.raise_check.result == FAILED:
            return RAISE_NOT_OBSERVED
        if self.raise_paused_until is not None:
            if now < self.raise_paused_until:
                return self.raise_paused_reason
            self.raise_paused_until = self.raise_paused_reason = None
        if self.safety == SAFETY_STALE:
            return RAISE_STALE
        if self.shadow.room_warm.active or self._room_at_high_edge(config):
            return RAISE_WARM
        away_end = config.away_end()
        returning = away_end is not None and away_end - timedelta(hours=RETURN_HOURS) <= now < away_end
        if away_end is not None and now < away_end - timedelta(hours=RETURN_HOURS):
            return RAISE_AWAY
        if config.night.on and not returning:
            local = now.astimezone(time_zones.require_zone(self.shadow.tz))
            if config.night.covers(local.hour * 60 + local.minute):
                return RAISE_NIGHT
        if self.safety == SAFETY_SETTLING:
            return RAISE_SETTLING
        return None

    def _room_at_high_edge(self, config: HeatConfig) -> bool:
        """Whether a counted room (its offset counted) reads at or above the band's high edge: no pre-heat into a warm
        house, and a raise ends there."""
        warmest = self.shadow.warmest_now(config)
        return warmest is not None and warmest >= config.target_c + BAND_C

    async def _async_raise_watch(self, now: datetime) -> None:
        """The raise check's readings: the baseline while B is open, a step while the raise holds."""
        if self.raise_check.result is not None or self.state != STATE_RUNNING:
            return
        sample = await self.async_sample(now)
        if self.raised and self.pending_b is None:
            outcome = self.raise_check.step(now, sample)
            if outcome is not None:
                self._log(now, "raise_check", outcome=outcome)
                _LOGGER.info("SpotNav heat control: the raise check of %s ended: %s", self.raise_entity, outcome)
            return
        if not self.raised:
            self._recent.append((now, sample))
            while self._recent and now - self._recent[0][0] > BASELINE:
                self._recent.popleft()

    async def _async_write_b(self, closed: bool, now: datetime, *, why: str) -> None:
        """Switch B and read it back. A closing only while A is clear (`_a_clear`); an opening is never refused."""
        b = self.raise_entity
        if b is None:
            return
        if closed and not self._a_clear(now):
            self._log_wait(now, "raise_waits_for_a", why)
            return
        if not closed and self._b_retry_open_at is not None and now < self._b_retry_open_at:
            return
        if closed and self._spaced(b, closed, now):
            self._log_wait(now, "raise_waits_for_spacing", why)
            return
        if closed and (blocked := self._prepare_close(b, now)) is not None:
            self._log_wait(now, "raise_not_possible", blocked)
            return
        self.pending_b = Pending(closed, now)
        day = self._day(now)
        self.writes[day] = self.writes.get(day, 0) + 1
        if closed:
            self.raised, self.raised_since = True, now
            self.raisings.append([now, None])
            # Kept before the closing goes out (see `_async_write`); A is read again after the wait.
            if not await self._async_save_now() or not self._a_clear(now):
                self.pending_b = None
                self.writes[day] -= 1
                self.raised, self.raised_since = False, None
                self.raisings.pop()
                self.snapshots.pop(b, None)
                self._log(now, "raise_not_written", why=why)
                self._save()
                return
        self._waits.pop("raise_waits_for_a", None)
        self._waits.pop("raise_waits_for_spacing", None)
        if closed:
            self._b_own_reopen_at = None
            self._b_close_unseen_at = now
        self._log(now, "raise" if closed else "raise_end", why=why)
        self._moved(b, closed, now)
        self._save()
        try:
            await self._async_call(b, closed)
        except Exception as err:  # noqa: BLE001 - any error is a write not taken
            _LOGGER.warning("SpotNav heat control: switching %s %s failed: %s", b, "on" if closed else "off", err)
            await self._async_failed_b(now, "write_failed")
            return
        if self.pending_b is not None and self._read(b) == closed:
            self._confirm_b(self.pending_b, now)

    def _confirm_b(self, pending: Pending, now: datetime) -> None:
        self.pending_b = None
        if pending.closed:
            self._b_close_unseen_at = None
            self.raise_check.begin(pending.at, [sample for _moment, sample in self._recent])
        else:
            self._end_raise(pending.at)
            self._b_retry_open_at = None
            self._b_open_failures = 0
            if self.raise_entity is not None:
                self._open_since[self.raise_entity] = now

    def _end_raise(self, at: datetime) -> None:
        if self.raised:
            self.raise_last_end = at
        self.raised, self.raised_since = False, None
        self._b_open_failures = 0
        for item in self.raisings:
            if item[1] is None:
                item[1] = at
        outcome = self.raise_check.abort("ended")
        if outcome is not None:
            self._log(at, "raise_check", outcome=outcome)
        self._recent.clear()

    async def _async_failed_b(self, now: datetime, why: str) -> None:
        """A B write that raised or was not read back: raising is set aside for the day (lowering goes on); an opening
        is tried again while SpotNav may hold B, a closing not seen is opened all the same."""
        pending = self.pending_b
        self.pending_b = None
        self._log(now, "raise_failed", why=why)
        self._pause_raise(now, WAIT_NO_READ_BACK)
        if pending is not None and not pending.closed:
            self._b_open_failures += 1
            self._b_retry_open_at = now + RETRY_OPEN
        elif self.raised and self.raise_entity is not None:
            await self._async_write_b(False, now, why=why)
            if self.raised and self._read(self.raise_entity) is False:
                self.pending_b, self._b_retry_open_at = None, None
                self._end_raise(now)

    async def _async_release_b(self, now: datetime, why: str) -> None:
        """Open B where SpotNav holds it (no write otherwise). A B that does not read is opened at its first
        readable look; a closing still on its way is opened at once."""
        if not self.raised or self.raise_entity is None:
            return
        if self._read(self.raise_entity) is None:
            self._reopen_b = True
            return
        if self.pending_b is not None and not self.pending_b.closed:
            if now - self.pending_b.at <= READ_BACK:
                return
            self._b_open_failures += 1
            self.pending_b = None
        await self._async_write_b(False, now, why=why)

    def _pause_raise(self, now: datetime, reason: str) -> None:
        if self.raise_paused_until is not None:
            return
        zone = time_zones.require_zone(self.shadow.tz)
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        self.raise_paused_until = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone).astimezone(now.tzinfo)
        self.raise_paused_reason = reason
        _LOGGER.info("SpotNav heat control: raising set aside until %s (%s)", self.raise_paused_until.isoformat(), reason)

    def raised_24h(self, now: datetime) -> timedelta:
        """SpotNav's raised time over the last 24 hours (a rolling day)."""
        return lowered_within([(item[0], item[1]) for item in self.raisings], now - timedelta(days=1), now)  # type: ignore[misc]

    def raised_minutes(self, hours: list[datetime], now: datetime) -> dict[datetime, int]:
        """Per hour: the minutes SpotNav held B (the raise) closed (up to now)."""
        intervals = [(item[0], item[1]) for item in self.raisings]
        out: dict[datetime, int] = {}
        for hour in hours:
            minutes = lowered_within(intervals, hour, min(now, hour + timedelta(hours=1))) if hour < now else timedelta(0)  # type: ignore[arg-type]
            out[hour] = round(minutes.total_seconds() / 60)
        return out

    # ---- figures

    def _day(self, now: datetime) -> str:
        return now.astimezone(time_zones.require_zone(self.shadow.tz)).date().isoformat()

    def _midnight(self, day: date) -> datetime:
        zone = time_zones.require_zone(self.shadow.tz)
        return datetime(day.year, day.month, day.day, tzinfo=zone)

    def lowered_24h(self, now: datetime) -> timedelta:
        """SpotNav's lowered time over the last 24 hours (a rolling day, not the local one)."""
        return lowered_within([(item[0], item[1]) for item in self.lowerings], now - timedelta(days=1), now)  # type: ignore[misc]

    def executed_minutes(self, hours: list[datetime], now: datetime) -> dict[datetime, int]:
        """Per hour: the minutes SpotNav held the relay closed (up to now)."""
        intervals = [(item[0], item[1]) for item in self.lowerings]
        out: dict[datetime, int] = {}
        for hour in hours:
            minutes = lowered_within(intervals, hour, min(now, hour + timedelta(hours=1))) if hour < now else timedelta(0)  # type: ignore[arg-type]
            out[hour] = round(minutes.total_seconds() / 60)
        return out

    def closings_ahead(self, now: datetime) -> list[tuple[datetime, datetime]] | None:
        """The relay's closings expected from now on while control runs (`relay_control.closings_ahead`: the running
        one to its decided end, then the plan's lowered runs by the rules); `None` while it does not run."""
        if self.state != STATE_RUNNING:
            return None
        held = self.held and self.decision is not None and self.decision.closed and self.decision.until is not None
        return relay_closings_ahead(
            self.shadow.plan,
            now,
            held_until=self.decision.until if held and self.decision is not None else None,
            last_end=self.last_end,
            lowerings=[(item[0], item[1]) for item in self.lowerings if item[1] is not None],  # type: ignore[misc]
            writes=self.writes,
            day_of=self._day,
            safety=self.safety,
            max_writes=paths.max_writes(self.relay),
        )

    def _set(self, state: str, reason: str | None) -> None:
        if (state, reason) != (self.state, self.reason):
            _LOGGER.debug("SpotNav heat control: %s (%s)", state, reason)
        self.state, self.reason = state, reason

    def _log(self, now: datetime, what: str, **extra: Any) -> None:
        self.log.append({"at": now.isoformat(), "what": what, **extra})

    def _log_wait(self, now: datetime, what: str, why: str) -> None:
        """A write that waits is logged once, not at every look it still waits (the log keeps `LOG_SIZE`)."""
        if self._waits.get(what) == why:
            return
        self._waits[what] = why
        self._log(now, what, why=why)

    def _trim(self, now: datetime) -> None:
        oldest = now - timedelta(days=DAYS_KEPT)
        self.lowerings = [item for item in self.lowerings if item[1] is None or item[1] >= oldest]  # type: ignore[operator]
        first = self._day(oldest)
        self.writes = {day: count for day, count in self.writes.items() if day >= first}
        self.raisings = [item for item in self.raisings if item[1] is None or item[1] >= oldest]  # type: ignore[operator]
        self.renewals = {day: count for day, count in self.renewals.items() if day >= first}
        self.offset_moves = [(at, direction) for at, direction in self.offset_moves if now - at < paths.REVERSAL_SPACING]
        self.activations = {day: count for day, count in self.activations.items() if day >= first}

    # ---- what is shown

    def status(self) -> dict[str, Any]:
        """The card's `control` block (see `heat/view.py`)."""
        result = None if self.relay is None else self.checks.get(self.relay)
        config = self.shadow.config()
        path = config.control_path if lower_entity(config) is not None else None
        return {
            "enabled": config.control_enabled,
            # The path steered (`heat/paths.py`), whether it has yet to run on a real heat pump, and the path's
            # entities that are disabled in their integration (the opt-in's confirm enables them, `heat/optin.py`).
            "path": path,
            "untested": path in paths.UNTESTED,
            "enable_entities": optin.describe(self.hass, config),
            # A stored offset that reads other than the household's own value while SpotNav holds nothing (see
            # `_track_home`): `{entity_id, value, home}`, or `None`.
            "offset_home": None if self.offset_home is None else dict(self.offset_home),
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
            "raised": self.raised,
            "raise_until": None
            if not self.raised
            or self.raise_decision is None
            or not self.raise_decision.closed
            or self.raise_decision.until is None
            or self.state != STATE_RUNNING
            else self.raise_decision.until.isoformat(),
        }

    def _write_classes(self) -> dict[str, Any]:
        """The day's writes per class for the diagnostics: the path's cap and what is used (closings and openings;
        a volatile offset's renewals apart)."""
        today = self._day(dt_util.utcnow())
        volatile = self.relay is not None and paths.parse(self.relay)[1] == paths.KIND_OFFSET and self._volatile(self.relay)
        return {
            "class": None if self.relay is None else paths.write_class(self.relay, volatile),
            "cap": paths.max_writes(self.relay),
            "used": self.writes.get(today, 0),
            "renewals": {"cap": paths.RENEWALS_DAY, "used": self.renewals.get(today, 0)},
            "activations": {"cap": paths.ACTIVATION_WRITES_DAY, "used": self.activations.get(today, 0)},
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status(),
            "kept": self._data(),
            "pending": None if self.pending is None else {"closed": self.pending.closed, "at": self.pending.at.isoformat()},
            "writes": self._write_classes(),
            "levels": None if self._levels is None else {"running_kw": self._levels.running_kw, "baseline_kw": self._levels.baseline_kw},
            "raise": {
                "entity": self.raise_entity,
                "reason": None if self.raise_decision is None else self.raise_decision.reason,
                "pending": None if self.pending_b is None else {"closed": self.pending_b.closed, "at": self.pending_b.at.isoformat()},
                # A pump that does not answer B: told here only, never as an alert (lowering goes on).
                "observed": "raise_not_observed" if self.raise_check.result == FAILED else self.raise_check.result,
                "check_last": self.raise_check.last,
            },
            "log": list(self.log),
        }
