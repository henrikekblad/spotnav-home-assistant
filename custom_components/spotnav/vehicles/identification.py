"""Which car was plugged in, at a charger more than one vehicle can charge at.

Runs only when the charger's vehicle list (`AutoSettings.vehicle_ids`, every detected vehicle by default)
holds two or more vehicles, the mode (`identify_mode`) is not `off`, and the charger reports a plug-in (a
charger that cannot say whether a car is connected never starts it).

**Evidence** (`judge`), read only from what Home Assistant already holds: identification never wakes a car,
and re-reads the cars' plug and tracker entities with `homeassistant.update_entity` at the plug-in and at the
ask deadline, at most once a minute per car (the refresh limiter `vehicle_refresh.py` keeps).

* strong +: the car's own plug sensor went on from five minutes before the plug-in;
* weak +: it is on but went on earlier, or the tracker says home;
* strong -: the car's plug sensor went to "not plugged" around or after the plug-in (one that only says it again
  says nothing: a cloud cache is re-written on every poll), a position away from home reported after the plug-in
  or at most two minutes before it and not the echo of SpotNav's own re-read (an older one may be the drive
  home), or the car identified at another SpotNav charger that is connected (a car just decided there counts at
  once).

**Automatic** (`decide`): exactly one strong + is the car; else the only one not excluded is; two strong +
is a conflict and asks at once; otherwise it asks after `ASK_AFTER_S` and keeps listening until
`LISTEN_FOR_S` after the plug-in, switching on decisive evidence unless a person answered. At most one
automatic decision per plug-in.

**The camera** (optional, `camera_rule.py`, `camera_identification.py`): while nothing has decided and two or
more cars are left, the charger's camera is asked once (one retry after an error) before the question. Its
answer decides alone only at high confidence between cars that differ in colour, each with a reference picture;
otherwise it puts the car it names first among the question's buttons. A query that takes longer than
`camera_rule.QUERY_TIMEOUT_S` is ignored. **Always ask** asks at the plug-in, the buttons ordered by evidence, and
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
from .camera_rule import camera_verdict, may_query, QUERY_TIMEOUT_S, Signature
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
#: A position written this soon after SpotNav asked Home Assistant to re-read the car is the echo of that re-read
#: (a cloud cache written again), not a report from the car.
REFRESH_ECHO_S: Final = 120.0
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
#: The charger's camera recognised the car (high confidence, among cars that differ in colour).
METHOD_CAMERA: Final = "camera"
METHODS: Final = (
    METHOD_MANUAL, METHOD_ANSWERED, METHOD_PLUG_SENSOR, METHOD_LOCATION, METHOD_ONLY_CANDIDATE, METHOD_ASSUMED,
    METHOD_CAMERA,
)
#: Methods that say which car it is, so another charger may exclude it.
_CONFIRMING: Final = (
    METHOD_MANUAL, METHOD_ANSWERED, METHOD_PLUG_SENSOR, METHOD_LOCATION, METHOD_ONLY_CANDIDATE, METHOD_CAMERA,
)

STATE_WAITING: Final = "waiting"
STATE_ASKING: Final = "asking"
STATE_DECIDED: Final = "decided"

#: Android shows at most this many buttons.
MAX_BUTTONS: Final = 3
ACTION_PREFIX: Final = "SPOTNAV_ID_"
EVENT_ACTION: Final = "mobile_app_notification_action"
EVENT_CLEARED: Final = "mobile_app_notification_cleared"
#: Why a person's choice cannot be taken (`answer_refusal`).
REFUSED_NOT_PLUGGED_IN: Final = "not_plugged_in"
REFUSED_NOT_HERE: Final = "not_here"
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
    #: When SpotNav last asked Home Assistant to re-read this car's entities (`REFRESH_ECHO_S`).
    refreshed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """One car's evidence: `positive` (`strong`, `weak` or `None`) and the method of a strong negative."""

    vehicle_id: str
    positive: str | None
    negative: str | None


def _fresh(state: State, t0: datetime, refreshed_at: datetime | None) -> bool:
    """Reported after the plug-in, or just before it (never a report from the drive home), and not the echo of
    SpotNav's own re-read: a cloud cache written again moves `last_reported` without the car saying anything."""
    reported = state.last_reported
    if reported < t0 - timedelta(seconds=POSITION_MARGIN_BEFORE_S):
        return False
    echo = refreshed_at is not None and refreshed_at <= reported <= refreshed_at + timedelta(seconds=REFRESH_ECHO_S)
    return not echo or state.last_changed >= t0 - timedelta(seconds=POSITION_MARGIN_BEFORE_S)


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
        # Only a car that went unplugged around or after the plug-in is not here. An "unplugged" that is only
        # written again says nothing: a cloud integration re-writes its cache (Kia's says "unplugged" for hours
        # after the car was plugged in), on its own poll or on SpotNav's re-read, and `last_reported` moves.
        if plug.last_changed >= t0 - timedelta(seconds=POSITION_MARGIN_BEFORE_S):
            negative = METHOD_PLUG_SENSOR
    location = candidate.location
    at_home = location_reading(location)
    if location is not None and at_home is False and _fresh(location, t0, candidate.refreshed_at):
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


def ordered(evidence: Sequence[Evidence], current: str | None, camera: str | None = None) -> list[str]:
    """The cars, likeliest first: by evidence (the car the camera names after a car whose own plug sensor says
    it was plugged in, before every other), then the current car, then as given."""

    def rank(pair: tuple[int, Evidence]) -> tuple[float, int, int]:
        index, item = pair
        kind: float
        if item.negative is not None:
            kind = 3
        elif item.vehicle_id == camera and item.positive != STRONG:
            kind = 0.5
        else:
            kind = {STRONG: 0, WEAK: 1}.get(item.positive or "", 2)
        return kind, 0 if item.vehicle_id == current else 1, index

    return [item.vehicle_id for _, item in sorted(enumerate(evidence), key=rank)]


def buttons(cars: Sequence[str]) -> tuple[list[str], bool]:
    """The cars that get a button, and whether an "Open SpotNav" button is added (Android shows three)."""
    if len(cars) <= MAX_BUTTONS:
        return list(cars), False
    return list(cars[: MAX_BUTTONS - 1]), True


def _evidence_record(candidate: Candidate, evidence: Evidence) -> dict[str, Any]:
    """One car's entities and what they said: the plug (state, when it changed and was last written), the position
    (home or away only), and the verdict: `plugged_in`, `likely`, `not_plugged_in`, `away`, `elsewhere` or `None`."""
    plug, location = candidate.plug, candidate.location
    if candidate.elsewhere:
        verdict: str | None = "elsewhere"
    elif evidence.negative == METHOD_PLUG_SENSOR:
        verdict = "not_plugged_in"
    elif evidence.negative == METHOD_LOCATION:
        verdict = "away"
    elif evidence.positive == STRONG:
        verdict = "plugged_in"
    elif evidence.positive == WEAK:
        verdict = "likely"
    else:
        verdict = None
    return {
        "vehicle_id": candidate.vehicle_id,
        "plug": None if plug is None else {
            "entity_id": plug.entity_id,
            "state": plug.state,
            "changed": plug.last_changed.isoformat(),
            "reported": plug.last_reported.isoformat(),
        },
        "location": None if location is None else {
            "entity_id": location.entity_id,
            "home": location_reading(location),
            "reported": location.last_reported.isoformat(),
        },
        "verdict": verdict,
    }


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
    #: Each car's entities and what they said at the last evaluation (`_evidence_record`).
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: The camera's queries at this plug-in, whether the last one failed with an error, the one running, the car
    #: its answer puts first, and its evidence entry (`{"camera": {entity_id, answer, confidence, used}}`).
    camera_attempts: int = 0
    camera_failed: bool = False
    camera_task: asyncio.Task[None] | None = None
    camera_pick: str | None = None
    camera_record: dict[str, Any] | None = None

    def all_evidence(self) -> list[dict[str, Any]]:
        return [*self.evidence, *([] if self.camera_record is None else [{"camera": self.camera_record}])]


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
        camera: Any = None,
    ) -> None:
        self._hass = hass
        #: The charger's camera (`camera_identification.CameraIdentification`), `None` without one.
        self._camera = camera
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
        # When SpotNav last asked Home Assistant to re-read each car (its echo is no report).
        self._refreshed_at: dict[str, datetime] = {}
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
        # A person's choice of the car is seen when it is written, so "just before the plug-in" is measured
        # from then (a snapshot listener may not hear of it until the plug-in itself).
        self._unsubscribe.append(self._store.add_write_listener(self._on_settings_written))
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
        if self._connected is not True:
            return None
        if session is None:
            # Nothing is being identified (identification off, a car chosen just before the plug-in, a decision
            # kept across a restart): with two or more cars at the charger the car can still be changed, so the
            # block says what stands.
            settings = self._settings()
            cars = self._candidates(settings)
            if len(cars) < 2:
                return None
            method = self._method or METHOD_MANUAL
            if settings.identify_mode == IDENTIFY_OFF and method == METHOD_MANUAL:
                # Identification off: nothing chose the car at this plug-in (no question, no detection), so the
                # block names no method; a person's correction still says it was answered.
                method = None
            return {
                "state": STATE_DECIDED,
                "method": method,
                "vehicle_id": self._planned_vehicle(),
                "since": None,
                "candidates": [{"vehicle_id": car, "name": name, "likely": False} for car, name in cars],
                "evidence": [],
            }
        return {
            "state": session.state,
            "method": self._method,
            "vehicle_id": self._planned_vehicle(),
            "since": session.t0.isoformat(),
            "candidates": [
                {"vehicle_id": car, "name": session.names.get(car, car), "likely": car in session.likely}
                for car in session.order
            ],
            "evidence": session.all_evidence(),
        }

    def diagnostics(self) -> dict[str, Any]:
        """Method, timings and the evidence the car was judged by: entity ids and states, never car names, plates,
        positions or zone names (a position says only home or away)."""
        session = self._session
        return {
            "method": self._method,
            "state": None if session is None else session.state,
            "since": None if session is None else session.t0.isoformat(),
            "candidates": 0 if session is None else len(session.cars),
            "asked_phones": 0 if session is None else len(session.phones),
            # When a person last chose the car (a choice shortly before a plug-in answers it, `RECENT_CHOICE_S`).
            "chosen_at": None if self._manual_at is None else self._manual_at.isoformat(),
            # The entities each car was judged by, their states and when they changed and were written: what a
            # field report needs. A position says only home or away, never where.
            "evidence": [] if session is None else session.all_evidence(),
            # The camera, its frame and which cars have reference pictures (never a picture).
            "camera": None if self._camera is None else self._camera.diagnostics(),
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
    def _on_settings_written(self, entry_id: str, before: AutoSettings, after: AutoSettings) -> None:
        if entry_id == self._entry_id and before.target.vehicle_id != after.target.vehicle_id:
            self._check_person()

    @callback
    def _check_person(self) -> None:
        """A change of the target vehicle that identification did not make is a person's choice, from now: this
        runs at the write (`_on_settings_written`), and finds nothing new when the charger reports later."""
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
                gone = (plug is not None and plug_reading(plug) is False and plug.last_changed >= since) or (
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
            if session.camera_task is not None and not session.camera_task.done():
                session.camera_task.cancel()

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
            self._refreshed_at[car] = dt_util.utcnow()
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
        record = []
        for car in session.cars:
            plug_id, location_id = self._sources(car)
            candidate = Candidate(
                car,
                plug=None if plug_id is None else self._hass.states.get(plug_id),
                location=None if location_id is None else self._hass.states.get(location_id),
                elsewhere=self._elsewhere(car),
                refreshed_at=self._refreshed_at.get(car),
            )
            evidence = judge(candidate, session.t0, now)
            found.append(evidence)
            record.append(_evidence_record(candidate, evidence))
        # What a field report needs to see why a car was decided: each car's entities, what they said, and when.
        session.evidence = record
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
        if session.state == STATE_DECIDED and session.listening and self._method == METHOD_CAMERA:
            self._correct_camera(session)
            return
        if not session.listening or session.state == STATE_DECIDED:
            return
        evidence = self._evidence(session, dt_util.utcnow())
        if session.state == STATE_WAITING:
            session.order = ordered(evidence, self._current_vehicle(), session.camera_pick)
        session.likely = frozenset(item.vehicle_id for item in evidence if item.positive == STRONG and not item.negative)
        if session.mode == IDENTIFY_ASK:
            return
        car, method, conflict = decide(evidence)
        if car is not None and method is not None:
            self._settle(car, method, "recognised")
        elif conflict:
            self._ask()
        else:
            self._maybe_ask_camera(session, evidence)

    # ------------------------------------------------------------------ the camera

    @callback
    def _correct_camera(self, session: _Session) -> None:
        """After the camera decided: a car's own decisive report corrects it (the same plug-in's switch, recorded
        by that report), and the decided car reporting itself unplugged or away with no other car decided brings
        the question back. A person's answer ends this: it outranks both."""
        evidence = self._evidence(session, dt_util.utcnow())
        car, method, conflict = decide(evidence)
        decided = self._planned_vehicle()
        if car is not None and method is not None:
            if car != decided:
                self._settle(car, method, "recognised")
            return
        if any(item.vehicle_id == decided and item.negative is not None for item in evidence) or conflict:
            session.state = STATE_WAITING
            session.order = ordered(evidence, self._current_vehicle())
            self._ask()

    def _maybe_ask_camera(self, session: _Session, evidence: Sequence[Evidence]) -> None:
        """Ask the camera, when it may be asked now (`camera_rule.may_query`) and has something to compare."""
        camera = self._camera
        if camera is None or (session.camera_task is not None and not session.camera_task.done()):
            return
        remaining = tuple(item.vehicle_id for item in evidence if item.negative is None)
        if not may_query(
            attempts=session.camera_attempts,
            failed=session.camera_failed,
            waiting=session.state == STATE_WAITING and session.listening,
            remaining=len(remaining),
            elapsed_s=(dt_util.utcnow() - session.t0).total_seconds(),
            window_s=ASK_AFTER_S,
        ):
            return
        if not camera.ready_for(remaining):
            return
        session.camera_attempts += 1
        session.camera_task = self._hass.async_create_background_task(
            self._async_ask_camera(session, remaining), f"spotnav camera identification {self._entry_id}"
        )

    async def _async_ask_camera(self, session: _Session, candidates: tuple[str, ...]) -> None:
        camera = self._camera
        settings = camera.settings()
        entity_id = None if settings is None else settings.camera_entity_id
        try:
            async with asyncio.timeout(QUERY_TIMEOUT_S):
                answer = await camera.async_ask(candidates)
        except TimeoutError:
            _LOGGER.debug("SpotNav's camera query took too long; the question is asked as without one")
            session.camera_failed = False
            session.camera_record = {"entity_id": entity_id, "answer": None, "confidence": None, "used": False}
            return
        except Exception as err:  # noqa: BLE001 - a failing camera or model is no evidence
            _LOGGER.debug("SpotNav's camera query failed: %s", type(err).__name__)
            session.camera_failed = True
            session.camera_record = {"entity_id": entity_id, "answer": None, "confidence": None, "used": False}
            if self._session is session:
                # Once more, if it still may (`camera_rule.QUERY_ATTEMPTS`).
                self._hass.loop.call_soon(self._evaluate)
            return
        session.camera_failed = False
        self._apply_camera(session, entity_id, answer.vehicle_id, answer.confidence, answer.now)

    @callback
    def _apply_camera(
        self, session: _Session, entity_id: str | None, answer: str | None, confidence: str | None, now: Signature
    ) -> None:
        """The camera's answer, applied only while nothing has decided and no one has answered."""
        record = {"entity_id": entity_id, "answer": answer or "none", "confidence": confidence, "used": False}
        session.camera_record = record
        if self._session is not session or session.state != STATE_WAITING or not session.listening:
            return
        if self._connected is False:
            return
        evidence = self._evidence(session, dt_util.utcnow())
        remaining = [item.vehicle_id for item in evidence if item.negative is None]
        verdict = camera_verdict(
            answer, confidence, remaining, self._camera.reference_signatures(remaining), now
        )
        if verdict.prefers is None:
            return
        record["used"] = True
        if verdict.decides is not None:
            self._settle(verdict.decides, METHOD_CAMERA, "recognised")
            # A car's own report still outranks the camera while the plug-in is listened to (`_correct_camera`).
            session.listening = True
            return
        session.camera_pick = verdict.prefers
        session.order = ordered(evidence, self._current_vehicle(), session.camera_pick)

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

    def answer_refusal(self, vehicle_id: Any) -> str | None:
        """Why a person's choice of `vehicle_id` cannot be taken now: `REFUSED_NOT_PLUGGED_IN` without a car
        plugged in, `REFUSED_NOT_HERE` for a car that cannot charge at this charger, else `None`."""
        if self._connected is not True:
            return REFUSED_NOT_PLUGGED_IN
        cars = [car for car, _ in self._candidates(self._settings())]
        if not isinstance(vehicle_id, str) or vehicle_id not in cars:
            return REFUSED_NOT_HERE
        return None

    async def async_answer(self, vehicle_id: Any) -> bool:
        """A person's answer from a phone, the card or the app, or a correction of a car already decided: taken
        whenever a car is plugged in and it is one of this charger's cars (`answer_refusal`), as `answered`, and it
        holds for the car's stay like an answer to the question. An open question is replaced silently."""
        if self.answer_refusal(vehicle_id) is not None:
            return False
        session = self._session
        retire_on: tuple[str, ...] = ()
        if session is None or vehicle_id not in session.cars:
            # Nothing was being identified (identification off, one car known, decided before a restart), or the
            # car came after the question did: the correction is the plug-in's decision, kept as an answer would
            # be. An open question goes off the phones it was sent to, as for any answer.
            if session is not None and session.state == STATE_ASKING:
                retire_on = session.phones
            settings = self._settings()
            cars = self._candidates(settings)
            self._close()
            self._session = _Session(
                t0=dt_util.utcnow(), mode=settings.identify_mode, cars=tuple(car for car, _ in cars),
                names=dict(cars), order=[car for car, _ in cars], state=STATE_DECIDED, listening=False,
            )
            if retire_on and self._notifier is not None:
                name = self._session.names.get(vehicle_id, vehicle_id)
                self._notifier.retire_vehicle_question(self.tag, retire_on, "chosen", name)
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
