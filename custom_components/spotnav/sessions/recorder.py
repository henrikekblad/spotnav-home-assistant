"""One charger's session recorder: watches the charge and keeps the session record.

A session opens when the charger is seen charging (whatever started it) and closes when it has stopped
for `END_DEBOUNCE_S`, or at once when the car is unplugged. A pause shorter than that (a car that hesitates)
is the same session. The session ends when the charge stopped, not when the debounce ran out.

A stop SpotNav itself made while the car stays plugged in, to give the charge back later, ends nothing however
long it lasts (`PauseFacts`, `_held`): load balancing's pause, a phase switch that settles, the sun waiting for
surplus, and the gap between two windows of one plan. The debounce counts only from when nothing holds the
charge any more: the car is unplugged, a person stopped it (or paused Auto), the plan ended its charge, or the
car ended it itself. The entry keeps its start cause and runs from the first start to the last stop. A person's
Start seen after a stop that SpotNav did not hold is a new decision and opens a new entry.

Energy is read as the difference between samples, from the charger's energy register (or SpotNav's own
integration of a smart plug's power, which stands in for one), or, with neither, estimated from the
current the charger was asked for and marked `estimated`. Each stretch of energy is split over the price
intervals it was delivered in (`costing.split_energy`) and kept as kWh with the raw spot price of each, so
a price change inside the session is honoured. No cost is stored: it is made when it is read, with the
person's settings then.

A session open across a restart is resumed when the charger is charging again, and otherwise closed at
the last time it was seen charging (or now, if the register shows energy was delivered meanwhile). A
charging state that cannot be read (the charger's integration reloading) is handled the same way: no
observation, so the session is neither split nor ended by it; if the charge is not seen again it ended
when it was last seen going.

Nothing here awaits or writes to a charger: it reads, and saves through the store.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .costing import merge_slices, split_energy, SpotInterval
from .model import (
    ChargeSession,
    SOURCE_ESTIMATED,
    SOURCE_INTEGRATED,
    SOURCE_REGISTER,
    STARTED_HYBRID,
    STARTED_MANUAL,
    STARTED_OTHER,
    STARTED_PLAN_WINDOW,
    STARTED_SOLAR,
)
from .store import SessionStore
from ..util import finite_number

_LOGGER = logging.getLogger(__name__)

#: A stopped charge this long is a finished session.
END_DEBOUNCE_S: Final = 120.0
#: How often an open session is sampled between the charger's own reports.
TICK_S: Final = 60
#: A register that falls to this or below is a reset to zero; its value is the energy since.
RESET_TOLERANCE_KWH: Final = 0.5
#: Less energy than this is not a charge (a charger that was started and never taken).
MIN_ENERGY_KWH: Final = 0.01
#: An estimated session shorter than this is not recorded.
MIN_ESTIMATED_S: Final = 120.0
#: How far a window's end, or the car ending the charge, may lie before the stop the recorder saw and still be
#: that stop's cause (the recorder may see a stop up to a tick late, and a stop sent at a window's end lands after it).
STOP_CAUSE_TOLERANCE_S: Final = 300.0


@dataclass(frozen=True, slots=True)
class PauseFacts:
    """What says whether a stopped charge is one SpotNav holds back to give back later (`SessionRecorder._held`)."""

    #: Load balancing holds the charge back (`ChargingController.paused_by_balancing`).
    balancing_paused: bool = False
    #: A phase switch settles (`ChargingController.phase_switch_settling`).
    phase_switch_settling: bool = False
    #: The sun (solar or hybrid) waits for surplus to charge again (`inputs.SOLAR_WAITING_REASONS`).
    solar_waiting: bool = False
    #: A pause of Auto is admitted: a person's Start or Stop, or a pause they chose.
    auto_paused: bool = False
    #: The installed plan's windows.
    plan_windows: tuple[tuple[datetime, datetime], ...] = ()
    #: When the car last ended a charge by itself in this plug-in (`ChargingController.car_ended_at`).
    car_ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PriceBook:
    """The raw spot prices around now and the market's units."""

    spot: tuple[SpotInterval, ...]
    area_id: str | None
    currency: str | None
    major_unit: str | None
    minor_unit: str | None


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """One captured instant of everything a recorder reads from a charger."""

    #: `None` when nothing readable says it (no observation: the charger's integration reloading).
    charging: bool | None
    #: `None` when the charger cannot say.
    connected: bool | None
    register_kwh: float | None
    #: The register is SpotNav's integration of a smart plug's power.
    register_integrated: bool
    #: The power the charger should be drawing, for the estimate; `None` when unknowable.
    estimate_kw: float | None
    strategy: str | None
    vehicle_id: str | None
    vehicle_name: str | None
    #: The fraction of the car's power covered by surplus now, when the solar executor knows it.
    solar_share: float | None
    #: How the vehicle was decided at this plug-in (`vehicles/identification.py`), `None` when nothing decided it.
    vehicle_decided_by: str | None = None
    #: The entity `register_kwh` is read from.
    register_entity: str | None = None
    #: Whether a stop now is SpotNav's own pause of the charge.
    pause: PauseFacts = PauseFacts()


def window_gap(windows: tuple[tuple[datetime, datetime], ...], stopped: datetime, now: datetime) -> tuple[bool, bool]:
    """Whether a window of the plan ended at or after the stop (`stopped`, give or take `STOP_CAUSE_TOLERANCE_S`)
    and by now; and whether a window that begins after the stop has not ended yet: the plan's next window, ahead, or
    open with its start on its way."""
    tolerance = timedelta(seconds=STOP_CAUSE_TOLERANCE_S)
    ended = any(stopped - tolerance <= end <= now for _start, end in windows)
    ahead = any(start > stopped and end > now for start, end in windows)
    return ended, ahead


def started_by(hint: str | None, strategy: str | None) -> str:
    """How a session started, from who asked and the strategy then in force."""
    if hint == STARTED_MANUAL:
        return STARTED_MANUAL
    if hint == "min_soc":
        # The car's minimum charge level: SpotNav's own automatic start, as a plan window's (never "other", which
        # reads as a car that came back with a new need).
        hint = STARTED_PLAN_WINDOW
    if hint in (STARTED_PLAN_WINDOW, STARTED_SOLAR):
        return STARTED_HYBRID if strategy == STARTED_HYBRID else hint
    return STARTED_OTHER


class SessionRecorder:
    def __init__(
        self,
        hass: HomeAssistant,
        charger_id: str,
        store: SessionStore,
        *,
        facts: Callable[[], SessionFacts],
        prices: Callable[[datetime], PriceBook | None],
        consume_cause: Callable[[], str | None],
        subscribe: Callable[[Callable[[], None]], Callable[[], None]] | None = None,
        now: Callable[[], datetime] = dt_util.utcnow,
        start_soc: Callable[[], float | None] | None = None,
    ) -> None:
        self._hass = hass
        self._charger_id = charger_id
        self._store = store
        self._facts = facts
        self._prices = prices
        self._consume_cause = consume_cause
        self._subscribe = subscribe
        self._now = now
        self._start_soc = start_soc
        self._was_charging = False
        self._session: ChargeSession | None = None
        self._idle_since: datetime | None = None
        self._idle_seen_at: datetime | None = None
        self._resumed_at: datetime | None = None
        #: A window's end stopped the charge in this run: the gap to the plan's next window holds it, even once a new
        #: plan no longer lists the window that ended.
        self._window_stop = False
        #: When the stopped charge was last seen held (`_held`).
        self._held_at: datetime | None = None
        self._cancels: list[Callable[[], None]] = []
        self._shutdown = False

    @property
    def session(self) -> ChargeSession | None:
        """The open session, or `None`."""
        return self._session

    @callback
    def async_start(self) -> None:
        """Resume what the store holds, begin watching, and decide once now."""
        stored = self._store.open_raw(self._charger_id)
        if stored is not None:
            self._session = stored
            self._resumed_at = stored.last_sample_at or stored.start
        if self._subscribe is not None:
            self._cancels.append(self._subscribe(self._on_event))
        self._cancels.append(async_track_time_interval(self._hass, self._on_tick, timedelta(seconds=TICK_S)))
        self.evaluate()

    @callback
    def async_shutdown(self) -> None:
        """Stop watching. An open session stays in the store for the next start to resume."""
        self._shutdown = True
        for cancel in self._cancels:
            cancel()
        self._cancels.clear()
        if self._session is not None:
            self._store.set_open(self._session, charger_id=self._charger_id)

    @callback
    def _on_event(self) -> None:
        self.evaluate()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self.evaluate()

    @callback
    def evaluate(self, now: datetime | None = None) -> None:
        """Decide from this instant's facts; the one entry point, also called by tests with a clock."""
        if self._shutdown:
            return
        now = now or self._now()
        facts = self._facts()
        session = self._session
        if session is None:
            if facts.charging:
                self._open(facts, now, self._consume_cause())
                self._was_charging = True
            return
        delivered = self._accrue(session, facts, now)
        if facts.vehicle_decided_by is not None and facts.connected is not False and (
            facts.vehicle_decided_by != session.vehicle_decided_by or facts.vehicle_id != session.vehicle_id
        ):
            # The car was identified, or answered for, after the charge began: the session is that car's.
            session.vehicle_id = facts.vehicle_id
            session.vehicle_name = facts.vehicle_name
            session.vehicle_decided_by = facts.vehicle_decided_by
        if facts.charging is None:
            # No observation: the charge is neither seen going nor seen stopped. A charge last seen going is
            # taken, as after a restart, to have gone on until then (`_resumed_at`); an idle debounce runs on.
            if self._was_charging and self._resumed_at is None:
                self._resumed_at = now
            if self._idle_seen_at is not None:
                self._look_held(facts, self._idle_since or now, now)
            if self._idle_seen_at is not None and (now - self._idle_seen_at).total_seconds() >= END_DEBOUNCE_S:
                self._close(session, self._idle_since or now, now)
            else:
                self._store.set_open(session, charger_id=self._charger_id)
            return
        self._was_charging = facts.charging
        if facts.charging:
            # Who started the charge, taken while it runs too, so a Start that changed nothing is never read later.
            cause = self._consume_cause()
            held_lately = self._held_at is not None and (now - self._held_at).total_seconds() <= 2 * TICK_S
            if self._idle_since is not None and cause == STARTED_MANUAL and not held_lately:
                # A person's Start after a stop that was not SpotNav's pause (a resume of a person's charge that load
                # balancing held back also reads as theirs): a new decision, a new entry.
                self._close(session, self._idle_since, now)
                self._open(facts, now, cause)
                return
            self._reset_idle()
            self._store.set_open(session, charger_id=self._charger_id)
            return
        if self._idle_since is None:
            # The charge stopped: it ended when it was last seen going, which after a restart is when
            # the previous run last sampled, unless energy arrived meanwhile.
            resumed = self._resumed_at
            self._idle_since = now if resumed is None or delivered > 0 else resumed
            self._idle_seen_at = now
            self._resumed_at = None
        if facts.connected is not False:
            self._look_held(facts, self._idle_since, now)
        if facts.connected is False or (
            self._idle_seen_at is not None
            and (now - self._idle_seen_at).total_seconds() >= END_DEBOUNCE_S
        ):
            self._close(session, self._idle_since, now)
        else:
            self._store.set_open(session, charger_id=self._charger_id)

    def _reset_idle(self) -> None:
        self._idle_since = self._idle_seen_at = self._resumed_at = self._held_at = None
        self._window_stop = False

    def _look_held(self, facts: SessionFacts, stopped: datetime, now: datetime) -> None:
        """SpotNav holds the charge back to give it back: the debounce counts from when nothing holds it any more."""
        if self._held(facts, stopped, now):
            self._idle_seen_at = self._held_at = now

    def _held(self, facts: SessionFacts, stopped: datetime, now: datetime) -> bool:
        """Whether the charge that stopped at `stopped` is SpotNav's own pause of it, given back while the car stays
        plugged in. Never when the car ended it. Load balancing's pause and a phase switch's settle are; while no pause
        of Auto is admitted, so are the sun waiting for surplus and the gap from a window's end to the plan's next."""
        pause = facts.pause
        tolerance = timedelta(seconds=STOP_CAUSE_TOLERANCE_S)
        if pause.car_ended_at is not None and pause.car_ended_at >= stopped - tolerance:
            return False
        if pause.balancing_paused or pause.phase_switch_settling:
            return True
        if pause.auto_paused:
            return False
        if pause.solar_waiting:
            return True
        ended, ahead = window_gap(pause.plan_windows, stopped, now)
        self._window_stop = ahead and (self._window_stop or ended)
        return self._window_stop

    def _open(self, facts: SessionFacts, now: datetime, cause: str | None) -> None:
        if facts.register_kwh is not None:
            source = SOURCE_INTEGRATED if facts.register_integrated else SOURCE_REGISTER
        else:
            source = SOURCE_ESTIMATED
        book = self._prices(now)
        session = ChargeSession(
            id=f"{self._charger_id}-{int(now.timestamp())}",
            charger_id=self._charger_id,
            start=now,
            end=None,
            energy_kwh=0.0,
            energy_source=source,
            priced_kwh=0.0,
            cost_minor=None,
            reference_cost_minor=None,
            area_id=None if book is None else book.area_id,
            currency=None if book is None else book.currency,
            major_unit=None if book is None else book.major_unit,
            minor_unit=None if book is None else book.minor_unit,
            started_by=started_by(cause, facts.strategy),
            strategy=facts.strategy,
            vehicle_id=facts.vehicle_id,
            vehicle_name=facts.vehicle_name,
            vehicle_decided_by=facts.vehicle_decided_by,
            solar_kwh=0.0,
            solar_known_kwh=0.0,
            last_register_kwh=facts.register_kwh,
            register_entity=facts.register_entity if facts.register_kwh is not None else None,
            last_sample_at=now,
            start_soc_percent=None if self._start_soc is None else finite_number(self._start_soc()),
        )
        self._session = session
        self._reset_idle()
        self._store.set_open(session, charger_id=self._charger_id)

    def _accrue(self, session: ChargeSession, facts: SessionFacts, now: datetime) -> float:
        """Take the energy delivered since the last sample and price it; returns the kWh."""
        since = session.last_sample_at or session.start
        if now <= since:
            return 0.0
        if facts.charging and not self._was_charging and self._idle_since is not None:
            # The charge goes again after a stop this run saw: the sample stood still at the stop, and what the
            # charger reports now was drawn since the last tick at most.
            since = max(since, now - timedelta(seconds=TICK_S))
        delivered = 0.0
        if session.energy_source == SOURCE_ESTIMATED:
            # Charging at either end of the span counts: a stop is seen at its first report, after the last tick.
            if (facts.charging or self._was_charging) and facts.estimate_kw is not None:
                delivered = facts.estimate_kw * (now - since).total_seconds() / 3600.0
        else:
            reading = facts.register_kwh
            if session.register_entity is not None and facts.register_entity != session.register_entity:
                # Another register counts from another origin: measured on from its first reading, never against
                # the last one's.
                session.last_register_kwh = None
            if reading is not None:
                session.register_entity = facts.register_entity
                baseline = session.last_register_kwh
                if baseline is not None:
                    if reading >= baseline:
                        delivered = reading - baseline
                    elif reading <= RESET_TOLERANCE_KWH:
                        delivered = reading
                session.last_register_kwh = reading
        if delivered > 0 or facts.charging or self._was_charging:
            # While the charge stands still the sample stays at its stop, so a restart in a long pause still knows
            # when the charge last went.
            session.last_sample_at = now
        if delivered <= 0:
            return 0.0
        session.energy_kwh += delivered
        book = self._prices(now)
        if book is not None:
            if session.currency is None:
                session.currency, session.major_unit, session.minor_unit = (
                    book.currency, book.major_unit, book.minor_unit,
                )
            if session.area_id is None:
                session.area_id = book.area_id
            session.intervals = merge_slices(session.intervals, split_energy(book.spot, since, now, delivered))
        if facts.solar_share is not None:
            session.solar_known_kwh += delivered
            session.solar_kwh += delivered * facts.solar_share
        return delivered

    def _close(self, session: ChargeSession, ended: datetime, now: datetime) -> None:
        self._session = None
        self._reset_idle()
        session.end = min(max(ended, session.start), now)
        session.last_register_kwh = None
        session.register_entity = None
        session.last_sample_at = None
        # The level at the start serves the running charge's bar only; a closed record keeps its old shape.
        session.start_soc_percent = None
        seconds = (session.end - session.start).total_seconds()
        if (session.estimated and seconds < MIN_ESTIMATED_S) or (
            not session.estimated and session.energy_kwh < MIN_ENERGY_KWH
        ):
            # A charge nothing took: not a session.
            self._store.set_open(None, charger_id=self._charger_id)
            return
        self._store.close(session, now)
