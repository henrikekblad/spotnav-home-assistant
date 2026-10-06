"""Which car was plugged in, at a charger more than one vehicle can charge at.

Runs only when the charger's vehicle list (`AutoSettings.vehicle_ids`, every detected vehicle by default)
holds two or more vehicles, the mode (`identify_mode`) is not `off`, and the charger reports a plug-in (a
charger that cannot say whether a car is connected never starts it).

**Evidence** (`judge`), read only from what Home Assistant already holds: identification never wakes a car,
and re-reads the cars' plug and tracker entities with `homeassistant.update_entity` at the plug-in and at the
ask deadline, at most once a minute per car (the refresh limiter `vehicle_refresh.py` keeps).

* strong +: the car's own plug sensor went on from five minutes before the plug-in;
* weak +: it is on but went on earlier, or the tracker says home;
* strong -: a fresh "not plugged" report (written a minute or more after the plug-in, or, for a car that streams
  its state, still not plugged after its report time), a fresh position away from home (the tracker written
  within two hours, or after the plug-in), or the car identified at another SpotNav charger that is connected.

**Automatic** (`decide`): exactly one strong + is the car; else the only one not excluded is; two strong +
is a conflict and asks at once; otherwise it asks after `ASK_AFTER_S` and keeps listening until
`LISTEN_FOR_S` after the plug-in, switching on decisive evidence unless a person answered. At most one
automatic decision per plug-in. **Always ask** asks at the plug-in, the buttons ordered by evidence, and
never switches by itself. **Off** does nothing.

**The question** goes to the charger's notification phones (event `vehicle_identify`) with one button per
car (at most three: with more, the two likeliest and "Open SpotNav"); the action ids carry an unguessable
nonce, and the first valid answer wins. A choice in the card or the app is an answer, and so is a person
changing the vehicle in the settings meanwhile. Once answered or decided the question is replaced on every
phone; unplugging clears it. Nobody answering, or the question dismissed, keeps the current car.

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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..planning.auto_settings import AutoSettings, AutoSettingsStore, IDENTIFY_ASK, IDENTIFY_OFF
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
#: A tracker written within this long is fresh.
TRACKER_FRESH_S: Final = 7200.0
#: Looked at again this often while listening: a poll that rewrites an unchanged value fires no state change.
REEVALUATE_S: Final = 60.0
#: A person who chose the car this soon before the plug-in has answered already.
RECENT_CHOICE_S: Final = 600.0
#: Integrations that push or stream the car's plug state (`plans/research_integrations`): seconds after the
#: plug-in by which a car plugged in here has said so. A car that still says "unplugged" then is not here.
#: A polled integration is never in this table: its report may simply be late.
PUSH_REPORT_S: Final = {
    "teslemetry": 120.0,
    "tessie": 120.0,
    "myskoda": 180.0,
    "mbapi2020": 180.0,
    "cardata": 180.0,
    "rivian": 180.0,
}

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


# --------------------------------------------------------------------------- the judgement (pure)


@dataclass(frozen=True, slots=True)
class Candidate:
    """What Home Assistant holds about one candidate car now."""

    vehicle_id: str
    plug: State | None = None
    #: The integration the plug sensor belongs to (its registry platform), for `PUSH_REPORT_S`.
    plug_platform: str | None = None
    location: State | None = None
    #: The car is identified at another SpotNav charger that is connected.
    elsewhere: bool = False


@dataclass(frozen=True, slots=True)
class Evidence:
    """One car's evidence: `positive` (`strong`, `weak` or `None`) and the method of a strong negative."""

    vehicle_id: str
    positive: str | None
    negative: str | None


def _fresh(state: State, t0: datetime, now: datetime) -> bool:
    reported = state.last_reported
    return reported >= t0 or (now - reported).total_seconds() <= TRACKER_FRESH_S


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
        report_s = PUSH_REPORT_S.get(candidate.plug_platform or "")
        if plug.last_reported >= t0 + timedelta(seconds=FRESH_REPORT_AFTER_S) or (
            report_s is not None and now >= t0 + timedelta(seconds=report_s) and plug.last_changed < t0
        ):
            negative = METHOD_PLUG_SENSOR
    location = candidate.location
    at_home = location_reading(location)
    if location is not None and at_home is False and _fresh(location, t0, now):
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

    # ------------------------------------------------------------------ lifecycle

    @callback
    def async_start(self, preview: Any = None) -> None:
        """Take the baseline (loading is not a plug-in) and start watching."""
        self._preview = preview
        self._connected = self._controller.adapter.vehicle_connected()
        self._expected_vehicle = self._settings().target.vehicle_id
        self._unsubscribe.append(self._controller.add_listener(self._observe))
        if preview is not None:
            self._unsubscribe.append(preview.add_listener(self._on_snapshot))
        self._unsubscribe.append(self._hass.bus.async_listen(EVENT_ACTION, self._on_action))
        self._unsubscribe.append(self._hass.bus.async_listen(EVENT_CLEARED, self._on_cleared))

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
        """Whether this charger has a car plugged in that is known to be `vehicle_id`."""
        return (
            self._connected is True
            and self._method in _CONFIRMING
            and self._current_vehicle() == vehicle_id
        )

    def dashboard(self) -> dict[str, Any] | None:
        """The dashboard's `identification` block: `None` unless a plug-in is being identified."""
        session = self._session
        if session is None:
            return None
        return {
            "state": session.state,
            "method": self._method,
            "vehicle_id": self._current_vehicle(),
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
        self._close()
        settings = self._settings()
        cars = self._candidates(settings)
        if settings.identify_mode == IDENTIFY_OFF:
            self._method = METHOD_MANUAL
            return
        if len(cars) < 2:
            self._method = METHOD_ONLY_CANDIDATE
            return
        if self._manual_at is not None and (now - self._manual_at).total_seconds() <= RECENT_CHOICE_S:
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
        session = self._session
        if session is not None and session.state == STATE_ASKING and self._notifier is not None:
            self._notifier.clear_vehicle_question(self.tag, session.phones)
        self._close()

    def _close(self) -> None:
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
        registry = er.async_get(self._hass)
        found = []
        for car in session.cars:
            plug_id, location_id = self._sources(car)
            entry = None if plug_id is None else registry.async_get(plug_id)
            candidate = Candidate(
                car,
                plug=None if plug_id is None else self._hass.states.get(plug_id),
                plug_platform=None if entry is None else entry.platform,
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
        if session is None or not session.listening or session.state == STATE_DECIDED:
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
        if session is None or session.state != STATE_WAITING:
            return
        session.state = STATE_ASKING
        session.nonce = secrets.token_hex(16)
        shown, open_button = buttons(session.order)
        session.shown = tuple(shown)
        if self._notifier is not None:
            session.phones = self._notifier.ask_vehicle(
                self.tag,
                [(f"{ACTION_PREFIX}{session.nonce}_{index}", session.names[car]) for index, car in enumerate(shown)],
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
        switch = None
        if car != self._current_vehicle():
            switch = self._hass.async_create_task(self._async_switch(car), eager_start=True)
        if was_asking and self._notifier is not None:
            self._notifier.retire_vehicle_question(self.tag, session.phones, wording, session.names.get(car, car))
        return switch

    async def _async_switch(self, car: str) -> None:
        """Plan for `car`: a settings write at the revision read now, as a person's choice would be."""
        settings = self._settings()
        if settings.target.vehicle_id == car:
            return
        self._expected_vehicle = car

        remembered = vehicle_properties.stored_properties(self._hass, car).target_percent

        def mutate(current: AutoSettings) -> AutoSettings:
            return current.with_target_vehicle(car, remembered)

        try:
            if self._preview is not None:
                await self._preview.async_apply_settings(mutate=mutate, expected_revision=settings.revision)
            else:
                await self._store.async_update(
                    self._entry_id, mutate=mutate, expected_revision=settings.revision, confirm=True
                )
        except Exception as err:  # noqa: BLE001 - a refused switch keeps the car that stands
            _LOGGER.warning("SpotNav could not switch the charger's vehicle: %s", getattr(err, "code", type(err).__name__))
        finally:
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
        session = self._session
        if session is None or session.nonce is None or not isinstance(action, str):
            return
        if not action.startswith(ACTION_PREFIX):
            return
        nonce, _, index = action[len(ACTION_PREFIX):].rpartition("_")
        if not hmac.compare_digest(nonce, session.nonce) or not index.isdigit():
            return
        position = int(index)
        if position >= len(session.shown):
            return
        self._settle(session.shown[position], METHOD_ANSWERED, "chosen")

    @callback
    def _on_cleared(self, event: Event) -> None:
        session = self._session
        if session is None or session.state != STATE_ASKING or event.data.get("tag") != self.tag:
            return
        # Swiped away: no answer, so the current car stays, and nothing switches it later.
        current = self._current_vehicle()
        self._method = METHOD_ASSUMED
        session.state = STATE_DECIDED
        session.listening = False
        session.nonce = None
        if self._notifier is not None and current is not None:
            self._notifier.retire_vehicle_question(self.tag, session.phones, "kept", session.names.get(current, current))
