"""The power a car really draws at the charger's planned current, learned from its charges.

The plan counts on the charger's current times its phases (`planner.power_kw`). A car whose onboard charger takes
less (a 7.4 kW charger on an 11 kW wallbox, a car that limits itself in the cold) delivers less per quarter-hour, and
the plan buys too few of them. This module learns what it really draws and the planner counts on the lower of the
two (`PlanRequest.power_cap_kw`). No setting.

* **What is learned from.** Minutes of a plan's own charge where nothing SpotNav did held the current back
  (`clean_minute`): a window of the installed Auto plan open and the plan expecting the charge
  (`ChargingController.plan_expects_charge`: no balancing pause, no hold, the car has not ended it), the charger
  charging and the car not ending the charge, past the window's first two minutes (a car waking up), the setpoint and
  the current SpotNav asked for both the plan's current, load balancing not limiting
  it, no phase switch settling or forced, the strategy not solar, and the car below `TAPER_PERCENT` where its level
  is known (above it a car tapers by itself). The energy is the count the need uses (the energy register, or the
  energy integrated from the charger's power), read once a minute (`PlanWatch`); a minute counts when it and the
  minute before were both clean.
* **Per what.** Per vehicle when the charger knows which one it plans for (`vehicle_properties.resolved_vehicle_id`),
  else per charger. Each keeps the last `MAX_SESSIONS` plug-ins as (minutes, kWh); a plug-in is the last one the
  charger saw (`ChargingController.plugged_in_at`, kept across a restart), so a restart does not start another.
* **Smoothed and bounded.** The median of the plug-ins' own powers (each with at least `MIN_SESSION_MINUTES`),
  bounded to [the power at the charger's least current, the planned power]. The least current is taken on one phase
  while the car's onboard phases are not known (a one-phase car on three-phase wiring draws below the three-phase
  floor).
* **Trusted.** Only with `TRUST_MINUTES` minutes over at least `TRUST_SESSIONS` plug-ins; until then the plan
  counts on the planned power, as before.
* **Reset.** What is learned is for one wiring: the phases the charge uses, the voltage between phases and the
  planned current. When any of them changes, the subject starts again (a value learned at 10 A says nothing about
  16 A, and a car that moved from one phase to three draws something else).
* **Stored** per charger (`spotnav.learned_power.<entry>`), and read as untrusted: anything malformed is dropped.

Learned charging efficiency (pairs of real levels around register energy) is not here; the fixed
`CHARGE_EFFICIENCY` stands.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION: Final = 1
_STORE_PREFIX: Final = f"{DOMAIN}.learned_power"

#: The minutes and plug-ins before a learned power is planned with.
TRUST_MINUTES: Final = 20.0
TRUST_SESSIONS: Final = 2
#: The plug-ins kept per subject, and the least minutes one needs to give a power of its own.
MAX_SESSIONS: Final = 5
MIN_SESSION_MINUTES: Final = 5.0
#: Above this level (where it is known) a car tapers by itself: nothing is learned.
TAPER_PERCENT: Final = 90.0
#: The longest gap between two readings one stretch may span, in seconds (they are taken once a minute); a longer
#: one (a restart, a busy loop) counts nothing.
MAX_STEP_S: Final = 400.0
#: How long, in seconds, after a write the store waits to save.
SAVE_DELAY_S: Final = 30.0

SUBJECT_CHARGER: Final = "charger"


def subject_for(vehicle_id: str | None) -> str:
    """What one learned value is for: the vehicle when known, else the charger."""
    return f"vehicle:{vehicle_id}" if vehicle_id else SUBJECT_CHARGER


@dataclass(frozen=True, slots=True)
class Wiring:
    """What a learned value is valid for: a change of any of these starts it again."""

    phases: int
    voltage_v: float
    amps: int

    def as_dict(self) -> dict[str, Any]:
        return {"phases": self.phases, "voltage_v": self.voltage_v, "amps": self.amps}

    @staticmethod
    def from_stored(raw: Any) -> Wiring | None:
        if not isinstance(raw, dict):
            return None
        phases, voltage, amps = raw.get("phases"), raw.get("voltage_v"), raw.get("amps")
        if phases not in (1, 3) or isinstance(phases, bool):
            return None
        if not _finite_positive(voltage) or isinstance(amps, bool) or not isinstance(amps, int) or amps <= 0:
            return None
        return Wiring(phases=phases, voltage_v=float(voltage), amps=amps)


@dataclass(frozen=True, slots=True)
class Session:
    """One plug-in's clean minutes and the energy delivered in them."""

    id: str
    minutes: float
    kwh: float

    @property
    def kw(self) -> float | None:
        return None if self.minutes < MIN_SESSION_MINUTES else self.kwh / (self.minutes / 60.0)

    @staticmethod
    def from_stored(raw: Any) -> Session | None:
        if not isinstance(raw, dict):
            return None
        ident, minutes, kwh = raw.get("id"), raw.get("minutes"), raw.get("kwh")
        if not isinstance(ident, str) or not ident or len(ident) > 128:
            return None
        if not _finite_positive(minutes) or not _finite(kwh) or float(kwh) < 0:
            return None
        return Session(id=ident, minutes=float(minutes), kwh=float(kwh))


@dataclass(frozen=True, slots=True)
class LearnedPower:
    """What is known of one subject's power, as the planner and the diagnostics read it."""

    subject: str
    #: The bounded median, or `None` with no plug-in long enough.
    kw: float | None
    minutes: float
    sessions: int
    trusted: bool

    @property
    def cap_kw(self) -> float | None:
        """What the planner counts on: the learned power once trusted, else nothing."""
        return self.kw if self.trusted else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "kw": None if self.kw is None else round(self.kw, 3),
            "minutes": round(self.minutes, 1),
            "sessions": self.sessions,
            "trusted": self.trusted,
        }


def learned_from(subject: str, sessions: list[Session], low_kw: float, high_kw: float) -> LearnedPower:
    """The median of the plug-ins' powers bounded to [low_kw, high_kw], and whether it is trusted."""
    powers = [kw for kw in (session.kw for session in sessions) if kw is not None]
    minutes = sum(session.minutes for session in sessions)
    kw = None
    if powers:
        kw = min(max(statistics.median(powers), low_kw), high_kw) if high_kw >= low_kw else high_kw
    trusted = kw is not None and minutes >= TRUST_MINUTES and len(powers) >= TRUST_SESSIONS
    return LearnedPower(subject=subject, kw=kw, minutes=minutes, sessions=len(powers), trusted=trusted)


class LearnedPowerStore:
    """One charger's learned powers, per subject, kept across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_PREFIX}.{entry_id}")
        self._subjects: dict[str, tuple[Wiring, list[Session]]] = {}

    async def async_load(self) -> None:
        """Read what was stored; anything that is not exactly what this writes is dropped. A file that cannot be read
        at all (corrupt, or written by a later version) never stops the charger: it starts with nothing learned."""
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a learned figure is never worth a charger that does not start
            _LOGGER.warning("SpotNav: the learned car power could not be read (%s); learning again", type(err).__name__)
            raw = None
        self._subjects = parse_stored(raw)

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: what was learned at it goes too."""
        await Store(hass, _STORE_VERSION, f"{_STORE_PREFIX}.{entry_id}").async_remove()

    def power(self, subject: str, wiring: Wiring, low_kw: float, high_kw: float) -> LearnedPower:
        """What is learned for `subject` on this wiring (nothing when it was learned on another)."""
        held = self._subjects.get(subject)
        sessions = held[1] if held is not None and held[0] == wiring else []
        return learned_from(subject, sessions, low_kw, high_kw)

    def add(self, subject: str, wiring: Wiring, session_id: str, kwh: float, minutes: float) -> None:
        """Count `minutes` clean minutes delivering `kwh` toward `session_id`; another wiring starts again."""
        if not (_finite(kwh) and _finite_positive(minutes)) or kwh < 0:
            return
        held = self._subjects.get(subject)
        sessions = list(held[1]) if held is not None and held[0] == wiring else []
        if sessions and sessions[-1].id == session_id:
            last = sessions[-1]
            sessions[-1] = Session(id=session_id, minutes=last.minutes + minutes, kwh=last.kwh + kwh)
        else:
            sessions.append(Session(id=session_id, minutes=minutes, kwh=kwh))
        self._subjects[subject] = (wiring, sessions[-MAX_SESSIONS:])
        self._store.async_delay_save(self._to_store, SAVE_DELAY_S)

    def _to_store(self) -> dict[str, Any]:
        return {
            "subjects": {
                subject: {
                    "wiring": wiring.as_dict(),
                    "sessions": [{"id": s.id, "minutes": s.minutes, "kwh": s.kwh} for s in sessions],
                }
                for subject, (wiring, sessions) in self._subjects.items()
            }
        }


@dataclass(frozen=True, slots=True)
class PowerSample:
    """One clean minute's reading of the count, and what it is learned for."""

    at: datetime
    kwh: float
    session: str
    subject: str
    wiring: Wiring


class PowerSampler:
    """Turns readings taken once a minute into clean minutes for the store: a stretch counts when it begins and
    ends on a clean reading of the same plug-in, subject and wiring, at most `MAX_STEP_S` apart."""

    def __init__(self, store: LearnedPowerStore) -> None:
        self._store = store
        self._last: PowerSample | None = None

    def sample(self, sample: PowerSample | None, high_kw: float) -> None:
        """Take one reading (`None`: this minute is not clean, and the stretch ends)."""
        last, self._last = self._last, sample
        if sample is None or last is None:
            return
        if (last.session, last.subject, last.wiring) != (sample.session, sample.subject, sample.wiring):
            return
        seconds = (sample.at - last.at).total_seconds()
        if not 0 < seconds <= MAX_STEP_S:
            return
        kwh = sample.kwh - last.kwh
        # A register that went back, or jumped further than three times the planned power could deliver: nothing.
        if kwh < 0 or kwh > 3 * high_kw * seconds / 3600.0 + 1.0:
            return
        self._store.add(sample.subject, sample.wiring, sample.session, kwh, seconds / 60.0)


def parse_stored(raw: Any) -> dict[str, tuple[Wiring, list[Session]]]:
    """The stored subjects that are well formed; a malformed subject or plug-in is left out."""
    subjects = raw.get("subjects") if isinstance(raw, dict) else None
    parsed: dict[str, tuple[Wiring, list[Session]]] = {}
    if not isinstance(subjects, dict):
        return parsed
    for subject, item in subjects.items():
        if not isinstance(subject, str) or not subject or not isinstance(item, dict):
            continue
        wiring = Wiring.from_stored(item.get("wiring"))
        stored_sessions = item.get("sessions")
        if wiring is None or not isinstance(stored_sessions, list):
            continue
        sessions = [s for s in (Session.from_stored(entry) for entry in stored_sessions) if s is not None]
        if sessions:
            parsed[subject] = (wiring, sessions[-MAX_SESSIONS:])
    return parsed


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite_positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0
