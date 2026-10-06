"""Which car was plugged in, at a charger more than one vehicle can charge at.

Runs only when the charger's vehicle list (`AutoSettings.vehicle_ids`, every detected vehicle by default)
holds two or more vehicles, the mode (`identify_mode`) is not `off`, and the charger reports a plug-in (a
charger that cannot say whether a car is connected never starts it).

**Evidence** (`judge`), read only from what Home Assistant already holds: identification never wakes a car,
and re-reads the cars' plug and tracker entities with `homeassistant.update_entity` at the plug-in and at the
ask deadline, at most once a minute per car (the refresh limiter `vehicle_refresh.py` keeps).

* strong +: the car's own plug sensor went on from five minutes before the plug-in;
* weak +: it is on but went on earlier, or the tracker says home;
* strong -: a "not plugged" report written a minute or more after the plug-in, a position away from home reported
  after the plug-in or at most two minutes before it (an older one may be the drive home), or the car identified
  at another SpotNav charger that is connected (a car just decided there counts at once).

**Automatic** (`decide`): exactly one strong + is the car; else the only one not excluded is; two strong +
is a conflict and asks at once; otherwise it asks after `ASK_AFTER_S` and keeps listening until
`LISTEN_FOR_S` after the plug-in, switching on decisive evidence unless a person answered. At most one
automatic decision per plug-in. **Always ask** asks at the plug-in, the buttons ordered by evidence, and
never switches by itself. **Off** does nothing.

**The question** goes to the charger's notification phones (event `vehicle_identify`) with one button per
car (at most three: with more, the two likeliest and "Open SpotNav"); the action ids carry an unguessable
nonce, and the first valid answer wins. A choice in the card or the app is an answer, and so is a person
changing the vehicle in the settings meanwhile. Once answered or decided the question is replaced on every
phone; unplugging for longer than `UNPLUG_DEBOUNCE_S` clears it (a shorter unplug is the same plug-in, so what
was decided or answered holds). Nobody answering, or the question dismissed, keeps the current car.

A switch is a normal settings write (`AutoSettings.with_target_vehicle`: the car's own target),
so the plan recalculates as after any other write. Nothing here starts, stops or owns a charge.
`method` is how the car was decided, for the session record (`METHODS`).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import callback, CALLBACK_TYPE, Event, HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..planning.auto_settings import AutoSettings, AutoSettingsError, AutoSettingsStore, IDENTIFY_ASK, IDENTIFY_OFF
from .identification_sources import identification_sources, location_reading, plug_reading
from . import vehicle_properties
from .vehicle_discovery import resolve_target_vehicle
from .vehicle_refresh import limiter_for, REFRESH_SERVICE, REFRESH_SERVICE_DOMAIN

_LOGGER = logging.getLogger(__name__)

#: Ask after this long without a decision (automatic mode).
ASK_AFTER_S: Final = 180.0
#: Keep listening for decisive evidence this long after the plug-in.
LISTEN_FOR_S: Final = 1800.0
#: An unanswered question is retired this long after the plug-in, if the car is still plugged in.
QUESTION_FOR_S: Final = 12 * 3600.0
#: A plug sensor that went on this long before the plug-in still counts as this plug-in.
PLUG_WINDOW_BEFORE_S: Final = 300.0
#: An "unplugged" report written this long after the plug-in is fresh.
FRESH_REPORT_AFTER_S: Final = 60.0
#: A position reported this long before the plug-in, or after it, is fresh. An older one may be the car's last
#: report on its way home (a cloud poll every half hour), so it excludes nothing and the question is asked.
POSITION_MARGIN_BEFORE_S: Final = 120.0
#: An unplug shorter than this is the same plug-in (a reseated cable, a flapping connector): what was decided,
#: or answered by a person, holds, and it still counts toward the one automatic switch.
UNPLUG_DEBOUNCE_S: Final = 120.0
#: A person's answer that meets a revision conflict is written again this many times: it is the newest intent.
ANSWER_RETRIES: Final = 3
#: Looked at again this often while listening: a poll that rewrites an unchanged value fires no state change.
REEVALUATE_S: Final = 60.0
#: A person who chose the car this soon before the plug-in has answered already.
RECENT_CHOICE_S: Final = 600.0
STRONG: Final = "strong"
WEAK: Final = "weak"

METHOD_MANUAL: Final = "manual"
METHOD_ANSWERED: Final = "answered"
METHOD_PLUG_SENSOR: Final = "plug_sensor"
METHOD_LOCATION: Final = "location"
METHOD_ONLY_CANDIDATE: Final = "only_candidate"
#: Nobody answered and nothing decided it: the car that was already chosen is assumed.
METHOD_ASSUMED: Final = "assumed"
METHODS: Final = (
    METHOD_MANUAL, METHOD_ANSWERED, METHOD_PLUG_SENSOR, METHOD_LOCATION, METHOD_ONLY_CANDIDATE, METHOD_ASSUMED,
)
#: Methods that say which car it is, so another charger may exclude it.
_CONFIRMING: Final = (METHOD_MANUAL, METHOD_ANSWERED, METHOD_PLUG_SENSOR, METHOD_LOCATION, METHOD_ONLY_CANDIDATE)

STATE_WAITING: Final = "waiting"
STATE_ASKING: Final = "asking"
STATE_DECIDED: Final = "decided"

#: Android shows at most this many buttons.
MAX_BUTTONS: Final = 3
ACTION_PREFIX: Final = "SPOTNAV_ID_"
EVENT_ACTION: Final = "mobile_app_notification_action"
EVENT_CLEARED: Final = "mobile_app_notification_cleared"
_STORE_VERSION: Final = 1
_STORE_KEY_PREFIX: Final = f"{DOMAIN}.identification"


# --------------------------------------------------------------------------- the judgement (pure)


@dataclass(frozen=True, slots=True)
class Candidate:
    """What Home Assistant holds about one candidate car now."""

    vehicle_id: str
    plug: State | None = None
    location: State | None = None
    #: The car is identified at another SpotNav charger that is connected.
    elsewhere: bool = False


@dataclass(frozen=True, slots=True)
class Evidence:
    """One car's evidence: `positive` (`strong`, `weak` or `None`) and the method of a strong negative."""

    vehicle_id: str
    positive: str | None
    negative: str | None


def _fresh(state: State, t0: datetime) -> bool:
    """Reported after the plug-in, or just before it: never a report from the drive home."""
    return state.last_reported >= t0 - timedelta(seconds=POSITION_MARGIN_BEFORE_S)


def judge(candidate: Candidate, t0: datetime, now: datetime) -> Evidence:
    """One car's evidence at `now` for a plug-in at `t0`."""
    positive: str | None = None
    negative: str | None = METHOD_LOCATION if candidate.elsewhere else None
    plug = candidate.plug
    reading = plug_reading(plug)
    if plug is not None and reading is True:
        recent = plug.last_changed >= t0 - timedelta(seconds=PLUG_WINDOW_BEFORE_S)
        positive = STRONG if recent else WEAK
    elif plug is not None and reading is False and negative is None:
        # Only a report written after the plug-in says "not here"; silence or an older report says nothing.
        if plug.last_reported >= t0 + timedelta(seconds=FRESH_REPORT_AFTER_S):
            negative = METHOD_PLUG_SENSOR
    location = candidate.location
    at_home = location_reading(location)
    if location is not None and at_home is False and _fresh(location, t0):
        negative = negative or METHOD_LOCATION
    elif at_home is True and positive is None:
        positive = WEAK
    return Evidence(candidate.vehicle_id, positive, negative)


def decide(evidence: Sequence[Evidence]) -> tuple[str | None, str | None, bool]:
    """(the car, how it was told, whether two cars both say they are plugged in)."""
    remaining = [item for item in evidence if item.negative is None]
    strong = [item for item in remaining if item.positive == STRONG]
    if len(strong) == 1:
        return strong[0].vehicle_id, METHOD_PLUG_SENSOR, False
    if len(strong) > 1:
        return None, None, True
    if len(remaining) == 1 and len(evidence) > 1:
        excluded = {item.negative for item in evidence if item.negative is not None}
        return remaining[0].vehicle_id, METHOD_LOCATION if METHOD_LOCATION in excluded else METHOD_PLUG_SENSOR, False
    return None, None, False


def ordered(evidence: Sequence[Evidence], current: str | None) -> list[str]:
    """The cars, likeliest first: by evidence, then the current car, then as given."""

    def rank(pair: tuple[int, Evidence]) -> tuple[int, int, int]:
        index, item = pair
        if item.negative is not None:
            kind = 3
        else:
            kind = {STRONG: 0, WEAK: 1}.get(item.positive or "", 2)
        return kind, 0 if item.vehicle_id == current else 1, index

    return [item.vehicle_id for _, item in sorted(enumerate(evidence), key=rank)]


def buttons(cars: Sequence[str]) -> tuple[list[str], bool]:
    """The cars that get a button, and whether an "Open SpotNav" button is added (Android shows three)."""
    if len(cars) <= MAX_BUTTONS:
        return list(cars), False
    return list(cars[: MAX_BUTTONS - 1]), True


# --------------------------------------------------------------------------- the runtime


@dataclass(slots=True)
class _Session:
    """One plug-in's identification."""

    t0: datetime
    mode: str
    cars: tuple[str, ...]
    names: dict[str, str]
    order: list[str]
    likely: frozenset[str] = frozenset()
    state: str = STATE_WAITING
    listening: bool = True
    nonce: str | None = None
    shown: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    unsubscribe: list[CALLBACK_TYPE] = field(default_factory=list)
    #: When the car was unplugged, while the debounce runs.
    unplugged_at: datetime | None = None
    #: After a replug within the debounce: what is new since the unplug (`recheck_from`) is looked for until
    #: `recheck_until`; the replug's instant is `replugged_at`.
    recheck_from: datetime | None = None
    recheck_until: datetime | None = None
    replugged_at: datetime | None = None


class VehicleIdentifier:
    """One charger's vehicle identification (see the module docstring)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        controller: Any,
        store: AutoSettingsStore,
        notifier: Any = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._controller = controller
        self._store = store
        self._notifier = notifier
        self._preview: Any = None
        self._connected: bool | None = None
        self._session: _Session | None = None
        self._method: str | None = None
        self._expected_vehicle: str | None = None
        self._manual_at: datetime | None = None
        self._unsubscribe: list[CALLBACK_TYPE] = []
        # The car last decided (by the evidence or a person), and the switches still being written: until they
        # land, that car is the one this charger has, for its own decisions and for the other chargers'.
        self._wanted: str | None = None
        self._switches = 0
        self._switch_lock = asyncio.Lock()
        self._unplug_cancel: CALLBACK_TYPE | None = None
        # What was decided at this plug-in, kept across a restart (`_STORE_KEY_PREFIX`): a restart with the car
        # still plugged in is no new plug-in, and a decided car is not asked about again.
        self._memory: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}")
        self._remembered: dict[str, Any] | None = None
        self._memory_lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Read what was decided at the plug-in in progress before a restart, if anything."""
        raw = await self._memory.async_load()
        self._remembered = raw if isinstance(raw, dict) and raw.get("method") in METHODS else None

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: forget what was decided at it."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    def _remember(self, method: str | None) -> None:
        """Keep (or, with `None`, forget) what was decided at this plug-in, in the order it was decided."""
        self._hass.async_create_task(self._async_remember(method), eager_start=True)

    async def _async_remember(self, method: str | None) -> None:
        async with self._memory_lock:
            try:
                if method is None:
                    await self._memory.async_remove()
                else:
                    await self._memory.async_save({"method": method})
            except Exception as err:  # noqa: BLE001 - losing it costs one question after a restart
                _LOGGER.debug("SpotNav could not keep the identification decision: %s", type(err).__name__)

    # ------------------------------------------------------------------ lifecycle

    @callback
    def async_start(self, preview: Any = None) -> None:
        """Take the baseline (loading is not a plug-in) and start watching."""
        self._preview = preview
        self._connected = self._controller.adapter.vehicle_connected()
        self._expected_vehicle = self._settings().target.vehicle_id
        remembered, self._remembered = self._remembered, None
        self._unsubscribe.append(self._controller.add_listener(self._observe))
        if preview is not None:
            self._unsubscribe.append(preview.add_listener(self._on_snapshot))
        self._unsubscribe.append(self._hass.bus.async_listen(EVENT_ACTION, self._on_action))
        self._unsubscribe.append(self._hass.bus.async_listen(EVENT_CLEARED, self._on_cleared))
        if self._connected is True:
            if remembered is not None:
                # Decided before the restart, the same car still plugged in: nothing to identify again.
                self._method = remembered["method"]
            else:
                # Plugged in across a restart with nothing decided: identify it as a plug-in seen late.
                self._plugged_in(dt_util.utcnow())
        elif remembered is not None:
            self._remember(None)

    @callback
    def async_shutdown(self) -> None:
        for remove in self._unsubscribe:
            remove()
        self._unsubscribe.clear()
        self._close()

    # ------------------------------------------------------------------ what others read

    @property
    def method(self) -> str | None:
        """How the car at this charger was decided for the latest plug-in (`METHODS`), or `None` before one."""
        return self._method

    @property
    def tag(self) -> str:
        """The notification tag that replaces or clears this charger's question on every phone."""
        return f"spotnav_{self._entry_id}_identify"

    def claims(self, vehicle_id: str) -> bool:
        """Whether this charger has a car plugged in that is known to be `vehicle_id` (a car just decided counts
        at once, while its switch is still being written)."""
        return (
            self._connected is True
            and self._method in _CONFIRMING
            and self._planned_vehicle() == vehicle_id
        )

    def dashboard(self) -> dict[str, Any] | None:
        """The dashboard's `identification` block: `None` unless a plugged-in car is being identified."""
        session = self._session
        if session is None or self._connected is False:
            return None
        return {
            "state": session.state,
            "method": self._method,
            "vehicle_id": self._planned_vehicle(),
            "since": session.t0.isoformat(),
            "candidates": [
                {"vehicle_id": car, "name": session.names.get(car, car), "likely": car in session.likely}
                for car in session.order
            ],
        }

    def diagnostics(self) -> dict[str, Any]:
        """Method and timings only: no car names, plates, places or entities."""
        session = self._session
        return {
            "method": self._method,
            "state": None if session is None else session.state,
            "since": None if session is None else session.t0.isoformat(),
            "candidates": 0 if session is None else len(session.cars),
            "asked_phones": 0 if session is None else len(session.phones),
        }

    # ------------------------------------------------------------------ observing

    def _settings(self) -> AutoSettings:
        return self._store.settings(self._entry_id)

    def _current_vehicle(self) -> str | None:
        settings = self._settings()
        return resolve_target_vehicle(self._hass, settings.target.vehicle_id, settings.vehicle_ids)[0]

    def _planned_vehicle(self) -> str | None:
        """The car decided last while its switch is still being written, else the one the settings name."""
        return self._wanted if self._switches > 0 else self._current_vehicle()

    @callback
    def _observe(self, *_: Any) -> None:
        connected = self._controller.adapter.vehicle_connected()
        before = self._connected
        if connected is not None:
            self._connected = connected
        self._check_person()
        if before is False and connected is True:
            self._plugged_in(dt_util.utcnow())
        elif before is True and connected is False:
            self._unplugged()

    @callback
    def _on_snapshot(self, _snapshot: Any) -> None:
        self._check_person()

    @callback
    def _on_source(self, _event: Event) -> None:
        self._evaluate()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._evaluate()

    @callback
    def _check_person(self) -> None:
        """A change of the target vehicle that identification did not make is a person's choice."""
        stored = self._settings().target.vehicle_id
        if stored == self._expected_vehicle:
            return
        self._expected_vehicle = stored
        self._manual_at = dt_util.utcnow()
        session = self._session
        if session is not None and stored is not None and stored in session.cars:
            self._settle(stored, METHOD_MANUAL, "chosen")

    # ------------------------------------------------------------------ a plug-in

    def _candidates(self, settings: AutoSettings) -> list[tuple[str, str]]:
        """The cars that can charge here, as (id, name), by name."""
        _, choices = resolve_target_vehicle(self._hass, settings.target.vehicle_id, settings.vehicle_ids)
        return [(c.id, c.name) for c in choices]

    @callback
    def _plugged_in(self, now: datetime) -> None:
        session = self._session
        if self._unplug_cancel is not None and session is not None:
            # Back within the debounce: the same plug-in unless what the cars report since the unplug says
            # another car is here now (`_recheck`).
            self._unplug_cancel()
            self._unplug_cancel = None
            session.recheck_from = session.unplugged_at
            session.recheck_until = now + timedelta(seconds=LISTEN_FOR_S)
            session.replugged_at = now
            session.unplugged_at = None
            if session.state == STATE_DECIDED:
                self._recheck()
            elif session.state == STATE_WAITING and now >= session.t0 + timedelta(seconds=ASK_AFTER_S):
                # The ask deadline passed while the charger was empty.
                self._ask()
            return
        self._begin(now)

    def _begin(self, now: datetime, *, again: bool = False) -> None:
        """Identify the car plugged in at `now`. `again`: another car than the one decided is here after a short
        unplug, so what was decided or answered before no longer applies."""
        self._close()
        self._remember(None)
        settings = self._settings()
        cars = self._candidates(settings)
        if settings.identify_mode == IDENTIFY_OFF:
            self._method = METHOD_MANUAL
            return
        if len(cars) < 2:
            self._method = METHOD_ONLY_CANDIDATE
            return
        if not again and self._manual_at is not None and (now - self._manual_at).total_seconds() <= RECENT_CHOICE_S:
            # Chosen just before the car arrived: that is the answer.
            self._method = METHOD_MANUAL
            return
        ids = tuple(car for car, _ in cars)
        current = self._current_vehicle()
        session = _Session(
            t0=now, mode=settings.identify_mode, cars=ids, names=dict(cars),
            order=ordered([Evidence(car, None, None) for car in ids], current),
        )
        self._session = session
        self._method = METHOD_ASSUMED
        watched = [entity for car in ids for entity in self._sources(car) if entity is not None]
        if watched:
            session.unsubscribe.append(
                async_track_state_change_event(self._hass, watched, self._on_source)
            )
        session.unsubscribe.append(
            async_track_time_interval(self._hass, self._on_tick, timedelta(seconds=REEVALUATE_S))
        )
        session.unsubscribe.append(
            async_track_point_in_utc_time(self._hass, self._ask_deadline, now + timedelta(seconds=ASK_AFTER_S))
        )
        session.unsubscribe.append(
            async_track_point_in_utc_time(self._hass, self._listen_end, now + timedelta(seconds=LISTEN_FOR_S))
        )
        session.unsubscribe.append(
            async_track_point_in_utc_time(self._hass, self._question_end, now + timedelta(seconds=QUESTION_FOR_S))
        )
        self._refresh(ids)
        self._evaluate()
        if session.mode == IDENTIFY_ASK and self._session is session:
            self._ask()

    @callback
    def _unplugged(self) -> None:
        if self._session is None:
            self._remember(None)
            return
        if self._unplug_cancel is not None:
            return
        self._session.unplugged_at = dt_util.utcnow()
        self._unplug_cancel = async_track_point_in_utc_time(
            self._hass, self._unplug_settled, dt_util.utcnow() + timedelta(seconds=UNPLUG_DEBOUNCE_S)
        )

    @callback
    def _unplug_settled(self, _now: datetime) -> None:
        """Unplugged for longer than a flap: the plug-in is over."""
        self._unplug_cancel = None
        session = self._session
        if session is not None and session.state == STATE_ASKING and self._notifier is not None:
            self._notifier.clear_vehicle_question(self.tag, session.phones)
        self._close()
        self._remember(None)

    @callback
    def _recheck(self) -> None:
        """After a replug within the debounce: is it another car than the decided one?

        It is when the decided car reports, since the unplug, that it is not plugged in or away from home, or
        (unless a person decided) another car's plug sensor went on since the unplug. Then what was decided no
        longer applies: the new car is identified as a fresh plug-in (a switch or the question, and its own one
        automatic switch). Otherwise it was a flap and the decided car stays.
        """
        session = self._session
        if session is None or session.recheck_from is None or session.replugged_at is None:
            return
        since = session.recheck_from
        decided = self._planned_vehicle()
        person = self._method in (METHOD_ANSWERED, METHOD_MANUAL)
        for car in session.cars:
            plug_id, location_id = self._sources(car)
            plug = None if plug_id is None else self._hass.states.get(plug_id)
            location = None if location_id is None else self._hass.states.get(location_id)
            if car == decided:
                gone = (plug is not None and plug_reading(plug) is False and plug.last_reported >= since) or (
                    location is not None and location_reading(location) is False and location.last_reported >= since
                )
                if gone:
                    break
            elif not person and plug is not None and plug_reading(plug) is True and plug.last_changed >= since:
                break
        else:
            return
        self._begin(session.replugged_at, again=True)

    def _close(self) -> None:
        if self._unplug_cancel is not None:
            self._unplug_cancel()
            self._unplug_cancel = None
        session = self._session
        self._session = None
        if session is not None:
            for remove in session.unsubscribe:
                remove()

    def _sources(self, car: str) -> tuple[str | None, str | None]:
        plug, location = identification_sources(self._hass, car)
        return plug.entity_id, location.entity_id

    def _refresh(self, cars: Iterable[str]) -> None:
        """Re-read the cars' plug and tracker entities (cheap, never a wake-up), at most once a minute each."""
        limiter = limiter_for(self._hass)
        for car in cars:
            entities = [entity for entity in self._sources(car) if entity is not None]
            if not entities or limiter.retry_after_s(car) is not None:
                continue
            limiter.note_refresh(car)
            self._hass.async_create_task(self._async_update(entities), eager_start=False)

    async def _async_update(self, entities: list[str]) -> None:
        try:
            await self._hass.services.async_call(
                REFRESH_SERVICE_DOMAIN, REFRESH_SERVICE, {"entity_id": entities}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - a car integration that cannot re-read is no evidence
            _LOGGER.debug("SpotNav could not re-read a car's identification entities: %s", type(err).__name__)

    # ------------------------------------------------------------------ deciding

    def _evidence(self, session: _Session, now: datetime) -> list[Evidence]:
        found = []
        for car in session.cars:
            plug_id, location_id = self._sources(car)
            candidate = Candidate(
                car,
                plug=None if plug_id is None else self._hass.states.get(plug_id),
                location=None if location_id is None else self._hass.states.get(location_id),
                elsewhere=self._elsewhere(car),
            )
            found.append(judge(candidate, session.t0, now))
        return found

    def _elsewhere(self, car: str) -> bool:
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id == self._entry_id:
                continue
            other = getattr(getattr(entry, "runtime_data", None), "identifier", None)
            if other is not None and other.claims(car):
                return True
        return False

    @callback
    def _evaluate(self) -> None:
        session = self._session
        if session is None or self._connected is False:
            return
        if (
            session.state == STATE_DECIDED
            and session.recheck_until is not None
            and dt_util.utcnow() < session.recheck_until
        ):
            self._recheck()
            return
        if not session.listening or session.state == STATE_DECIDED:
            return
        evidence = self._evidence(session, dt_util.utcnow())
        if session.state == STATE_WAITING:
            session.order = ordered(evidence, self._current_vehicle())
        session.likely = frozenset(item.vehicle_id for item in evidence if item.positive == STRONG and not item.negative)
        if session.mode == IDENTIFY_ASK:
            return
        car, method, conflict = decide(evidence)
        if car is not None and method is not None:
            self._settle(car, method, "recognised")
        elif conflict:
            self._ask()

    @callback
    def _ask_deadline(self, _now: datetime) -> None:
        self._refresh(() if self._session is None else self._session.cars)
        self._evaluate()
        self._ask()

    @callback
    def _listen_end(self, _now: datetime) -> None:
        session = self._session
        if session is not None:
            session.listening = False
            self._ask()

    @callback
    def _question_end(self, _now: datetime) -> None:
        session = self._session
        if session is not None and session.state == STATE_ASKING and self._notifier is not None:
            self._notifier.clear_vehicle_question(self.tag, session.phones)
        if session is not None:
            session.state = STATE_DECIDED
            session.nonce = None

    @callback
    def _ask(self) -> None:
        session = self._session
        if session is None or session.state != STATE_WAITING or self._connected is False:
            # No question for an empty charger: a replug within the debounce asks, if it still has to.
            return
        session.state = STATE_ASKING
        session.nonce = secrets.token_hex(16)
        shown, open_button = buttons(session.order)
        session.shown = tuple(shown)
        if self._notifier is not None:
            session.phones = self._notifier.ask_vehicle(
                self.tag,
                [
                    (f"{ACTION_PREFIX}{self._entry_id}_{session.nonce}_{index}", session.names[car])
                    for index, car in enumerate(shown)
                ],
                open_button=open_button,
            )

    @callback
    def _settle(self, car: str, method: str, wording: str) -> asyncio.Task[None] | None:
        """Decide the plug-in's car: switch to it when it is not the current one, retire the question.

        Answers the switch's task, if there is one.
        """
        session = self._session
        if session is None:
            return None
        was_asking = session.state == STATE_ASKING
        session.state = STATE_DECIDED
        session.listening = False
        session.nonce = None
        self._method = method
        self._remember(method)
        person = method in (METHOD_ANSWERED, METHOD_MANUAL)
        switch = None
        if car != self._planned_vehicle():
            self._wanted = car
            self._switches += 1
            switch = self._hass.async_create_task(self._async_switch(car, person=person), eager_start=True)
        if was_asking and self._notifier is not None:
            self._notifier.retire_vehicle_question(self.tag, session.phones, wording, session.names.get(car, car))
        return switch

    async def _async_switch(self, car: str, *, person: bool) -> None:
        """Plan for `car`: a settings write at the revision read now, one switch at a time.

        A switch a newer decision has replaced is not written. An automatic switch that meets a revision
        conflict yields to the write that came first; a person's answer is the newest intent and is written
        again on the record that now stands.
        """
        try:
            async with self._switch_lock:
                for _attempt in range(ANSWER_RETRIES + 1 if person else 1):
                    if self._wanted != car:
                        return
                    settings = self._settings()
                    if settings.target.vehicle_id == car:
                        return
                    self._expected_vehicle = car
                    remembered = vehicle_properties.stored_properties(self._hass, car).target_percent

                    def mutate(current: AutoSettings, remembered: float | None = remembered) -> AutoSettings:
                        return current.with_target_vehicle(car, remembered)

                    try:
                        if self._preview is not None:
                            await self._preview.async_apply_settings(mutate=mutate, expected_revision=settings.revision)
                        else:
                            await self._store.async_update(
                                self._entry_id, mutate=mutate, expected_revision=settings.revision, confirm=True
                            )
                        return
                    except AutoSettingsError as err:
                        if err.code != "revision_conflict":
                            raise
                        self._expected_vehicle = self._settings().target.vehicle_id
        except Exception as err:  # noqa: BLE001 - a refused switch keeps the car that stands
            _LOGGER.warning("SpotNav could not switch the charger's vehicle: %s", getattr(err, "code", type(err).__name__))
        finally:
            self._switches -= 1
            self._expected_vehicle = self._settings().target.vehicle_id

    # ------------------------------------------------------------------ answers

    async def async_answer(self, vehicle_id: Any) -> bool:
        """A person's answer from the card or the app: `False` when nothing is being identified or the car is not
        one of the candidates."""
        session = self._session
        if session is None or not isinstance(vehicle_id, str) or vehicle_id not in session.cars:
            return False
        switch = self._settle(vehicle_id, METHOD_ANSWERED, "chosen")
        if switch is not None:
            await switch
        return True

    @callback
    def _on_action(self, event: Event) -> None:
        action = event.data.get("action")
        if not isinstance(action, str) or not action.startswith(ACTION_PREFIX):
            return
        parts = action[len(ACTION_PREFIX):].rsplit("_", 2)
        if len(parts) != 3 or parts[0] != self._entry_id or not parts[2].isdigit():
            return
        _, nonce, index = parts
        session = self._session
        if session is None or session.nonce is None or not hmac.compare_digest(nonce, session.nonce):
            # A button of a question that is gone (answered elsewhere, unplugged, a restart): take it off the
            # phones rather than leave a button that does nothing.
            if session is None or session.state != STATE_ASKING:
                self._clear_everywhere()
            return
        position = int(index)
        if position >= len(session.shown):
            return
        self._settle(session.shown[position], METHOD_ANSWERED, "chosen")

    def _clear_everywhere(self) -> None:
        if self._notifier is not None:
            self._notifier.clear_vehicle_question(self.tag, tuple(self._settings().notifications.targets))

    @callback
    def _on_cleared(self, event: Event) -> None:
        session = self._session
        if session is None or session.state != STATE_ASKING or event.data.get("tag") != self.tag:
            return
        # Swiped away: no answer, so the current car stays, and nothing switches it later.
        current = self._current_vehicle()
        self._method = METHOD_ASSUMED
        self._remember(METHOD_ASSUMED)
        session.state = STATE_DECIDED
        session.listening = False
        session.nonce = None
        if self._notifier is not None and current is not None:
            self._notifier.retire_vehicle_question(self.tag, session.phones, "kept", session.names.get(current, current))
