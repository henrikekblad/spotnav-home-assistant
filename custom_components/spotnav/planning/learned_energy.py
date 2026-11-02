"""How much energy from the wall one percent of the car's battery takes, learned from its charges.

A target becomes a wall-energy need as `capacity x (target - level) / 100 / CHARGE_EFFICIENCY`. A battery size
that is off (a degraded pack, net against gross, a guess) or an efficiency far from the fixed one (a cold pack,
one phase, a low current) makes the plan buy too little time or too much. From the energy count and the car's
level before and after a stretch of charging only one number can be seen: wall kWh per percent
(`capacity / efficiency / 100`). That is what is learned here, per car; the planner needs nothing else. No setting.

* **A sample.** A stretch of one plug-in (`ChargingController.plugged_in_at`, kept across a restart, else
  `plugged_in_for_count`) of one identified car (the one the level is read from) between two fresh readings of its
  level from one source (`charger` or `vehicle`): not an estimate and no older than
  `target_stop.SOC_FRESH_MAX_AGE_S` when seen, the later one made after the earlier. The energy is the count the
  need uses (the energy register, or the energy integrated from the charger's power) at the moment each reading
  was made: interpolated between the register's posts while it posts steadily around it
  (`soc_estimate.register_value_at`), or, only while the charger is not charging, the value it held from a look
  before the reading to now. The stretch counts once the level rose at least `MIN_DELTA_PERCENT` points; its
  later end moves on with each fresh reading of the same plug-in, so a charge that spans several windows of a plan
  is one sample. The charger saying the car is unplugged, another plug-in, another car (or none identified),
  another source of the level, a register that went back or a level that fell starts a stretch again. The
  stretch's start is stored, so a restart during a plug-in keeps it.
* **Bounds.** With the car's battery size the sample must mean an efficiency between `MIN_EFFICIENCY` and
  `MAX_EFFICIENCY`; without one, a battery between `MIN_CAPACITY_KWH` and `MAX_CAPACITY_KWH` at
  `CHARGE_EFFICIENCY`. A sample outside is not kept and is counted (`discarded`). Every read counts only the
  samples that fit today's battery size; the need's read drops those that no longer fit for good (and counts
  them), the others only look.
* **Smoothed and trusted.** From the last `MAX_SAMPLES` samples of ended plug-ins (the open plug-in's own, still
  growing, waits until it ends): trusted when at least `TRUST_SAMPLES` of them lie within `TRUST_SPREAD` of their
  median, and then the figure is the median of those that agree, so one odd charge neither moves it nor takes the
  trust away. Until then everything is worked out as before.
* **Used** for a target's need and the dashboard's (`target_need`, `efficiency_for`), and for the level carried
  forward between readings (`SocReader`, given the figure from setup), so a replan from an estimate and the stop
  on one count as the need does.
* **Fed** once a minute by the look at the plan's windows (`AutoPlannerController._check_plan`, the seam the
  learned power and the behind-plan rule use), so only charges of an Auto plan are learned from.
* **Stored** per charger (`spotnav.learned_energy.<entry>`), keyed by car, and read as untrusted: anything
  malformed is dropped, and a file that cannot be read starts empty.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..execution.target_stop import SOC_FRESH_MAX_AGE_S
from ..vehicles.soc_estimate import CHARGE_EFFICIENCY, register_value_at, REGISTER_TOLERANCE_KWH, target_need_kwh

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION: Final = 1
_STORE_PREFIX: Final = f"{DOMAIN}.learned_energy"

#: The least rise of the level (points) a stretch needs to be a sample: whole-percent readings make less too rough.
MIN_DELTA_PERCENT: Final = 10.0
#: The samples kept per car, and how many (all within `TRUST_SPREAD` of their median) before the need uses them.
MAX_SAMPLES: Final = 5
TRUST_SAMPLES: Final = 3
TRUST_SPREAD: Final = 0.10
#: What a sample may mean: an efficiency in this range for the car's battery size, or without one a battery size in
#: this range at `CHARGE_EFFICIENCY`.
MIN_EFFICIENCY: Final = 0.70
MAX_EFFICIENCY: Final = 1.0
MIN_CAPACITY_KWH: Final = 10.0
MAX_CAPACITY_KWH: Final = 200.0
#: The samples stored per car: the last `MAX_SAMPLES` of ended plug-ins and the open one's.
_STORED_SAMPLES: Final = MAX_SAMPLES + 1
#: How far back (s) the register's values are kept (in memory) to read its value when a level was made.
HISTORY_S: Final = 1800.0
#: How long, in seconds, after a write the store waits to save.
SAVE_DELAY_S: Final = 30.0
#: Bounds on what is stored: identifiers, and the count of discarded samples.
_MAX_ID_LEN: Final = 128
_MAX_DISCARDED: Final = 1_000_000


def fits(kwh_per_percent: float, capacity_kwh: float | None) -> bool:
    """Whether wall kWh per percent is believable for a battery of `capacity_kwh` (or of any size, without one)."""
    if not _finite_positive(kwh_per_percent):
        return False
    if capacity_kwh is None or not _finite_positive(capacity_kwh):
        implied_capacity = kwh_per_percent * 100.0 * CHARGE_EFFICIENCY
        return MIN_CAPACITY_KWH <= implied_capacity <= MAX_CAPACITY_KWH
    efficiency = capacity_kwh / (kwh_per_percent * 100.0)
    return MIN_EFFICIENCY <= efficiency <= MAX_EFFICIENCY


@dataclass(frozen=True, slots=True)
class EnergySample:
    """One stretch: the wall energy it took and the points the level rose, for one plug-in (`id`)."""

    id: str
    kwh: float
    delta_percent: float
    at: datetime

    @property
    def kwh_per_percent(self) -> float:
        return self.kwh / self.delta_percent

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kwh": self.kwh, "delta_percent": self.delta_percent, "at": self.at.isoformat()}

    @staticmethod
    def from_stored(raw: Any) -> EnergySample | None:
        if not isinstance(raw, dict):
            return None
        ident, kwh, delta, at = raw.get("id"), raw.get("kwh"), raw.get("delta_percent"), _when(raw.get("at"))
        if not _ident(ident) or at is None or not _finite_positive(kwh) or not _finite(delta):
            return None
        if not MIN_DELTA_PERCENT <= float(delta) <= 100.0:
            return None
        return EnergySample(id=ident, kwh=float(kwh), delta_percent=float(delta), at=at)


@dataclass(frozen=True, slots=True)
class Anchor:
    """A stretch's start: the car, the plug-in, the level and when it was made, and the count then."""

    vehicle_id: str
    session: str
    soc_percent: float
    at: datetime
    kwh: float
    #: Where the level was read (`charger` or `vehicle`, `SocReading.source`): another source starts again.
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "session": self.session,
            "soc_percent": self.soc_percent,
            "at": self.at.isoformat(),
            "kwh": self.kwh,
            "source": self.source,
        }

    @staticmethod
    def from_stored(raw: Any) -> Anchor | None:
        if not isinstance(raw, dict):
            return None
        vehicle, session, soc, kwh = raw.get("vehicle_id"), raw.get("session"), raw.get("soc_percent"), raw.get("kwh")
        at = _when(raw.get("at"))
        if not _ident(vehicle) or not _ident(session) or at is None or not _finite(soc) or not _finite(kwh):
            return None
        if not 0.0 <= float(soc) <= 100.0 or float(kwh) < 0:
            return None
        source = raw.get("source")
        if source is not None and not _ident(source):
            return None
        return Anchor(
            vehicle_id=vehicle, session=session, soc_percent=float(soc), at=at, kwh=float(kwh), source=source
        )


@dataclass(frozen=True, slots=True)
class LearnedEnergy:
    """What is known of one car's wall kWh per percent, as the planner and the diagnostics read it."""

    vehicle_id: str | None
    #: The median of the samples, or `None` with none.
    kwh_per_percent: float | None
    samples: tuple[EnergySample, ...]
    trusted: bool
    discarded: int = 0
    #: The open plug-in's own sample, not counted until the plug-in ends.
    pending: EnergySample | None = None

    @property
    def use_kwh_per_percent(self) -> float | None:
        """What the need counts on: the learned figure once trusted, else nothing (the battery size and the fixed
        efficiency, as before)."""
        return self.kwh_per_percent if self.trusted else None

    def as_dict(self) -> dict[str, Any]:
        # No car's identity (the diagnostics name none), and the samples keyed oldest first (no lists of data).
        return {
            "for_car": self.vehicle_id is not None,
            "kwh_per_percent": None if self.kwh_per_percent is None else round(self.kwh_per_percent, 4),
            "samples": {
                str(index): {"kwh": round(s.kwh, 3), "delta_percent": round(s.delta_percent, 1), "at": s.at.isoformat()}
                for index, s in enumerate(self.samples, start=1)
            },
            "pending_sample": None if self.pending is None else {
                "kwh": round(self.pending.kwh, 3),
                "delta_percent": round(self.pending.delta_percent, 1),
                "at": self.pending.at.isoformat(),
            },
            "trusted": self.trusted,
            "discarded": self.discarded,
        }


NOTHING_LEARNED: Final = LearnedEnergy(vehicle_id=None, kwh_per_percent=None, samples=(), trusted=False)


def learned_from(
    vehicle_id: str | None, samples: list[EnergySample], discarded: int = 0, open_session: str | None = None
) -> LearnedEnergy:
    """From the last `MAX_SAMPLES` samples of ended plug-ins (the open plug-in's own, still growing, is left out until
    it ends): trusted when at least `TRUST_SAMPLES` of them lie within `TRUST_SPREAD` of their median, and then the
    figure is the median of those that agree, so one odd charge neither moves it nor takes the trust away."""
    pending = next((sample for sample in samples if sample.id == open_session), None) if open_session else None
    kept = [sample for sample in samples if sample is not pending][-MAX_SAMPLES:]
    if not kept:
        return LearnedEnergy(vehicle_id, None, (), False, discarded, pending)
    median = statistics.median(sample.kwh_per_percent for sample in kept)
    agreeing = [
        sample.kwh_per_percent for sample in kept if abs(sample.kwh_per_percent - median) <= TRUST_SPREAD * median
    ]
    trusted = len(agreeing) >= TRUST_SAMPLES
    figure = statistics.median(agreeing) if trusted else median
    return LearnedEnergy(vehicle_id, figure, tuple(kept), trusted, discarded, pending)


@dataclass(slots=True)
class _Car:
    samples: list[EnergySample]
    discarded: int = 0
    #: The last plug-in whose sample was discarded, so a stretch that stays out of bounds is counted once.
    discarded_session: str | None = None


class LearnedEnergyStore:
    """One charger's learned wall kWh per percent, per car, and the open stretch's start, kept across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_PREFIX}.{entry_id}")
        self._cars: dict[str, _Car] = {}
        self.anchor: Anchor | None = None
        #: The plug-in (car, plug-in) last seen going on: its sample is not counted until it ends.
        self.open: tuple[str, str] | None = None

    async def async_load(self) -> None:
        """Read what was stored; anything that is not exactly what this writes is dropped. A file that cannot be read
        at all (corrupt, or written by a later version) never stops the charger: it starts with nothing learned."""
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a learned figure is never worth a charger that does not start
            _LOGGER.warning(
                "SpotNav: the learned energy per percent could not be read (%s); learning again", type(err).__name__
            )
            raw = None
        self._cars, self.anchor, self.open = parse_stored(raw)

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The charger is gone for good: what was learned at it goes too."""
        await Store(hass, _STORE_VERSION, f"{_STORE_PREFIX}.{entry_id}").async_remove()

    def learned(self, vehicle_id: str | None, capacity_kwh: float | None, *, drop: bool = False) -> LearnedEnergy:
        """What is learned for this car, counting only the samples that fit today's battery size; nothing without an
        identified car. With `drop` (the planner's need) those that no longer fit are dropped for good and counted;
        without it (the estimate, the dashboard, the diagnostics) this only looks."""
        if not vehicle_id:
            return NOTHING_LEARNED
        car = self._cars.get(vehicle_id)
        if car is None:
            return learned_from(vehicle_id, [])
        fitting = [sample for sample in car.samples if fits(sample.kwh_per_percent, capacity_kwh)]
        discarded = min(_MAX_DISCARDED, car.discarded + len(car.samples) - len(fitting))
        if drop and len(fitting) != len(car.samples):
            car.discarded, car.samples = discarded, fitting
            self._save()
        open_session = self.open[1] if self.open is not None and self.open[0] == vehicle_id else None
        return learned_from(vehicle_id, fitting, discarded, open_session)

    def put(self, vehicle_id: str, sample: EnergySample, capacity_kwh: float | None) -> bool:
        """Keep `sample` as its plug-in's sample (replacing that plug-in's shorter one), or, when it is out of
        bounds, drop that plug-in's sample and count it once. Returns whether it was kept."""
        car = self._cars.setdefault(vehicle_id, _Car(samples=[]))
        others = [kept for kept in car.samples if kept.id != sample.id]
        if not fits(sample.kwh_per_percent, capacity_kwh):
            if car.discarded_session != sample.id:
                car.discarded_session = sample.id
                car.discarded = min(_MAX_DISCARDED, car.discarded + 1)
            car.samples = others
            self._save()
            return False
        if car.discarded_session == sample.id:
            # The stretch came back within bounds as it grew: it was not a bad one after all.
            car.discarded_session = None
            car.discarded = max(0, car.discarded - 1)
        car.samples = [*others, sample][-_STORED_SAMPLES:]
        self._save()
        return True

    def set_anchor(self, anchor: Anchor | None) -> None:
        if anchor != self.anchor:
            self.anchor = anchor
            self._save()

    def set_open(self, plug_in: tuple[str, str] | None) -> None:
        if plug_in != self.open:
            self.open = plug_in
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(self._to_store, SAVE_DELAY_S)

    def _to_store(self) -> dict[str, Any]:
        return {
            "vehicles": {
                vehicle_id: {
                    "samples": [sample.as_dict() for sample in car.samples],
                    "discarded": car.discarded,
                    "discarded_session": car.discarded_session,
                }
                for vehicle_id, car in self._cars.items()
            },
            "anchor": None if self.anchor is None else self.anchor.as_dict(),
            "open": None if self.open is None else {"vehicle_id": self.open[0], "session": self.open[1]},
        }


@dataclass(frozen=True, slots=True)
class SocSeen:
    """The car's level as one look sees it: the percentage, its age (s), whether it is an estimate, and where it was
    read (`charger` or `vehicle`)."""

    soc_percent: float | None
    age_s: float | None
    estimated: bool
    source: str | None = None

    @property
    def fresh(self) -> bool:
        return (
            self.soc_percent is not None
            and not self.estimated
            and self.age_s is not None
            and 0.0 <= self.age_s <= SOC_FRESH_MAX_AGE_S
        )


class EnergySampler:
    """Turns the once-a-minute looks into samples for the store (see the module docstring)."""

    def __init__(self, store: LearnedEnergyStore) -> None:
        self._store = store
        #: The register's values by when they were posted, and by when they were looked at.
        self._posts: dict[datetime, float] = {}
        self._looks: list[tuple[datetime, float]] = []

    def observe(
        self,
        now: datetime,
        *,
        vehicle_id: str | None,
        session: str | None,
        soc: SocSeen,
        register_kwh: float | None,
        register_posted_at: datetime | None,
        capacity_kwh: float | None,
        charging: bool = True,
    ) -> None:
        """One look: what the car's level and the count say now. `session` is `None` with no plug-in known or the
        car unplugged; `charging` says whether the charger charges now (a value the register held is believed only
        while it does not)."""
        self._remember(now, register_kwh, register_posted_at)
        anchor = self._store.anchor
        if not session:
            # Unplugged, or no plug-in known: the stretch ends, and so does the open plug-in.
            self._store.set_anchor(None)
            self._store.set_open(None)
            return
        if not vehicle_id:
            # No car identified: an unidentified car is not one car.
            self._store.set_anchor(None)
            return
        self._store.set_open((vehicle_id, session))
        if anchor is not None and (anchor.vehicle_id, anchor.session, anchor.source) != (
            vehicle_id, session, soc.source,
        ):
            anchor = None  # a plug-out, another car, or the level read elsewhere: the stretch starts again
        if anchor is not None and register_kwh is not None and register_kwh < anchor.kwh - REGISTER_TOLERANCE_KWH:
            anchor = None  # the count went back (a reset, a meter replaced)
        if register_kwh is None or not soc.fresh:
            self._store.set_anchor(anchor)
            return
        assert soc.soc_percent is not None and soc.age_s is not None
        made_at = now - timedelta(seconds=soc.age_s)
        kwh = self._value_at(made_at, now, register_kwh, charging=charging)
        if kwh is None:
            self._store.set_anchor(anchor)
            return
        if anchor is None or soc.soc_percent < anchor.soc_percent or kwh < anchor.kwh - REGISTER_TOLERANCE_KWH:
            self._store.set_anchor(Anchor(vehicle_id, session, soc.soc_percent, made_at, kwh, soc.source))
            return
        self._store.set_anchor(anchor)
        if made_at <= anchor.at:
            return  # the reading the stretch starts from, seen again
        delta = soc.soc_percent - anchor.soc_percent
        energy = kwh - anchor.kwh
        if delta >= MIN_DELTA_PERCENT and energy > 0:
            self._store.put(vehicle_id, EnergySample(session, energy, delta, made_at), capacity_kwh)

    def _remember(self, now: datetime, register_kwh: float | None, posted_at: datetime | None) -> None:
        horizon = now - timedelta(seconds=HISTORY_S)
        if register_kwh is not None:
            self._posts[posted_at or now] = register_kwh
            self._looks.append((now, register_kwh))
        self._posts = {at: value for at, value in self._posts.items() if at >= horizon}
        self._looks = [(at, value) for at, value in self._looks if at >= horizon]

    def _value_at(self, made_at: datetime, now: datetime, register_kwh: float, *, charging: bool) -> float | None:
        """The count when the level was made: interpolated between the register's posts while it posts steadily,
        else, while the charger is not charging, the value it stood at from a look at or before then to now (a
        register that books in chunks while charging would skew it); `None` when neither is known."""
        value = register_value_at(self._posts.items(), made_at, now)
        if value is not None:
            return value
        if charging:
            return None
        before = [kwh for at, kwh in self._looks if at <= made_at]
        if before and abs(before[-1] - register_kwh) <= REGISTER_TOLERANCE_KWH:
            # A count that only rises stood still from that look to now, so it held this value when the level was
            # made.
            return before[-1]
        return None


def parse_stored(raw: Any) -> tuple[dict[str, _Car], Anchor | None, tuple[str, str] | None]:
    """The stored cars, the open stretch's start and the open plug-in that are well formed; anything malformed is
    left out."""
    cars: dict[str, _Car] = {}
    if not isinstance(raw, dict):
        return cars, None, None
    vehicles = raw.get("vehicles")
    if isinstance(vehicles, dict):
        for vehicle_id, item in vehicles.items():
            if not _ident(vehicle_id) or not isinstance(item, dict):
                continue
            stored = item.get("samples")
            if not isinstance(stored, list):
                continue
            samples = [s for s in (EnergySample.from_stored(entry) for entry in stored) if s is not None]
            discarded = item.get("discarded")
            count = int(discarded) if _finite(discarded) and 0 <= discarded <= _MAX_DISCARDED else 0
            session = item.get("discarded_session")
            cars[vehicle_id] = _Car(
                samples=samples[-_STORED_SAMPLES:], discarded=count, discarded_session=session if _ident(session) else None
            )
    stored_open = raw.get("open")
    plug_in = None
    if isinstance(stored_open, dict) and _ident(stored_open.get("vehicle_id")) and _ident(stored_open.get("session")):
        plug_in = (stored_open["vehicle_id"], stored_open["session"])
    return cars, Anchor.from_stored(raw.get("anchor")), plug_in


def with_learned(learned: LearnedEnergy, capacity_kwh: float | None) -> float | None:
    """The battery size the need is worked out with: one that, divided by `CHARGE_EFFICIENCY`, takes the learned wall
    kWh per percent once trusted, else `capacity_kwh` as it is."""
    ratio = learned.use_kwh_per_percent
    return capacity_kwh if ratio is None else ratio * 100.0 * CHARGE_EFFICIENCY


def target_need(
    learned: LearnedEnergy,
    *,
    soc_percent: float,
    capacity_kwh: float | None,
    target_percent: float | None,
    vehicle_max_percent: float | None = None,
) -> tuple[str, float | None]:
    """`(reason, wall kWh)` a target needs: `soc_estimate.target_need_kwh` on the car's learned wall kWh per percent
    once trusted, else on its battery size and `CHARGE_EFFICIENCY`. The one way a target becomes kWh, for the planner
    and the dashboard alike, so they never disagree."""
    return target_need_kwh(
        soc_percent=soc_percent,
        capacity_kwh=with_learned(learned, capacity_kwh),
        target_percent=target_percent,
        vehicle_max_percent=vehicle_max_percent,
    )


def efficiency_for(learned: LearnedEnergy, capacity_kwh: float | None) -> float:
    """The efficiency that, with `capacity_kwh`, gives the learned wall kWh per percent once trusted (what the
    dashboard's `soc.efficiency` states, so the card's `(target - level) x capacity / efficiency` is `target_need`'s
    figure), else `CHARGE_EFFICIENCY`. Within (0, 1] by the bounds; anything else falls back."""
    ratio = learned.use_kwh_per_percent
    if ratio is None or capacity_kwh is None or not _finite_positive(capacity_kwh):
        return CHARGE_EFFICIENCY
    efficiency = capacity_kwh / (ratio * 100.0)
    return efficiency if 0.0 < efficiency <= MAX_EFFICIENCY else CHARGE_EFFICIENCY


def _ident(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_ID_LEN


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    parsed = dt_util.parse_datetime(value)
    return None if parsed is None or parsed.tzinfo is None else parsed


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite_positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0

