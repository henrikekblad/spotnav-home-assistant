"""The house's own current per phase, learned: its hourly peaks of the last `WINDOW_DAYS` days (SpotNav 3.0).

What it is for: the home battery's grid charge cap (`battery/gate.py` `grid_charge_cap_kw`), which must keep every
phase under the main fuse even if Home Assistant stops while a grid charge runs (Sigenergy has no dead-man: the
command stays). Without a per-phase history the cap takes the house's peak as `max(p90, 4 kW)` all on one phase,
which on a 20 A fuse leaves no grid charge at all. With one, each phase's own learned peak counts.

**What is measured** (`SitePhasePeaks.sample`, every `SAMPLE_S` from what the site already holds; no service call.
The days before SpotNav kept any are read back once from Home Assistant's recorder and run through the very same
reading, `site/phase_backfill.py`): per phase, the site meter's current, signed as load balancing takes it (its magnitude, negative
where the phase's active power is known to export), less each charger's own measured current on that phase, less
the home battery's share:

* **The plant's own per-phase power** (exact). Where the battery's integration states the plant's AC power on each
  phase (`battery/adapters.py` `plant_phase_power`, Sigenergy's `plant_phase_{a,b,c}_active_power`; positive for
  output, which on a hybrid inverter is the sun's power and the battery's together) and all three readings are
  fresh (reported within the site's maximum age), the house's current on a phase is exactly the meter's less the
  chargers' plus the plant's output there: house = meter - chargers + plant / V. Every minute counts, whatever the
  battery and the sun do, and the bias factor below does not apply to those minutes. A sample whose house reads
  below `-PLANT_MISMATCH_A` on a phase does not count this way (the plant's phases A, B, C are taken as the meter's
  L1, L2, L3; a plant wired otherwise would show so) and falls to the rule below.
* **The battery** (without the plant's per-phase power, or with it stale or unknown). Its split over the phases is not known (a SigenStor can deliver unbalanced), so only a sample
  with the battery's power within `BATTERY_BAND_W` (0.5 kW) of zero counts per phase, and there it is counted the way
  that makes the house largest: its whole discharge on every phase, none of its charge. A phase then never reads
  less than the house drew; it reads at most 0.5 kW / 230 V = 2.2 A more. Wider, the error grows with the battery's
  power and a large charge would read as house. A battery configured on the site whose power cannot be read live
  counts nothing; a site without one counts it as 0.
* **The chargers.** A charger's reading counts while it is fresh (reported within the site's maximum age); a stale
  one (an OCPP charger repeats its last current all night after a pause) counts 0, so the car is then counted in the
  house (larger, never smaller). A single-phase charger takes its largest reading on its chosen phase; one whose phase
  is not known ("Automatic") makes a sample not count while it draws. A minute with a charger's current changing by
  more than `CHARGER_STEP_A`, and the minutes on either side of it, do not count (a charger's report lags the meter:
  a car starting would read as the house for those seconds).
* **The measurement.** Only a sample on the site's usable measurement counts (the site `observing`, every phase
  read): a stale or missing phase is no sample.

**Kept** per clock hour (UTC): per phase the largest 1-minute mean (a minute counts with at least `MIN_SAMPLES`
samples, every one of them counting), the minutes counted, and two figures for the bias below; `WINDOW_DAYS` days,
in the site's own store (bounded, read as untrusted: anything malformed is dropped).

**What the cap uses** (`learned`): once `MIN_READY_DAYS` days in the window each have at least `DAY_MINUTES` counted
minutes (either way), per phase **the largest hourly peak in the whole window** (not a quantile: the minutes that count are biased
toward the battery standing idle, mostly night, so the rarest peaks are the ones to keep; a 1-minute mean already
leaves out a motor's start), scaled up by the **bias factor** and plus `MARGIN_A`. The bias factor is how much larger
the house's whole load (its current summed over the phases, known exactly whatever the battery's split) got in every
minute than in the minutes that counted (at least 1): a house whose battery covers its evening peaks has those peaks
reckoned at that ratio. The ratio alone would take the hidden load as spread like the load seen, and a phase that
is quiet while the battery idles (the oven's, the heat pump's immersion heater's) would be scaled from next to nothing.
So each phase is also reckoned at its own peak plus **the whole hidden load** (the difference of those two sums, in
amps, as if all of it sat on that phase), and the larger of the two counts. This bound was chosen over skipping the
early stage for a large factor: it holds at every stage and rests on no threshold, and with the plant's per-phase
power (exact minutes) it is not needed at all. Exact minutes are kept apart from these and count as they are, unscaled: the cap takes per
phase the larger of the two (each plus `MARGIN_A`); once `READY_DAYS` days each have `DAY_MINUTES` exact minutes,
the exact peaks alone (the battery's band no longer needed). Loads that back off by themselves (a heat pump's own
load monitor shedding its immersion heater) never peak on the meter, so they are left out as they should be.

**A cap that grows with the days kept** (`extra_margin_a`). Below `MIN_READY_DAYS` counted days nothing learned is
used: the conservative rule stays. From there to `READY_DAYS` the learned peaks count with a further margin on top of
`MARGIN_A`, `EXTRA_MARGIN_A` at `MIN_READY_DAYS` and falling in a straight line to nothing at `READY_DAYS`. The
learned peaks then replace the conservative rule altogether (the cap may be larger than that rule gave: that is the
point), so the numbers have to hold by themselves:

* What few days miss is a peak not yet seen. The cap does not take each phase's own peak: it takes the **largest
  phase's** peak as the house on every phase (`gate.grid_charge_cap_kw`), so a load that moves to another phase, or a
  heavy load on a phase that was quiet so far, is covered up to the largest phase's. What is left to miss is the
  largest phase's own peak growing. On the one house measured so far (11 days of November, heat pump, cooking, 20 A)
  that figure after one day was up to 5.1 A below the 11 days' largest, after two days at most 1.2 A, after three at
  most 1.1 A, after a week 0.3 A. One day is therefore never enough.
* `MIN_READY_DAYS` = 7: a whole week, so every weekday's habits (the weekend's cooking, the washing day) are in
  it once. One house's eleven days are no ground for fewer, and a week costs little: a site whose sensors have been
  in Home Assistant for a while has more than seven days at once from the recorder's history (ten by default).
* `EXTRA_MARGIN_A` = 4 A (0.9 kW on the phase, with `MARGIN_A` 6 A in all at seven days): over ten times what a
  week missed there. It is not sized for a large appliance never run in those days on top of the largest phase's
  peak at its worst minute; nor is `MARGIN_A` after two weeks. What covers that is that the peak kept is the
  window's largest minute on the largest phase with every other load of that minute in it, and that a main fuse
  carries an overload of that size for the minutes such a coincidence lasts.
* The days counted need `DAY_MINUTES` each as before, and the bias factor applies as before.

The sun: on a hybrid inverter the sun's power reaches the meter through the plant, so without the plant's per-phase
power it hides house load in the meter's reading while it shines. It only ever lowers what is seen, and the largest
peaks come from the hours without it, which every day has. With it, the sun is part of the plant's output and is
added back.

The heat pump's meter: where its sensor has per-phase siblings (`<sensor>_l1` .. `_l3`, power), their hourly peaks
are kept beside the house's, for the diagnostics only.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import math
from typing import TYPE_CHECKING, Any, Final

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S, DOMAIN
from .site_capacity import PHASES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .site_capacity_controller import SiteCapacityController

_LOGGER = logging.getLogger(__name__)

#: How often a sample is taken (s), and how many of a minute's must count for it to count.
SAMPLE_S: Final = 10.0
MIN_SAMPLES: Final = 4
#: The battery's power within which a sample counts per phase (W; see the module).
BATTERY_BAND_W: Final = 500.0
#: A charger whose current changes by more than this within a minute makes it, and its neighbours, not count (A).
CHARGER_STEP_A: Final = 1.0
#: A single-phase charger of unknown phase draws from this current on (A).
DRAWING_A: Final = 1.0
#: The window kept (days), the days with counted minutes before the learned peaks count, and the minutes a day needs.
WINDOW_DAYS: Final = 28
READY_DAYS: Final = 14
DAY_MINUTES: Final = 120
#: Added to each phase's learned peak (A).
MARGIN_A: Final = 2.0
#: The counted days from which the learned peaks are used at all, and the further margin (A) they are used with at
#: that many days, falling in a straight line to nothing at `READY_DAYS` (see the module for the reasoning).
MIN_READY_DAYS: Final = 7
EXTRA_MARGIN_A: Final = 4.0
#: Until the learning is done a day counts toward `MIN_READY_DAYS` and the further margin only spread over the day:
#: its counted minutes in at least this many clock hours, or at least this many of them.
SPREAD_HOURS: Final = 12
SPREAD_MINUTES: Final = 600
#: A day whose minutes come from the recorder's history does not count where more than this share of the minutes
#: that could have counted was lost to a reading that stood still (its freshness not known); and a day that rests
#: on the history weighs this much of a day sampled live, in the further margin and in the end of the learning.
HISTORY_LOST_SHARE: Final = 0.30
HISTORY_DAY_WEIGHT: Final = 0.5
#: A phase's current is read as at most this (A): anything beyond is a broken reading, not kept.
MAX_A: Final = 1000.0
#: An exact sample whose house reads below this on a phase (A, negative) is not counted exactly (see the module).
PLANT_MISMATCH_A: Final = 3.0

#: Only to tell a broken plant reading (`MAX_A`); the sample's own voltage is used for the current.
_NOMINAL_PHASE_V: Final = 230.0
_HOUR_S: Final = 3600
_MINUTE_S: Final = 60

Triple = tuple[float, float, float]


def extra_margin_a(days: float) -> float | None:
    """The margin on top of `MARGIN_A` the learned peaks are used with at `days` counted days (A): `None` below
    `MIN_READY_DAYS` (nothing learned is used), `EXTRA_MARGIN_A` at `MIN_READY_DAYS`, falling in a straight line to 0
    at `READY_DAYS` and beyond (see the module)."""
    if days < MIN_READY_DAYS:
        return None
    if days >= READY_DAYS:
        return 0.0
    return EXTRA_MARGIN_A * (READY_DAYS - days) / (READY_DAYS - MIN_READY_DAYS)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and abs(number) <= MAX_A else None


def _triple(value: Any) -> Triple | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    numbers = [_finite(item) for item in value]
    if any(number is None for number in numbers):
        return None
    return (numbers[0], numbers[1], numbers[2])  # type: ignore[return-value]


def _max_triple(first: Triple | None, second: Triple | None) -> Triple | None:
    if first is None:
        return second
    if second is None:
        return first
    return (max(first[0], second[0]), max(first[1], second[1]), max(first[2], second[2]))


def _max(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


@dataclass(frozen=True, slots=True)
class Reading:
    """One sample's inputs, already read: the meter's current per phase (signed: + import, - export; `None` when the
    measurement is not usable), what the chargers draw per phase (A; `None` while one draws on an unknown phase),
    each charger's readings (for the change check), the battery's power (W, + charging; `None` when it cannot be
    read; 0 on a site without one), the phase voltage (V), the heat pump's per-phase power (W, diagnostics) and the
    plant's own power per phase (W, + output into the house; `None` without it, or with a phase stale or unknown)."""

    site_a: Triple | None
    chargers_a: Triple | None
    charger_readings: tuple[float, ...]
    battery_w: float | None
    phase_volts: float
    heat_w: Triple | None = None
    plant_w: Triple | None = None
    #: Only for a sample of the recorder's history (`site/phase_backfill.py`): it does not count because a reading
    #: stood still or was not there, so that its freshness could not be judged (the measurement, a charger drawing,
    #: or the battery's power with no plant per-phase power to do without it).
    unjudged: bool = False


def exact_phase_a(reading: Reading) -> Triple | None:
    """The house's current per phase from the plant's own per-phase power (see the module), `None` without it, without
    the measurement or a charger's phase, or where a phase reads below `-PLANT_MISMATCH_A`."""
    if reading.plant_w is None or reading.site_a is None or reading.chargers_a is None or reading.phase_volts <= 0:
        return None
    house = (
        reading.site_a[0] - reading.chargers_a[0] + reading.plant_w[0] / reading.phase_volts,
        reading.site_a[1] - reading.chargers_a[1] + reading.plant_w[1] / reading.phase_volts,
        reading.site_a[2] - reading.chargers_a[2] + reading.plant_w[2] / reading.phase_volts,
    )
    return None if min(house) < -PLANT_MISMATCH_A else house


def house_phase(reading: Reading) -> tuple[Triple | None, bool]:
    """The house's current per phase in this sample and whether it is exact: from the plant's per-phase power where
    that counts (`exact_phase_a`), else within the battery's band (`house_phase_a`)."""
    exact = exact_phase_a(reading)
    if exact is not None:
        return exact, True
    return house_phase_a(reading), False


def house_phase_a(reading: Reading) -> Triple | None:
    """The house's current per phase in this sample by the battery's band (see the module), `None` when the sample
    does not count so: the measurement, a charger's phase or the battery's power unknown, or the battery outside
    the band."""
    if reading.site_a is None or reading.chargers_a is None or reading.battery_w is None or reading.phase_volts <= 0:
        return None
    if abs(reading.battery_w) > BATTERY_BAND_W:
        return None
    discharge_a = max(-reading.battery_w, 0.0) / reading.phase_volts
    return (
        reading.site_a[0] - reading.chargers_a[0] + discharge_a,
        reading.site_a[1] - reading.chargers_a[1] + discharge_a,
        reading.site_a[2] - reading.chargers_a[2] + discharge_a,
    )


def house_sum_a(reading: Reading) -> float | None:
    """The house's whole load as current summed over the phases (A), whatever the battery does: the meter's sum less
    the chargers' plus the plant's output where its per-phase power counts (the sun included), else less the
    battery's power. `None` when any of them is unknown."""
    exact = exact_phase_a(reading)
    if exact is not None:
        return sum(exact)
    if reading.site_a is None or reading.chargers_a is None or reading.battery_w is None or reading.phase_volts <= 0:
        return None
    return sum(reading.site_a) - sum(reading.chargers_a) - reading.battery_w / reading.phase_volts


@dataclass(slots=True)
class _Minute:
    """One minute being taken."""

    minute: int
    samples: int = 0
    house: list[float] | None = None
    house_ok: bool = True
    #: Every sample of the minute exact (`exact_phase_a`).
    exact: bool = True
    total: float = 0.0
    total_ok: bool = True
    low: tuple[float, ...] | None = None
    high: tuple[float, ...] | None = None
    heat: list[float] | None = None
    heat_samples: int = 0
    unsettled: bool = False
    #: A sample of it could not be judged (`Reading.unjudged`).
    unjudged: bool = False

    def add(self, reading: Reading) -> None:
        self.samples += 1
        if reading.unjudged:
            self.unjudged = True
        house, exact = house_phase(reading)
        if not exact:
            self.exact = False
        if house is None:
            self.house_ok = False
        elif self.house_ok:
            self.house = list(house) if self.house is None else [a + b for a, b in zip(self.house, house, strict=True)]
        total = house_sum_a(reading)
        if total is None:
            self.total_ok = False
        else:
            self.total += total
        readings = reading.charger_readings
        if self.low is None or self.high is None:
            self.low = self.high = readings
        elif len(self.low) != len(readings):
            self.unsettled = True  # the site's chargers changed within the minute
        else:
            self.low = tuple(min(a, b) for a, b in zip(self.low, readings, strict=True))
            self.high = tuple(max(a, b) for a, b in zip(self.high, readings, strict=True))
        if reading.heat_w is not None:
            self.heat_samples += 1
            self.heat = list(reading.heat_w) if self.heat is None else [
                a + b for a, b in zip(self.heat, reading.heat_w, strict=True)
            ]

    @property
    def settled(self) -> bool:
        """Enough samples, and no charger's current changed by more than `CHARGER_STEP_A`."""
        if self.unsettled or self.samples < MIN_SAMPLES or self.low is None or self.high is None:
            return False
        return all(high - low <= CHARGER_STEP_A for low, high in zip(self.low, self.high, strict=True))

    def house_mean(self) -> Triple | None:
        if not self.house_ok or self.house is None or self.samples < MIN_SAMPLES:
            return None
        return (self.house[0] / self.samples, self.house[1] / self.samples, self.house[2] / self.samples)

    def total_mean(self) -> float | None:
        if not self.total_ok or self.samples < MIN_SAMPLES:
            return None
        return self.total / self.samples

    def heat_mean(self) -> Triple | None:
        if self.heat is None or self.heat_samples < MIN_SAMPLES:
            return None
        return (self.heat[0] / self.heat_samples, self.heat[1] / self.heat_samples, self.heat[2] / self.heat_samples)


@dataclass(slots=True)
class HourMark:
    """One clock hour's high-water marks: per phase the largest minute counted by the battery's band (A) and the
    minutes so counted, the same for the exact minutes (`exact_a`, `exact_minutes`), the house's whole load's largest
    minute among every minute (`total_all`) and among the band's counted ones (`total_counted`), and the heat pump's
    per-phase peak (kW). `backfilled`: read back from the recorder's history (`site/phase_backfill.py`), not sampled
    live; `lost` then says how many of its minutes did not count because a reading's freshness could not be judged
    (`Reading.unjudged`)."""

    house_a: Triple | None = None
    minutes: int = 0
    total_all: float | None = None
    total_counted: float | None = None
    heat_kw: Triple | None = None
    exact_a: Triple | None = None
    exact_minutes: int = 0
    backfilled: bool = False
    lost: int = 0

    @property
    def counted(self) -> int:
        """The minutes counted, either way."""
        return self.minutes + self.exact_minutes

    def as_dict(self) -> dict[str, Any]:
        def rounded(values: Triple | None, digits: int = 2) -> list[float] | None:
            return None if values is None else [round(value, digits) for value in values]

        return {
            **({"h": True, "l": self.lost} if self.backfilled else {}),
            "a": rounded(self.house_a),
            "n": self.minutes,
            "all": None if self.total_all is None else round(self.total_all, 2),
            "counted": None if self.total_counted is None else round(self.total_counted, 2),
            "heat": rounded(self.heat_kw, 3),
            "xa": rounded(self.exact_a),
            "xn": self.exact_minutes,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> HourMark | None:
        if not isinstance(raw, Mapping):
            return None
        minutes = raw.get("n")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 60:
            return None
        house = _triple(raw.get("a"))
        if minutes and house is None:
            return None
        exact_minutes = raw.get("xn", 0)  # absent in marks kept before the exact minutes
        if isinstance(exact_minutes, bool) or not isinstance(exact_minutes, int) or not 0 <= exact_minutes <= 60:
            return None
        exact = _triple(raw.get("xa"))
        if (exact_minutes and exact is None) or minutes + exact_minutes > 60:
            return None
        lost = raw.get("l", 0)
        if isinstance(lost, bool) or not isinstance(lost, int) or not 0 <= lost <= 60 - minutes - exact_minutes:
            return None
        return cls(
            house_a=house if minutes else None,
            minutes=minutes,
            total_all=_finite(raw.get("all")),
            total_counted=_finite(raw.get("counted")),
            heat_kw=_triple(raw.get("heat")),
            exact_a=exact if exact_minutes else None,
            exact_minutes=exact_minutes,
            backfilled=raw.get("h") is True,  # absent in marks kept before the backfill
            lost=lost,
        )


def _heat_w(heat_kw: Triple | None) -> Triple | None:
    return None if heat_kw is None else (heat_kw[0] * 1000.0, heat_kw[1] * 1000.0, heat_kw[2] * 1000.0)


@dataclass(frozen=True, slots=True)
class Learned:
    """What the window says (`PhasePeaks.learned`)."""

    #: Whether the learning is done: `READY_DAYS` days with `DAY_MINUTES` counted minutes, a day that rests on the
    #: recorder's history weighing `HISTORY_DAY_WEIGHT` (`days_weighted`). No further margin then.
    ready: bool
    #: Per phase the largest hourly peak in the window counted by the battery's band (A), `None` while none counted.
    raw_a: Triple | None
    #: The bias factor (at least 1) and the per-phase peaks (A): the larger of the band's (raw x factor, or raw plus
    #: the whole hidden load) and the exact peak, plus `MARGIN_A`; the exact peak alone once `exact_days` reach
    #: `READY_DAYS`. The cap uses them plus `extra_margin_a` (`phase_peaks`).
    factor: float
    peaks_a: Triple | None
    days: int
    hours: int
    minutes: int
    heat_w: Triple | None
    #: Per phase the largest exact hourly peak in the window (A), the days with `DAY_MINUTES` exact minutes, and the
    #: exact minutes (`exact_phase_a`).
    exact_a: Triple | None = None
    exact_days: int = 0
    exact_minutes: int = 0
    #: The hours read back from the recorder's history, how many of `days` count only with them, and the days whose
    #: history does not count (`HISTORY_LOST_SHARE`).
    history_hours: int = 0
    history_days: int = 0
    history_days_dropped: int = 0
    #: `days` with the history's at their weight; the days that count until the learning is done (spread over the
    #: day, and no more than the time from the first to the last of them holds: see the module); and the further
    #: margin the peaks are used with now (A, `extra_margin_a`), `None` while they are not used.
    days_weighted: float = 0.0
    days_spread: int = 0
    extra_margin_a: float | None = None

    @property
    def days_left(self) -> int:
        """The days still to count before the learning is done (0 once `ready`): whole days, rounded up, and at
        least, since a day counts only with `DAY_MINUTES` counted minutes."""
        return 0 if self.ready else max(math.ceil(READY_DAYS - self.days_weighted), 1)

    @property
    def usable(self) -> bool:
        """Whether the cap uses the learned peaks (`MIN_READY_DAYS` days that count), with a further margin until
        `ready`."""
        return self.extra_margin_a is not None

    @property
    def basis(self) -> str:
        """What the peaks rest on: `exact` (the plant's per-phase power alone), `mixed` or `band`; `none` without."""
        if self.peaks_a is None:
            return "none"
        if self.exact_days >= READY_DAYS:
            return "exact"
        return "band" if self.exact_a is None else "mixed" if self.raw_a is not None else "exact"

    def as_dict(self) -> dict[str, Any]:
        def per_phase(values: Triple | None, digits: int = 1) -> dict[str, float] | None:
            return None if values is None else {
                phase: round(value, digits) for phase, value in zip(PHASES, values, strict=True)
            }

        return {
            "ready": self.ready,
            "raw_a": per_phase(self.raw_a),
            "factor": round(self.factor, 3),
            "margin_a": MARGIN_A,
            "peaks_a": per_phase(self.peaks_a),
            "usable": self.usable,
            "extra_margin_a": None if self.extra_margin_a is None else round(self.extra_margin_a, 2),
            "days_counted": self.days,
            "days_min": MIN_READY_DAYS,
            "days_spread": self.days_spread,
            "days_from_history": self.history_days,
            "days_from_history_dropped": self.history_days_dropped,
            "hours_from_history": self.history_hours,
            "days_needed": READY_DAYS,
            "days_left": self.days_left,
            "hours": self.hours,
            "minutes": self.minutes,
            "heat_peak_w": per_phase(self.heat_w, 0),
            "exact_a": per_phase(self.exact_a),
            "exact_days": self.exact_days,
            "exact_minutes": self.exact_minutes,
            "basis": self.basis,
        }

    def phase_peaks(self) -> dict[str, float] | None:
        """The per-phase peaks the cap uses, by phase name: the learned ones plus the further margin of the days
        counted so far (`extra_margin_a`); `None` below `MIN_READY_DAYS` (the conservative rule)."""
        extra = self.extra_margin_a
        if extra is None or self.peaks_a is None:
            return None
        return {phase: value + extra for phase, value in zip(PHASES, self.peaks_a, strict=True)}


class PhasePeaks:
    """The hourly marks of the last `WINDOW_DAYS` days and the minute being taken (pure; times are epoch seconds)."""

    def __init__(self) -> None:
        self.hours: dict[int, HourMark] = {}
        self._minute: _Minute | None = None
        #: The last closed minutes, newest last (a minute counts once both its neighbours are known settled).
        self._closed: deque[_Minute] = deque(maxlen=3)

    def add(self, now: float, reading: Reading) -> bool:
        """One sample at `now`; whether a mark changed (a minute was counted)."""
        minute = int(now // _MINUTE_S)
        changed = False
        current = self._minute
        if current is not None and current.minute != minute:
            changed = self._close(current)
            current = None
        if current is None:
            current = self._minute = _Minute(minute)
        current.add(reading)
        return changed

    def _close(self, minute: _Minute) -> bool:
        """`minute` is over: the minute before it is now judged (its neighbours known)."""
        closed = self._closed
        if closed and closed[-1].minute != minute.minute - 1:
            closed.clear()  # a gap: the minutes before it have no known next one, and are not counted
        closed.append(minute)
        changed = False
        if len(closed) >= 2:
            middle = closed[-2]
            before = closed[-3] if len(closed) == 3 else None
            neighbours_settled = minute.settled and (before is not None and before.settled)
            changed = self._count(middle, settled=neighbours_settled and middle.settled)
        self._prune(minute.minute * _MINUTE_S)
        return changed

    def _count(self, minute: _Minute, *, settled: bool) -> bool:
        hour = minute.minute * _MINUTE_S // _HOUR_S
        mark = self.hours.get(hour)
        if mark is None:
            mark = self.hours[hour] = HourMark()
        heat = minute.heat_mean()
        if heat is not None:
            mark.heat_kw = _max_triple(mark.heat_kw, (heat[0] / 1000.0, heat[1] / 1000.0, heat[2] / 1000.0))
        if minute.unjudged:
            mark.lost += 1  # never counted as well: an unjudged sample is no house (`_Minute.house_ok`)
        if not settled:
            return heat is not None or minute.unjudged
        total = minute.total_mean()
        if total is not None:
            mark.total_all = _max(mark.total_all, total)
        house = minute.house_mean()
        if house is not None and minute.exact:
            mark.exact_a = _max_triple(mark.exact_a, house)
            mark.exact_minutes += 1
        elif house is not None:
            mark.house_a = _max_triple(mark.house_a, house)
            mark.minutes += 1
            if total is not None:
                mark.total_counted = _max(mark.total_counted, total)
        return True

    def _prune(self, now: float) -> None:
        oldest = int(now // _HOUR_S) - WINDOW_DAYS * 24
        for hour in [hour for hour in self.hours if hour < oldest]:
            del self.hours[hour]

    def learned(self, now: float) -> Learned:
        """The window's peaks (see the module)."""
        oldest = int(now // _HOUR_S) - WINDOW_DAYS * 24
        raw: Triple | None = None
        exact: Triple | None = None
        heat: Triple | None = None
        total_all: float | None = None
        total_counted: float | None = None
        #: Per day: the minutes and the clock hours counted live, the same from the history, the history's lost
        #: minutes, the exact minutes either way, and the first and last counted hour.
        live: dict[int, list[int]] = {}
        past: dict[int, list[int]] = {}
        exact_live: dict[int, int] = {}
        exact_past: dict[int, int] = {}
        ends: dict[int, tuple[int, int]] = {}
        hours = minutes = exact_minutes = history_hours = 0
        for hour, mark in self.hours.items():
            if hour < oldest:
                continue
            day = hour // 24
            heat = _max_triple(heat, mark.heat_kw)
            total_all = _max(total_all, mark.total_all)
            band = mark.minutes > 0 and mark.house_a is not None
            exactly = mark.exact_minutes > 0 and mark.exact_a is not None
            counted = (mark.minutes if band else 0) + (mark.exact_minutes if exactly else 0)
            if mark.backfilled and (counted or mark.lost):
                kept = past.setdefault(day, [0, 0, 0])
                kept[2] += mark.lost
            if band:
                raw = _max_triple(raw, mark.house_a)
                total_counted = _max(total_counted, mark.total_counted)
                minutes += mark.minutes
            if exactly:
                exact = _max_triple(exact, mark.exact_a)
                exact_minutes += mark.exact_minutes
                side = exact_past if mark.backfilled else exact_live
                side[day] = side.get(day, 0) + mark.exact_minutes
            if counted:
                hours += 1
                history_hours += 1 if mark.backfilled else 0
                kept = (past if mark.backfilled else live).setdefault(day, [0, 0, 0])
                kept[0] += counted
                kept[1] += 1
                first, last = ends.get(day, (hour, hour))
                ends[day] = (min(first, hour), max(last, hour))
        # A day's history counts only where few of its minutes were lost to readings that stood still.
        dropped = {
            day for day, (counted, _hours, lost) in past.items()
            if counted + lost and lost / (counted + lost) > HISTORY_LOST_SHARE
        }
        day_count = live_count = exact_day_count = spread_count = spread_live = 0
        first_hour: int | None = None
        last_hour: int | None = None
        for day in set(live) | set(past):
            own = live.get(day, [0, 0, 0])
            more = [0, 0, 0] if day in dropped else past.get(day, [0, 0, 0])
            counted = own[0] + more[0]
            if counted < DAY_MINUTES:
                continue
            day_count += 1
            live_count += 1 if own[0] >= DAY_MINUTES else 0
            if exact_live.get(day, 0) + (0 if day in dropped else exact_past.get(day, 0)) >= DAY_MINUTES:
                exact_day_count += 1
            if counted >= SPREAD_MINUTES or own[1] + more[1] >= SPREAD_HOURS:
                spread_count += 1
                spread_live += 1 if own[0] >= SPREAD_MINUTES or own[1] >= SPREAD_HOURS else 0
                first_hour = ends[day][0] if first_hour is None else min(first_hour, ends[day][0])
                last_hour = ends[day][1] if last_hour is None else max(last_hour, ends[day][1])
        weighted = live_count + HISTORY_DAY_WEIGHT * (day_count - live_count)
        # No more days than the time from the first to the last counted hour holds (a day more than its whole days:
        # seven days in a row reach from one day's noon to the seventh's).
        span_days = 0 if first_hour is None or last_hour is None else (last_hour - first_hour + 1 + 24) // 24
        spread = min(spread_count, span_days)
        factor = 1.0
        if total_all is not None and total_counted is not None:
            factor = max(1.0, total_all / max(total_counted, 1.0))
        # What the battery hid (see the module): scaled by the factor, and never less than the whole hidden load
        # on that one phase.
        hidden = 0.0
        if total_all is not None and total_counted is not None:
            hidden = max(total_all - total_counted, 0.0)
        band_peaks = None if raw is None or exact_day_count >= READY_DAYS else tuple(
            max(max(value, 0.0) * factor, max(value, 0.0) + hidden) for value in raw
        )
        exact_peaks = None if exact is None else tuple(max(value, 0.0) for value in exact)
        largest = _max_triple(band_peaks, exact_peaks)  # type: ignore[arg-type]
        peaks = None if largest is None else tuple(value + MARGIN_A for value in largest)
        ready = peaks is not None and day_count >= READY_DAYS and weighted >= READY_DAYS
        extra: float | None = None
        if ready:
            extra = 0.0
        elif peaks is not None:
            # The margin falls with the days that count, the history's at their weight; never below what is left
            # of it while the learning is not done by the days' own count.
            extra = extra_margin_a(spread)
            if extra is not None:
                extra = max(extra, extra_margin_a(max(
                    spread_live + HISTORY_DAY_WEIGHT * (spread_count - spread_live), float(MIN_READY_DAYS)
                )) or 0.0)
        return Learned(
            ready=ready,
            raw_a=raw,
            factor=factor,
            peaks_a=peaks,  # type: ignore[arg-type]
            days=day_count,
            hours=hours,
            minutes=minutes + exact_minutes,
            heat_w=_heat_w(heat),
            exact_a=exact,
            exact_days=exact_day_count,
            exact_minutes=exact_minutes,
            history_hours=history_hours,
            history_days=day_count - live_count,
            history_days_dropped=len(dropped),
            days_weighted=weighted,
            days_spread=spread,
            extra_margin_a=extra,
        )

    def covered(self, first_hour: int, hours: int) -> bool:
        """Whether every one of `hours` clock hours from `first_hour` has a mark (sampled or read back)."""
        return all(hour in self.hours for hour in range(first_hour, first_hour + hours))

    def merge_history(self, history: PhasePeaks, now: float) -> int:
        """The marks read back from the recorder (`site/phase_backfill.py`) into these; how many hours with counted
        minutes were taken (an hour read back with nothing counted is kept too, so it is not read again).
        An hour with minutes counted live keeps its own mark whatever the history says (the live reading knows when
        each sensor last reported, the history only when it last changed). An hour already read back is replaced only
        by a mark with more counted minutes."""
        newest = int(now // _HOUR_S)
        oldest = newest - WINDOW_DAYS * 24
        taken = 0
        for hour, mark in history.hours.items():
            if not oldest <= hour <= newest:
                continue
            kept = self.hours.get(hour)
            if kept is not None and kept.counted and (not kept.backfilled or kept.counted >= mark.counted):
                continue
            if kept is not None:  # nothing counted in it: only its heat pump's peak is worth keeping
                mark.heat_kw = _max_triple(mark.heat_kw, kept.heat_kw)
            mark.backfilled = True
            self.hours[hour] = mark
            taken += 1 if mark.counted else 0
        return taken

    def as_dict(self) -> dict[str, Any]:
        return {"hours": {str(hour): mark.as_dict() for hour, mark in sorted(self.hours.items())}}

    def load(self, raw: Any, now: float) -> None:
        """The stored marks, read as untrusted: an hour key that is no whole hour of the window, or a mark that does
        not read, is dropped. Marks taken since the start are kept over stored ones of the same hour."""
        hours = raw.get("hours") if isinstance(raw, Mapping) else None
        if not isinstance(hours, Mapping):
            return
        newest = int(now // _HOUR_S)
        oldest = newest - WINDOW_DAYS * 24
        for key, value in list(hours.items())[: WINDOW_DAYS * 24 + 48]:
            try:
                hour = int(key)
            except (TypeError, ValueError):
                continue
            if not oldest <= hour <= newest or hour in self.hours:
                continue
            mark = HourMark.from_dict(value)
            if mark is not None:
                self.hours[hour] = mark


# ---------------------------------------------------------------------------------------------- the site's shell


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.phase_peaks.{entry_id}"


def _charger_draw(
    site: SiteCapacityController, charger_entry_id: str, max_age_s: float, now: float, *, from_history: bool = False
) -> tuple[Triple | None, tuple[float, ...], bool]:
    """What one charger draws per phase now (A; `None` when it draws on a phase not known), its readings, and whether
    the draw is `None` because the history cannot say (below).

    `from_history`: the reading is one of the recorder's (`site/phase_backfill.py`), which keeps when a state last
    changed, not when it was last reported. A current that stood still for longer than the site's maximum age may
    then be a charger that stopped reporting or a car drawing evenly, and it cannot be told which: such a reading
    of `DRAWING_A` or more makes the sample not count (`None`), rather than count the car as house (a false peak of
    the car's whole current) or take off a current the car may no longer draw."""
    measured = site.charger_measured_current(charger_entry_id)
    if measured is None:
        return (0.0, 0.0, 0.0), (), False
    values: list[float] = []
    unknown = False
    for phase in PHASES:
        value = measured.get(phase)
        ages = [age for age in (value.age_s, value.report_age_s) if age is not None]
        usable = value.value is not None and value.problem is None
        fresh = usable and ages and min(ages) <= max_age_s
        if from_history and usable and not fresh and float(value.value) >= DRAWING_A:  # type: ignore[arg-type]
            unknown = True
        values.append(max(float(value.value), 0.0) if fresh else 0.0)  # type: ignore[arg-type]
    readings = tuple(values)
    if unknown:
        return None, readings, True
    phases, phase = site.reckoned_phases(charger_entry_id)
    if phases == 3:
        return (values[0], values[1], values[2]), readings, False
    largest = max(values)
    if phase not in PHASES:
        return (None if largest >= DRAWING_A else (0.0, 0.0, 0.0)), readings, False
    return tuple(largest if name == phase else 0.0 for name in PHASES), readings, False  # type: ignore[return-value]


def heat_phase_entities(site: SiteCapacityController) -> tuple[str, str, str] | None:
    """The heat pump's meter's per-phase power entities, by the site's configuration: its power sensor's `_l1` ..
    `_l3` siblings (whether they exist is for the one that reads them); `None` without a power meter."""
    meter = getattr(getattr(getattr(site, "house_forecast", None), "heat", None), "meter", None)
    entity_id = getattr(meter, "entity_id", None)
    if not isinstance(entity_id, str) or getattr(meter, "kind", None) != "power":
        return None
    return (f"{entity_id}_l1", f"{entity_id}_l2", f"{entity_id}_l3")


def plant_phase_entities(site: SiteCapacityController) -> tuple[tuple[str, str, str], float] | None:
    """The entities of the plant's own power per phase and their sign, as the battery's facts name them from its
    adapter (`battery/adapters.py` `plant_phase_power`); `None` where they name none."""
    facts = getattr(getattr(site, "battery_shadow", None), "facts", None)
    entity_ids = getattr(facts, "plant_phase_entities", None)
    sign = getattr(facts, "plant_phase_sign", None)
    if not entity_ids or len(entity_ids) != 3 or sign not in (1.0, -1.0):
        return None
    return (entity_ids[0], entity_ids[1], entity_ids[2]), sign


def entities(site: SiteCapacityController) -> list[str]:
    """Every entity a reading of the site may read (`read_site`), by its configuration and the battery's adapter,
    whatever their states are now: the site's measurement, each charger's measured current and the battery's power
    (`SiteCapacityController.measurement_entity_ids`), the plant's per-phase power and the heat pump's meter's.
    The ones whose history is read back (`site/phase_backfill.py`)."""
    found = set(site.measurement_entity_ids())
    plant = plant_phase_entities(site)
    if plant is not None:
        found.update(plant[0])
    found.update(heat_phase_entities(site) or ())
    return sorted(found)


def _heat_phase_w(site: SiteCapacityController) -> Triple | None:
    """The heat pump's meter's per-phase power (W) where its sensor has `_l1` .. `_l3` siblings, else `None`."""
    from homeassistant.const import UnitOfPower  # noqa: PLC0415 - kept beside its only use

    entity_ids = heat_phase_entities(site)
    if entity_ids is None:
        return None
    values: list[float] = []
    for entity_id in entity_ids:
        state = site.hass.states.get(entity_id)
        if state is None:
            return None
        value = _finite_any(state.state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        values.append(value * 1000.0 if unit == UnitOfPower.KILO_WATT else value)
    return (values[0], values[1], values[2])


def _plant_phase_w(site: SiteCapacityController, max_age_s: float, now: float) -> Triple | None:
    """The plant's own power per phase (W, + output; `battery/adapters.py` `plant_phase_power`), where the battery's
    facts name its entities and all three read a power reported within `max_age_s`; else `None`."""
    from ..battery.facts import to_kw  # noqa: PLC0415 - the battery package imports the site

    named = plant_phase_entities(site)
    if named is None:
        return None
    entity_ids, sign = named
    values: list[float] = []
    for entity_id in entity_ids:
        state = site.hass.states.get(entity_id)
        if state is None:
            return None
        kw = to_kw(state.state, state.attributes.get("unit_of_measurement"))
        reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        if kw is None or reported is None or not 0.0 <= now - reported.timestamp() <= max_age_s:
            return None
        watts = kw * 1000.0 * sign
        if abs(watts) / _NOMINAL_PHASE_V > MAX_A:  # a broken reading
            return None
        values.append(watts)
    return (values[0], values[1], values[2])


def _finite_any(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_site(site: SiteCapacityController, now: float) -> Reading:
    """One sample's inputs from what the site holds now. The one reading there is: the recorder's history is run
    through it too, as a site that holds the states of then (`site/phase_backfill.py` `SiteAt`, `from_history`)."""
    from ..battery.gate import phase_volts  # noqa: PLC0415 - the battery package imports the site
    from ..planning.grid_voltage import stored_voltage_between_phases_v  # noqa: PLC0415

    result = site.result
    site_a: Triple | None = None
    if result.state == "observing":
        values: list[float] = []
        for phase in PHASES:
            magnitude = result.measured_phase_current_a.get(phase)
            if magnitude is None or not math.isfinite(magnitude):
                break
            power = result.phase_signed_active_power_w.get(phase)
            values.append(-abs(magnitude) if power is not None and power < 0 else abs(magnitude))
        else:
            site_a = (values[0], values[1], values[2])
    max_age_s = float(site.config.get(CONF_MAX_AGE_S, DEFAULT_MAX_AGE_S))
    from_history = getattr(site, "from_history", False) is True
    chargers: list[float] | None = [0.0, 0.0, 0.0]
    readings: list[float] = []
    unjudged = False
    for charger_entry_id in site.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        draw, own, unknown = _charger_draw(site, charger_entry_id, max_age_s, now, from_history=from_history)
        unjudged = unjudged or unknown
        readings.extend(own)
        if draw is None or chargers is None:
            chargers = None
            continue
        chargers = [a + b for a, b in zip(chargers, draw, strict=True)]
    configured, battery_w = site.battery_live_w()
    plant_w = _plant_phase_w(site, max_age_s, now)
    if not configured:
        battery_w = 0.0
    return Reading(
        site_a=site_a,
        chargers_a=None if chargers is None else (chargers[0], chargers[1], chargers[2]),
        charger_readings=tuple(readings),
        battery_w=battery_w,
        phase_volts=phase_volts(stored_voltage_between_phases_v(site.config)),
        heat_w=_heat_phase_w(site),
        plant_w=plant_w,
        # In the history a reading that stood still reads as stale: the sample is lost to that, not to the house.
        unjudged=from_history and (site_a is None or unjudged or (battery_w is None and plant_w is None)),
    )


class SitePhasePeaks:
    """One site's learned per-phase house peaks, kept in its own store (see the module)."""

    def __init__(self, hass: HomeAssistant | None = None, entry_id: str | None = None) -> None:
        from ..debug_day import DayStore  # noqa: PLC0415 - Home Assistant's storage, kept out of the pure part

        self.peaks = PhasePeaks()
        self._hass = hass
        self._store = None if hass is None or entry_id is None else DayStore(hass, store_key(entry_id), self._data)
        self._failed = False
        #: The reading back of the recorder's history (`site/phase_backfill.py`), once started.
        self.backfill: Any = None

    def _data(self) -> dict[str, Any]:
        return self.peaks.as_dict()

    async def async_load(self, now: float) -> None:
        if self._store is None:
            return
        stored = await self._store.async_load()
        try:
            self.peaks.load(stored, now)
        except Exception:  # noqa: BLE001 - a broken store starts again; the conservative rule counts meanwhile
            _LOGGER.debug("SpotNav phase peaks: the kept marks could not be read", exc_info=True)

    def sample(self, site: SiteCapacityController, now: float) -> None:
        """One sample of `site` now. Never raises."""
        try:
            if self.peaks.add(now, read_site(site, now)) and self._store is not None:
                self._store.changed()
        except Exception:  # noqa: BLE001 - a measurement must never disturb the site
            if not self._failed:
                self._failed = True
                _LOGGER.warning("SpotNav site: the house's per-phase peaks could not be sampled", exc_info=True)

    def learned(self, now: float) -> Learned:
        return self.peaks.learned(now)

    def start_backfill(self, site: SiteCapacityController) -> None:
        """Read the days before these marks back from the recorder, in the background (`site/phase_backfill.py`);
        stopped by `async_close`. Never raises."""
        if self._hass is None or self.backfill is not None:
            return
        try:
            from .phase_backfill import Backfill  # noqa: PLC0415 - it imports this module

            self.backfill = Backfill(self._hass, self.peaks, self._history_taken)
            self.backfill.start(site)
        except Exception:  # noqa: BLE001 - without it the marks grow from the live samples alone
            _LOGGER.debug("SpotNav phase peaks: the backfill could not be started", exc_info=True)

    def _history_taken(self) -> None:
        if self._store is not None:
            self._store.changed()

    def backfill_status(self) -> dict[str, Any] | None:
        """What the last reading back of the recorder's history gave, for the diagnostics; `None` before one ran."""
        backfill = self.backfill
        return None if backfill is None or backfill.last is None else {"runs": backfill.runs, **backfill.last}

    async def async_close(self) -> None:
        if self.backfill is not None:
            await self.backfill.async_stop()
        if self._store is not None:
            await self._store.async_close()

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        from ..debug_day import DayStore  # noqa: PLC0415

        await DayStore.async_remove(hass, store_key(entry_id))


__all__: Sequence[str] = (
    "BATTERY_BAND_W", "DAY_MINUTES", "EXTRA_MARGIN_A", "HISTORY_DAY_WEIGHT", "HISTORY_LOST_SHARE", "HourMark",
    "Learned", "MARGIN_A", "MIN_READY_DAYS", "PhasePeaks", "SPREAD_HOURS", "SPREAD_MINUTES",
    "READY_DAYS", "Reading", "SitePhasePeaks", "PLANT_MISMATCH_A", "WINDOW_DAYS", "exact_phase_a", "extra_margin_a",
    "house_phase", "house_phase_a", "house_sum_a",
)
