"""The state of charge a target is planned and stopped against, read honestly.

A car's state-of-charge sensor is often an hourly cloud poll that goes stale mid-charge; a
charger that reports the car's state of charge is live. Either way the question is the current
state of charge and how sure we are, answered in three layers:

* `resolve_soc` is pure: raw reading, kept anchor, the charger's cumulative energy register and
  the battery size in; effective reading and updated anchor out. The whole policy lives here.
* `SocReader` is the stateful shell: reads the raw sources, keeps the anchor (persisted so a
  restart mid-charge keeps the estimate), watches the sources, and serves both the planner and
  the stop decision, so the planned and the enforced state of charge are one number.
* `target_need_kwh` is the wall energy a target needs, including the charging loss.

    soc_est = soc_at_last_fresh_reading + delivered_since_kwh x efficiency / capacity_kwh x 100

`delivered_since_kwh` is the change of the charger's energy register since the reading. Many registers
lag (a cloud that books an hour's energy at once), so the register value the reading is measured from (its
baseline) is never one posted before the reading: it is the first value the register posts at or after it
(`resolve_soc`). Energy charged before the reading and booked late lands in that value, not in the estimate;
energy charged between the reading and that value is left uncounted (an under-count, never an over-count).

A reading timed by the car's own report time arrives later than it was made (a cloud poll: the field night of
21-22 October, an EV6's 70 % made at 02:47:54 arrived at 03:02:51, and the 2.7 kWh charged in between was left
out, 3.1 points). When the register is seen posting steadily from the report time to the arrival (a value at
most `REGISTER_STEADY_GAP_S` before and after the report time, no longer gap after it, no reset), it is not
lagging there: the baseline is its value at the report time, interpolated between the two values around it
(`register_value_at`, from the values `SocReader` saw in the last `REGISTER_HISTORY_S`). Otherwise, or with no
values seen (after a restart), the rule above stands. The
result is flagged `estimated`, a fresh reading (no older than `target_stop.SOC_FRESH_MAX_AGE_S`)
always replaces it, and it is never invented: with no register, no capacity or a register that
went backwards the raw reading is reported as is. Nothing here writes to a charger or a car.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..execution.target_stop import (
    charge_ceiling_percent,
    charger_soc_entity_id,
    SOC_FRESH_MAX_AGE_S,
    SocReading,
    SocSource,
    TimeBasis,
)
from ..planning.planner import resolve_target_energy, TargetEnergyRequest
from ..util import finite_number
from .vehicle_discovery import (
    _device_name,
    charger_vehicle_ids,
    discover_vehicles,
    resolve_target_vehicle,
    vehicle_soc_entity_id,
)


_LOGGER = logging.getLogger(__name__)

#: Wall energy to battery energy (AC charging loses roughly 8-12 %). Used in both directions, so
#: an error cancels between planning and stopping instead of compounding.
CHARGE_EFFICIENCY: Final = 0.9

#: A register step back smaller than this is noise; a bigger one is a meter reset and voids the
#: estimate (never negative delivery).
REGISTER_TOLERANCE_KWH: Final = 0.05

#: Two readings whose instants differ by less than this are the same reading seen twice.
SAME_READING_S: Final = 1.0

#: At most this often a register that only rose is written to storage with the anchor (`SocAnchor.peak_kwh`):
#: what a crash loses of it is under-counted after a reset, never over-counted.
PEAK_SAVE_INTERVAL_S: Final = 60.0

#: A register seen posting at least this often around a car's report time, and on to the reading's arrival, is not
#: lagging there: the reading's baseline is its value at the report time (`register_value_at`). An Easee cloud
#: register that books an hour at once has no value this close and keeps the first value after the reading.
REGISTER_STEADY_GAP_S: Final = 120.0

#: How long a vehicle's reported capacity and ceiling are reused (a pack size does not change).
FACTS_CACHE_S: Final = 300.0

#: A reading that moved less than this since the planner last heard of it does not trigger a
#: new calculation.
RECALCULATE_DELTA_PERCENT: Final = 2.0

#: A register value and when it was posted (`None`: the register was unreadable then).
RegisterSample = tuple[datetime, float | None]

#: Which value a baseline is (`SocAnchor.baseline`): the register's at the car's report time, or the first one
#: posted after the reading.
BaselineRule = Literal["report_time", "after_arrival"]

_STORE_VERSION: Final = 1
_STORE_KEY_PREFIX: Final = f"{DOMAIN}.soc_anchor"


@dataclass(frozen=True, slots=True)
class SocAnchor:
    """The last real reading and the energy register when it was taken.

    `register_kwh` is the baseline: `None` until the register has posted a value after `read_at` (or while it
    is unreadable). Such an anchor carries a reading, and an estimate only once that value has become its
    baseline (`resolve_soc`). `read_at` is when the reading was made, not noticed. `register_entity_id` names
    the register `register_kwh` was read from: a baseline from another register (one found again, or
    chosen by a person) is dropped and taken again, never subtracted across two meters.

    A register that falls back (a per-session register that starts again at a plug-in or a restart, a meter
    that reads 0 while its charger reboots) is followed (`track_register`): what it had counted since the
    reading is kept in `carried_kwh` and it is measured on from its new value. `peak_kwh` is the highest value
    it showed since the baseline (what a fall carries); `reset_from_kwh` the value it last fell from, with
    `resume_carried_kwh` what was carried then: a register back at that value was a glitch, and is measured
    on from there with that carry, so the values it showed while down are never counted twice.
    `time_basis` is what `read_at` was taken from (`SocReading.time_basis`). `baseline` says which value the
    baseline is (`BaselineRule`, `resolve_soc`), for the diagnostics.
    """

    soc_percent: float
    register_kwh: float | None
    read_at: datetime
    source: SocSource
    vehicle_id: str | None
    register_entity_id: str | None = None
    carried_kwh: float = 0.0
    reset_from_kwh: float | None = None
    resume_carried_kwh: float = 0.0
    time_basis: TimeBasis = "last_updated"
    #: Not part of the anchor's identity: a rise alone is saved at most every `PEAK_SAVE_INTERVAL_S`.
    peak_kwh: float | None = field(default=None, compare=False)
    #: Only shown: it changes with `register_kwh`.
    baseline: BaselineRule | None = field(default=None, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "soc_percent": self.soc_percent,
            "register_kwh": self.register_kwh,
            "read_at": self.read_at.isoformat(),
            "source": self.source,
            "vehicle_id": self.vehicle_id,
            "register_entity_id": self.register_entity_id,
            "carried_kwh": self.carried_kwh,
            "reset_from_kwh": self.reset_from_kwh,
            "resume_carried_kwh": self.resume_carried_kwh,
            "peak_kwh": self.peak_kwh,
            "time_basis": self.time_basis,
            "baseline": self.baseline,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SocAnchor | None:
        """A stored anchor, or `None` for anything that is not exactly one (storage is untrusted)."""
        if not isinstance(raw, dict):
            return None
        soc = finite_number(raw.get("soc_percent"))
        register = raw.get("register_kwh")
        register = None if register is None else finite_number(register)
        source = raw.get("source")
        vehicle_id = raw.get("vehicle_id")
        read_at_raw = raw.get("read_at")
        if soc is None or not 0.0 <= soc <= 100.0 or source not in ("vehicle", "charger"):
            return None
        if vehicle_id is not None and not isinstance(vehicle_id, str):
            return None
        if raw.get("register_kwh") is not None and register is None:
            return None
        register_entity_id = raw.get("register_entity_id")
        if register_entity_id is not None and not isinstance(register_entity_id, str):
            return None
        if register_entity_id is None:
            # Stored before the register was named (or with none): which meter the baseline came from is
            # unknown, so it is taken again from the register read next.
            register = None
        read_at = dt_util.parse_datetime(read_at_raw) if isinstance(read_at_raw, str) else None
        if read_at is None or read_at.tzinfo is None:
            return None
        basis = raw.get("time_basis")
        anchor = cls(
            soc, register, read_at, source, vehicle_id, register_entity_id,
            time_basis=basis if basis in ("last_updated", "vehicle_timestamp") else "last_updated",
        )
        if register is None:
            return anchor
        stored_rule = raw.get("baseline")
        if stored_rule in ("report_time", "after_arrival"):
            anchor = replace(anchor, baseline=stored_rule)
        # What a fallen register carried (stored since 14 October); anything malformed carries nothing, which
        # can only under-count.
        reset_from = finite_number(raw.get("reset_from_kwh"))
        peak = finite_number(raw.get("peak_kwh"))
        return replace(
            anchor,
            carried_kwh=_non_negative(raw.get("carried_kwh")),
            reset_from_kwh=reset_from,
            resume_carried_kwh=_non_negative(raw.get("resume_carried_kwh")) if reset_from is not None else 0.0,
            peak_kwh=peak if peak is not None and peak >= register else register,
        )


def _non_negative(raw: object) -> float:
    value = finite_number(raw)
    return value if value is not None and value > 0 else 0.0


@dataclass(frozen=True, slots=True)
class SocResolution:
    """`resolve_soc`'s answer: what to use now, and the anchor to keep."""

    reading: SocReading | None
    anchor: SocAnchor | None


def is_fresh(reading: SocReading | None) -> bool:
    """Whether this reading is recent enough to be the state of charge as it is."""
    return (
        reading is not None
        and reading.soc_percent is not None
        and reading.age_s is not None
        and reading.age_s <= SOC_FRESH_MAX_AGE_S
    )


def delivered_since_kwh(anchor: SocAnchor, register_kwh: float | None) -> float | None:
    """The energy delivered since `anchor`'s reading: what a fallen register carried plus the register's rise
    since the baseline; `None` without a register (now or at the anchor) or when it went backwards."""
    if register_kwh is None or anchor.register_kwh is None:
        return None
    step = register_kwh - anchor.register_kwh
    if step < -REGISTER_TOLERANCE_KWH:
        return None
    return anchor.carried_kwh + max(0.0, step)


def track_register(anchor: SocAnchor | None, register_kwh: float | None) -> SocAnchor | None:
    """`anchor` with the register's value now taken in. Pure.

    * a rise is remembered (`peak_kwh`);
    * a fall of more than `REGISTER_TOLERANCE_KWH` below the highest value since the baseline is a register that
      started counting again (a session register at a plug-in or a restart, a meter reading 0 while its charger
      reboots): what it had counted is carried and it is measured on from the value it fell to. Whatever it
      counted before it fell was delivered after the reading, so nothing is invented; what it counted while it
      was not watched is lost, an under-count;
    * a register back at or above the value it last fell from was a glitch: it is measured on from that value
      with the carry it had then, and what it showed while down is dropped (a session register that counted that
      far again is under-counted, never counted twice).
    """
    if anchor is None or anchor.register_kwh is None or register_kwh is None:
        return anchor
    base = anchor.register_kwh
    peak = max(base, anchor.peak_kwh if anchor.peak_kwh is not None else base)
    reset_from = anchor.reset_from_kwh
    if reset_from is not None and register_kwh >= reset_from - REGISTER_TOLERANCE_KWH:
        return replace(
            anchor,
            register_kwh=reset_from,
            carried_kwh=anchor.resume_carried_kwh,
            reset_from_kwh=None,
            resume_carried_kwh=0.0,
            peak_kwh=max(register_kwh, reset_from),
        )
    if register_kwh < peak - REGISTER_TOLERANCE_KWH:
        carried = round(anchor.carried_kwh + max(0.0, peak - base), 6)
        return replace(
            anchor,
            register_kwh=register_kwh,
            carried_kwh=carried,
            reset_from_kwh=peak if reset_from is None else reset_from,
            resume_carried_kwh=carried if reset_from is None else anchor.resume_carried_kwh,
            peak_kwh=register_kwh,
        )
    if register_kwh > peak or anchor.peak_kwh is None:
        return replace(anchor, peak_kwh=max(peak, register_kwh))
    return anchor


def estimate_soc_percent(
    anchor: SocAnchor,
    *,
    register_kwh: float | None,
    capacity_kwh: float | None,
    ceiling_percent: float | None = None,
) -> float | None:
    """The state of charge carried forward from `anchor` by the energy delivered since, or `None`.

    `None` without a register (now or at the anchor), without a positive capacity, or when the
    register went backwards (`delivered_since_kwh`). Clamped to the car's own charge limit (`ceiling_percent`,
    else 100): a car takes nothing past it, so energy counted beyond it is charging loss, not charge. Never below
    the anchor's own reading.
    """
    if capacity_kwh is None or not capacity_kwh > 0:
        return None
    delivered = delivered_since_kwh(anchor, register_kwh)
    if delivered is None:
        return None
    ceiling = charge_ceiling_percent(ceiling_percent)
    estimate = min(ceiling, anchor.soc_percent + delivered * CHARGE_EFFICIENCY / capacity_kwh * 100.0)
    return max(anchor.soc_percent, estimate)


def register_value_at(samples: Iterable[RegisterSample], at: datetime, until: datetime) -> float | None:
    """The register's value at `at`, interpolated between the values posted around it, or `None` unless the
    register was posting steadily from then to `until`. Pure.

    Steadily: a value posted at most `REGISTER_STEADY_GAP_S` before `at` and one at most that after it, no longer
    gap between values from then to `until` (nor from the last one to `until`), no unreadable value and no fall
    of more than `REGISTER_TOLERANCE_KWH` (a reset: interpolated only within one unbroken run). A register that
    posts this often books its energy as it is charged, so its value at `at` holds nothing charged after `at`.
    """
    ordered = sorted(dict(samples).items())
    start = None
    for index, (posted_at, _value) in enumerate(ordered):
        if posted_at > at:
            break
        start = index
    if start is None:
        return None
    window = [sample for sample in ordered[start:] if sample[0] <= until]
    gap = timedelta(seconds=REGISTER_STEADY_GAP_S)
    if not window or at - window[0][0] > gap or until - window[-1][0] > gap:
        return None
    previous: tuple[datetime, float] | None = None
    for posted_at, value in window:
        if value is None:
            return None
        if previous is not None and (
            posted_at - previous[0] > gap or value < previous[1] - REGISTER_TOLERANCE_KWH
        ):
            return None
        previous = (posted_at, value)
    before_at, before = window[0]
    if before_at == at:
        return before
    if len(window) < 2:
        return None
    after_at, after = window[1]
    assert before is not None and after is not None
    if after_at - at > gap:
        return None
    share = (at - before_at).total_seconds() / (after_at - before_at).total_seconds()
    return before + (after - before) * share


def resolve_soc(
    *,
    reading: SocReading | None,
    anchor: SocAnchor | None,
    register_kwh: float | None,
    capacity_kwh: float | None,
    now: datetime,
    vehicle_id: str | None,
    register_entity_id: str | None = None,
    plugged_in_throughout: bool = False,
    ceiling_percent: float | None = None,
    register_updated_at: datetime | None = None,
    since_restart: bool = False,
    register_history: Iterable[RegisterSample] = (),
) -> SocResolution:
    """The state of charge to plan and stop against, and the anchor to keep. Pure.

    * a fresh usable reading is used as is and becomes the anchor;
    * otherwise the anchor carries the reading forward by delivered energy and the result is
      flagged `estimated` (also when the entity is `unavailable`, as a sleeping car is);
    * a stale reading newer than the anchor replaces it only while nothing has been delivered
      since the anchor (a car driven or charged elsewhere while idle). A reading timed by the car's own report
      time (`SocReading.time_basis` `vehicle_timestamp`) that is newer than the anchor is a report the car made
      since, and replaces it however old it is: its baseline follows the rule below, so the energy between the
      report and the baseline is not counted (an under-count);
    * a reading timed by the car whose value moved from the anchor's while its report time did not (the time
      written after the value, or a sensor that times something else) is timed by its `last_updated` instead;
    * with no anchor a stale reading starts one;
    * the baseline (`SocAnchor.register_kwh`) is the first register value posted at or after the reading
      (`register_updated_at`; `None` means not known and takes the value as current). A register that lags
      books energy charged before the reading later: a baseline posted before the reading would count that
      energy on top of the reading (the field night of 12 October: a 77 % reading at 02:48 measured from a
      value posted at 02:16, and the 03:05 step, charged before 02:45, counted again). Until such a value
      arrives the anchor has no baseline and the reading is not carried forward; energy charged between the
      reading and that value is not counted. It can only under-count, provided the register's first value
      after a reading holds what was charged before it (a register that lags past its own next update is not
      caught). One exception: a reading timed by the car's own report time that arrived after it
      is measured from the register's value at the report time when `register_history` (the values seen
      lately) shows the register posting steadily from then to now with no reset (`register_value_at`), and
      that value is not above the one the rule above would take. A register that posts that often does not lag,
      so nothing charged before the report time is counted, and the energy between the report and its arrival
      is (the field night of 21-22 October: a 70 % made at 02:47:54 and seen at 03:02:51 left 2.7 kWh out). The
      anchor's `baseline` says which rule gave its baseline;
    * an anchor's baseline from another register than `register_entity_id` is dropped and taken again under
      the same rule: energy before it is not credited, so it can only under-count;
    * a register that falls back is followed (`track_register`): what it counted is carried, never subtracted;
    * whether nothing was delivered since the anchor (the stale reading's replacement above) is told by the
      register; a lagging register can make it look so while energy was charged, and the newer reading then
      replaces the anchor with a baseline under the rule above, which can only under-count;
    * a reading timed by its `last_updated` with the anchor's value is the anchor's own reading set again, not a
      new one, when it is the entity coming back (`SocReading.restored`: a reload or late load of its
      integration) while the car is shown to have stayed plugged in through the gap (`plugged_in_throughout`),
      or when it is the first state SpotNav has seen of it since it started (`since_restart`: Home Assistant
      restarted and the car's integration set its last value again, stamped with the start; the field night of
      13-14 October, a 43 % hours old taken as a fresh reading and the evening's charge forgotten). The anchor
      and its baseline stay, whatever the register reads, and the reading's age is the anchor's. A value that
      moved, or a car report time newer than the anchor, is a new reading;
    * the estimate is bounded by the car's own charge limit (`ceiling_percent`, `estimate_soc_percent`);
    * where no estimate can be made the raw reading is returned untouched, or `None`.
    """
    if anchor is not None and anchor.vehicle_id != vehicle_id:
        anchor = None
    if (
        anchor is not None
        and anchor.register_kwh is not None
        and anchor.register_entity_id != register_entity_id
    ):
        # A baseline read from another meter (a register found again, or chosen): never subtracted, and nothing it
        # carried is credited.
        anchor = SocAnchor(
            anchor.soc_percent, None, anchor.read_at, anchor.source, anchor.vehicle_id, anchor.register_entity_id,
            time_basis=anchor.time_basis,
        )
    if (
        anchor is not None
        and anchor.register_kwh is None
        and register_kwh is not None
        and _posted_after(register_updated_at, anchor.read_at)
    ):
        # The first value the register posted after the reading (or the first readable one, after a restart
        # before the charger's integration had its register): what it had booked of the energy charged before
        # the reading is in it. Its value at the car's report time instead, when it was seen posting steadily.
        baseline, rule = _baseline(
            anchor.read_at, anchor.time_basis, _arrived_at(reading, anchor.read_at, now),
            register_kwh, register_updated_at, register_history, now,
        )
        anchor = replace(
            anchor, register_kwh=baseline, register_entity_id=register_entity_id, peak_kwh=register_kwh,
            baseline=rule,
        )
    anchor = track_register(anchor, register_kwh)
    usable = reading is not None and reading.soc_percent is not None
    read_at = now
    if usable:
        assert reading is not None
        read_at = _read_at(reading, now)
        if (
            reading.time_basis == "vehicle_timestamp"
            and anchor is not None
            and reading.soc_percent != anchor.soc_percent
            and not _newer(read_at, anchor)
            and reading.state_age_s is not None
        ):
            # The value moved but the car's report time did not: the state's own time says when it came.
            reading = replace(reading, age_s=reading.state_age_s, time_basis="last_updated", time_entity_id=None)
            read_at = _read_at(reading, now)
    restored = (
        usable
        and reading is not None
        and reading.time_basis == "last_updated"
        and anchor is not None
        and reading.soc_percent == anchor.soc_percent
        and ((reading.restored and plugged_in_throughout) or (since_restart and _newer(read_at, anchor)))
    )
    if usable and not restored:
        assert reading is not None and reading.soc_percent is not None
        newer = anchor is None or _newer(read_at, anchor)
        if is_fresh(reading):
            if newer:
                anchor = _new_anchor(
                    reading, read_at, vehicle_id, register_kwh, register_entity_id, register_updated_at,
                    register_history, now,
                )
            return SocResolution(reading, anchor)
        if newer:
            delivered = None if anchor is None else delivered_since_kwh(anchor, register_kwh)
            idle = delivered is None or delivered <= REGISTER_TOLERANCE_KWH
            if idle or reading.time_basis == "vehicle_timestamp":
                anchor = _new_anchor(
                    reading, read_at, vehicle_id, register_kwh, register_entity_id, register_updated_at,
                    register_history, now,
                )
    if anchor is None:
        return SocResolution(reading, None)
    if restored:
        assert reading is not None
        # The anchor's own reading, set again by the start: as old as the anchor.
        reading = replace(reading, age_s=max(0.0, (now - anchor.read_at).total_seconds()), time_basis="anchor")
    estimate = estimate_soc_percent(
        anchor, register_kwh=register_kwh, capacity_kwh=capacity_kwh, ceiling_percent=ceiling_percent
    )
    delivered = delivered_since_kwh(anchor, register_kwh)
    delivered_something = estimate is not None and delivered is not None and delivered > REGISTER_TOLERANCE_KWH
    if estimate is None or (usable and not delivered_something):
        # Nothing to add to the reading (or nothing to add it with): it stands as reported.
        return SocResolution(reading, anchor)
    return SocResolution(
        SocReading(
            soc_percent=estimate,
            source=anchor.source,
            entity_id=reading.entity_id if reading is not None else None,
            vehicle_id=vehicle_id,
            age_s=max(0.0, (now - anchor.read_at).total_seconds()),
            estimated=True,
            time_basis="anchor",
        ),
        anchor,
    )


def _read_at(reading: SocReading, now: datetime) -> datetime:
    """When a reading was made, by its age (`now` when its age is not known)."""
    age = reading.age_s
    return now - timedelta(seconds=max(0.0, age)) if age is not None else now


def _newer(read_at: datetime, anchor: SocAnchor) -> bool:
    """Whether a reading made at `read_at` is another one than the anchor's, made after it."""
    return (read_at - anchor.read_at).total_seconds() > SAME_READING_S


def _posted_after(register_updated_at: datetime | None, read_at: datetime) -> bool:
    """Whether a register value was posted after a reading, or at the same instant (`None`: not known, taken as
    current)."""
    return register_updated_at is None or register_updated_at >= read_at


def _arrived_at(reading: SocReading | None, read_at: datetime, now: datetime) -> datetime | None:
    """When the reading made at `read_at` arrived, by its state's own time, when `reading` is that reading timed
    by the car's report time; `None` when not known."""
    if (
        reading is None
        or reading.soc_percent is None
        or reading.time_basis != "vehicle_timestamp"
        or reading.state_age_s is None
        or abs((_read_at(reading, now) - read_at).total_seconds()) > SAME_READING_S
    ):
        return None
    return now - timedelta(seconds=max(0.0, reading.state_age_s))


def _baseline(
    read_at: datetime,
    time_basis: TimeBasis,
    arrived_at: datetime | None,
    register_kwh: float | None,
    register_updated_at: datetime | None,
    register_history: Iterable[RegisterSample],
    now: datetime,
) -> tuple[float | None, BaselineRule | None]:
    """The baseline for a reading made at `read_at` and the rule that gave it (`resolve_soc`): none while the
    register's value was posted before the reading; its value at the car's report time when that came before the
    arrival and the register was posting steadily from then to now (and that value is not above the present one);
    else the present value."""
    if register_kwh is None or not _posted_after(register_updated_at, read_at):
        return None, None
    if time_basis == "vehicle_timestamp" and arrived_at is not None and read_at < arrived_at:
        samples = list(register_history)
        if register_updated_at is not None:
            samples.append((register_updated_at, register_kwh))
        at_report = register_value_at(samples, read_at, now)
        if at_report is not None and at_report <= register_kwh:
            return at_report, "report_time"
    return register_kwh, "after_arrival"


def _new_anchor(
    reading: SocReading,
    read_at: datetime,
    vehicle_id: str | None,
    register_kwh: float | None,
    register_entity_id: str | None,
    register_updated_at: datetime | None,
    register_history: Iterable[RegisterSample],
    now: datetime,
) -> SocAnchor:
    """An anchor for `reading`, with a baseline (`_baseline`) only when the register was posted after the
    reading."""
    assert reading.soc_percent is not None
    basis: TimeBasis = "vehicle_timestamp" if reading.time_basis == "vehicle_timestamp" else "last_updated"
    baseline, rule = _baseline(
        read_at, basis, _arrived_at(reading, read_at, now), register_kwh, register_updated_at, register_history, now
    )
    return SocAnchor(
        reading.soc_percent, baseline, read_at, reading.source, vehicle_id, register_entity_id,
        time_basis=basis, peak_kwh=register_kwh if baseline is not None else None, baseline=rule,
    )


def target_need_kwh(
    *,
    soc_percent: float,
    capacity_kwh: float | None,
    target_percent: float | None,
    vehicle_max_percent: float | None = None,
) -> tuple[str, float | None]:
    """`(reason, wall kWh)` a target needs from `soc_percent`, charging loss included.

    `reason` is `planner.resolve_target_energy`'s; the energy is
    `(target - soc) x capacity / CHARGE_EFFICIENCY`, `None` without a capacity.
    """
    resolution = resolve_target_energy(
        TargetEnergyRequest(
            soc_percent=soc_percent,
            capacity_kwh=capacity_kwh,
            stored_target_percent=None if target_percent is None else round(target_percent),
            vehicle_max_percent=vehicle_max_percent,
            apply_default_target=False,
        )
    )
    if resolution.kwh is None:
        return resolution.reason, None
    return resolution.reason, resolution.kwh / CHARGE_EFFICIENCY


def battery_room_kwh(
    *, soc_percent: float | None, capacity_kwh: float | None, vehicle_max_percent: float | None = None
) -> float | None:
    """The wall energy the battery still has room for: `capacity x (ceiling - soc) / 100 / efficiency`,
    the ceiling being the car's own charge limit (else 100), zero at or above it; `None` without a level
    or a battery size."""
    if soc_percent is None:
        return None
    _, kwh = target_need_kwh(
        soc_percent=soc_percent,
        capacity_kwh=capacity_kwh,
        target_percent=100,
        vehicle_max_percent=vehicle_max_percent,
    )
    return kwh


def read_energy_register_kwh(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A cumulative energy register in kWh, or `None` when unreadable or not a meter.

    Requires a numeric state, `Wh`/`kWh` and `state_class: total_increasing`, so a session or
    momentary value that falls back to zero is never misread as a reset.
    """
    return read_energy_register(hass, entity_id)[0]


def read_energy_register(hass: HomeAssistant, entity_id: str | None) -> tuple[float | None, datetime | None]:
    """`read_energy_register_kwh` and when the register posted that value (its `last_updated`), or
    `(None, None)`."""
    if not entity_id:
        return None, None
    state = hass.states.get(entity_id)
    value = _register_kwh(state)
    return (None, None) if value is None or state is None else (value, state.last_updated)


def _register_kwh(state: Any) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    attributes = state.attributes or {}
    if attributes.get("device_class") != "energy":
        return None
    if attributes.get("state_class") != "total_increasing":
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    unit = attributes.get("unit_of_measurement")
    if unit == "Wh":
        return value / 1000.0
    if unit == "kWh":
        return value
    return None


class SocReader:
    """One charger's state-of-charge source: raw sources in, the effective reading out.

    `raw_reader(vehicle_id)` is the preferred-source read (charger while live, else vehicle);
    this class adds the anchor, estimate, persistence and watch. Public methods are safe to call
    from the event loop and none awaits.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        raw_reader: Callable[[str | None], SocReading | None],
        register_entity_id: Callable[[], str | None],
        charge_control: Callable[[], str | None],
        remembered_capacity: Callable[[str | None], float | None],
        now: Callable[[], datetime] | None = None,
        plugged_in_since: Callable[[], datetime | None] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._now: Callable[[], datetime] = now if now is not None else dt_util.utcnow
        self._raw_reader = raw_reader
        self._register_entity_id = register_entity_id
        self._charge_control = charge_control
        self._remembered_capacity = remembered_capacity
        # Since when the charger has itself seen the car plugged in without a gap (`None`: not shown).
        self._plugged_in_since = plugged_in_since
        # Per source entity: the instant of the state with which it came back (from `unavailable`,
        # `unknown` or absent) and since when it had been gone, until it updates a value it had.
        self._comebacks: dict[str, tuple[datetime, datetime | None]] = {}
        self._gone_at: dict[str, datetime] = {}
        # Source entities seen with a value while watched (an update of it, or its going away): their state is no
        # longer the one they had when SpotNav started (`resolve_soc`'s `since_restart`).
        self._seen_live: set[str] = set()
        # When a register that only rose was last saved with the anchor (`PEAK_SAVE_INTERVAL_S`).
        self._peak_saved_at: datetime | None = None
        # What the last resolution was made of, for the diagnostics.
        self._last_reading: SocReading | None = None
        self._last_since_restart = False
        self._store: Store[dict[str, Any]] = Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}")
        self._anchor: SocAnchor | None = None
        self._facts_cache: dict[str, tuple[datetime, float | None, float | None]] = {}
        self._watch_cancel: Callable[[], None] | None = None
        self._watched: tuple[str, ...] = ()
        self._watch_vehicle: str | None = None
        self._on_reading: Callable[[], None] | None = None
        self._last_notified: float | None = None
        self._notify_next = False
        # While a calculation lacks a level: the stored vehicle it was for, and the listener for a battery
        # sensor's state anywhere (one with no state yet resolves to nothing `ensure_watch` could watch).
        self._awaited_vehicle: str | None = None
        self._arrival_cancel: Callable[[], None] | None = None
        self._closed = False

    async def async_load(self) -> None:
        """Restore the anchor, so a restart in the middle of a charge keeps the estimate."""
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self._anchor = SocAnchor.from_dict(saved.get("anchor"))

    @staticmethod
    async def async_remove_stored(hass: HomeAssistant, entry_id: str) -> None:
        """The entry is gone for good: forget its anchor."""
        await Store(hass, _STORE_VERSION, f"{_STORE_KEY_PREFIX}.{entry_id}").async_remove()

    @callback
    def async_shutdown(self) -> None:
        self._closed = True
        self._drop_watch()
        self._drop_arrival()

    def await_reading(self, stored_vehicle_id: str | None) -> None:
        """A calculation found no usable level: tell `set_on_reading`'s callback of the first one that
        arrives, whatever it is (the same value as the last one heard included). At start-up the car's
        sensor may get its first state only after the planner ran; until then it resolves to nothing to
        watch, so any battery sensor's state is listened for and the vehicle resolved again on each."""
        if self._closed:
            return
        self._notify_next = True
        self._awaited_vehicle = stored_vehicle_id
        if self._arrival_cancel is None:
            self._arrival_cancel = self._hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._on_any_battery_state, event_filter=_battery_state
            )

    def _drop_arrival(self) -> None:
        if self._arrival_cancel is not None:
            self._arrival_cancel()
        self._arrival_cancel = None

    @callback
    def _on_any_battery_state(self, _event: Event[EventStateChangedData]) -> None:
        vehicle_id, _ = resolve_target_vehicle(
            self._hass, self._awaited_vehicle, charger_vehicle_ids(self._hass, self._entry_id)
        )
        self.ensure_watch(vehicle_id)
        reading = self.read(vehicle_id)
        if reading is not None and reading.soc_percent is not None:
            self._tell(reading)

    def set_on_reading(self, callback_: Callable[[], None] | None) -> None:
        """Ask to be told when a reading moved enough to make a new plan worth calculating."""
        self._on_reading = callback_

    def notify_next_reading(self) -> None:
        """Tell `set_on_reading`'s callback of the next reading that arrives, however little it moved: the
        planner waits for the car's new level after a charge (`vehicle_update_wait`), and even the same
        value reported again is the answer it waits for."""
        self._notify_next = True

    def _vehicle_facts(self, vehicle_id: str | None) -> tuple[float | None, float | None]:
        """`(reported capacity kWh, vehicle ceiling %)` for `vehicle_id`, reused for a few minutes."""
        if not vehicle_id:
            return (None, None)
        now = self._now()
        cached = self._facts_cache.get(vehicle_id)
        if cached is not None and (now - cached[0]).total_seconds() < FACTS_CACHE_S:
            return (cached[1], cached[2])
        capacity: float | None = None
        ceiling: float | None = None
        for candidate in discover_vehicles(self._hass):
            if candidate.id == vehicle_id:
                capacity = candidate.battery_capacity_kwh
                ceiling = candidate.target_soc_percent_max
                break
        if capacity is None and cached is not None:
            # A sleeping car drops out of discovery; its pack size has not changed.
            capacity = cached[1]
        if ceiling is None and cached is not None:
            ceiling = cached[2]
        self._facts_cache[vehicle_id] = (now, capacity, ceiling)
        return (capacity, ceiling)

    def capacity_with_source(self, vehicle_id: str | None) -> tuple[float | None, str | None]:
        """`(kWh, "reported" | "stored")`: the vehicle's own figure, else the stored one; `(None, None)` if missing."""
        reported, _ = self._vehicle_facts(vehicle_id)
        if reported is not None and reported > 0:
            return reported, "reported"
        remembered = self._remembered_capacity(vehicle_id)
        if remembered is not None and remembered > 0:
            return remembered, "stored"
        return None, None

    def capacity_kwh(self, vehicle_id: str | None) -> float | None:
        """The battery size: reported by the vehicle, else the one stored, else missing."""
        return self.capacity_with_source(vehicle_id)[0]

    def reported_capacity_kwh(self, vehicle_id: str | None) -> float | None:
        reported = self._vehicle_facts(vehicle_id)[0]
        return reported if reported is not None and reported > 0 else None

    def vehicle_max_percent(self, vehicle_id: str | None) -> float | None:
        return self._vehicle_facts(vehicle_id)[1]

    def vehicle_name(self, vehicle_id: str | None) -> str | None:
        if not vehicle_id:
            return None
        device = dr.async_get(self._hass).async_get(vehicle_id)
        return None if device is None else _device_name(device)

    def has_source(self, vehicle_id: str | None) -> bool:
        """Whether any state-of-charge entity resolves (value not required)."""
        return charger_soc_entity_id(self._hass, self._charge_control()) is not None or (
            vehicle_soc_entity_id(self._hass, vehicle_id) is not None
        )

    def read(self, vehicle_id: str | None) -> SocReading | None:
        """The effective state of charge now: a fresh reading, else the estimate, else the raw one.

        For the car the charger plans for: its reading moves the one anchor (a fresh reading, or another car
        chosen). A read for no car at all is only a look (`peek`) while a car holds the anchor: it never drops
        that car's anchor.
        """
        if not vehicle_id and self._anchor is not None and self._anchor.vehicle_id:
            return self.peek(vehicle_id)
        resolution = self._resolve(vehicle_id, self._anchor)
        previous, self._anchor = self._anchor, resolution.anchor
        if resolution.anchor != previous:
            self._save()
        elif resolution.anchor is not None and previous is not None and resolution.anchor.peak_kwh != previous.peak_kwh:
            # Only the register's rise: saved now and then, so a restart that finds a fallen register carries it.
            now = self._now()
            if self._peak_saved_at is None or (now - self._peak_saved_at).total_seconds() >= PEAK_SAVE_INTERVAL_S:
                self._save()
        return resolution.reading

    def peek(self, vehicle_id: str | None) -> SocReading | None:
        """`read` without touching the anchor: for a car only shown (another car at the charger in the card's
        list, a site's overview), never the one planned for. The anchor's car gets its estimate; any other car
        its reading as it is.

        One anchor is kept per charger. Before, a dashboard that listed every car at the charger read each of
        them through `read`: the second car took the anchor, and the planned car's next read anchored its stale
        reading again at the register's present value, so nothing it had been given counted any more.
        """
        anchor = self._anchor if self._anchor is not None and self._anchor.vehicle_id == vehicle_id else None
        return self._resolve(vehicle_id, anchor).reading

    def _resolve(self, vehicle_id: str | None, anchor: SocAnchor | None) -> SocResolution:
        register_entity_id = self._register_entity_id()
        reading, throughout = self._with_comeback(self._raw_reader(vehicle_id))
        since_restart = self._since_restart(reading)
        self._last_reading, self._last_since_restart = reading, since_restart
        register_kwh, register_updated_at = read_energy_register(self._hass, register_entity_id)
        return resolve_soc(
            reading=reading,
            anchor=anchor,
            register_kwh=register_kwh,
            register_updated_at=register_updated_at,
            capacity_kwh=self.capacity_kwh(vehicle_id),
            now=self._now(),
            vehicle_id=vehicle_id,
            register_entity_id=register_entity_id,
            plugged_in_throughout=throughout,
            ceiling_percent=self.vehicle_max_percent(vehicle_id),
            since_restart=since_restart,
        )

    def _since_restart(self, reading: SocReading | None) -> bool:
        """Whether `reading` is its entity's state as SpotNav found it when it started (`resolve_soc`'s
        `since_restart`): no update of it, and no going away, seen since. A reload of SpotNav counts as a start:
        the state then is the one the car's integration last wrote, whenever that was."""
        return reading is not None and reading.entity_id is not None and reading.entity_id not in self._seen_live

    def diagnostics(self) -> dict[str, Any]:
        """The kept anchor and what the last reading's age was measured from (no entity's value beyond the
        state of charge, no name)."""
        reading = self._last_reading
        return {
            "anchor": None if self._anchor is None else self._anchor.as_dict(),
            "last_reading": None
            if reading is None
            else {
                "source": reading.source,
                "soc_percent": reading.soc_percent,
                "age_s": reading.age_s,
                "time_basis": reading.time_basis,
                "time_entity_id": reading.time_entity_id,
                "state_age_s": reading.state_age_s,
                "restored": reading.restored,
                "since_restart": self._last_since_restart,
            },
        }

    def _with_comeback(self, reading: SocReading | None) -> tuple[SocReading | None, bool]:
        """`reading` flagged `restored` when its state is its entity coming back, and whether the charger saw
        the car plugged in since before the entity went away (`resolve_soc`'s `plugged_in_throughout`).

        The first state seen after SpotNav set up (a restart, a reload) has no gap the charger watched across,
        so it is never shown continuous: a new reading, as an unplug or an unknown connection makes it."""
        if reading is None or reading.entity_id is None:
            return reading, False
        comeback = self._comebacks.get(reading.entity_id)
        state = self._hass.states.get(reading.entity_id)
        if comeback is None or state is None or state.last_updated != comeback[0]:
            return reading, False
        gone_since = comeback[1]
        since = None if self._plugged_in_since is None else self._plugged_in_since()
        throughout = since is not None and gone_since is not None and since <= gone_since
        return replace(reading, restored=True), throughout

    @callback
    def _note_source_state(self, event: Event[EventStateChangedData]) -> None:
        """Remember when a source entity went away and the state it came back with."""
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        gone = (STATE_UNAVAILABLE, STATE_UNKNOWN)
        if old is not None and old.state not in gone:
            # It had a value while watched, and it changed or went away: what it shows next is no start's.
            self._seen_live.add(entity_id)
        if new is None or new.state in gone:
            if old is not None and old.state not in gone:
                self._gone_at[entity_id] = new.last_updated if new is not None else self._now()
            self._comebacks.pop(entity_id, None)
            return
        if old is None or old.state in gone:
            gone_since = self._gone_at.pop(entity_id, None)
            if old is not None and gone_since is None:
                gone_since = old.last_updated
            self._comebacks[entity_id] = (new.last_updated, gone_since)
        else:
            # An update of a value it had (the same value with a new poll included): a reading.
            self._comebacks.pop(entity_id, None)

    def _save(self) -> None:
        if self._closed:
            return
        self._peak_saved_at = self._now()
        self._store.async_delay_save(
            lambda: {"anchor": None if self._anchor is None else self._anchor.as_dict()}, 1.0
        )

    def ensure_watch(self, vehicle_id: str | None) -> None:
        """Watch the source entities so a fresh reading is anchored the moment it arrives."""
        if self._closed:
            return
        entity_ids = tuple(
            entity_id
            for entity_id in (
                charger_soc_entity_id(self._hass, self._charge_control()),
                vehicle_soc_entity_id(self._hass, vehicle_id),
            )
            if entity_id
        )
        self._watch_vehicle = vehicle_id
        if entity_ids == self._watched:
            return
        self._drop_watch()
        self._watched = entity_ids
        if entity_ids:
            self._watch_cancel = async_track_state_change_event(
                self._hass, list(entity_ids), self._on_source_changed
            )

    def _drop_watch(self) -> None:
        if self._watch_cancel is not None:
            self._watch_cancel()
        self._watch_cancel = None
        self._watched = ()

    @callback
    def _on_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self._note_source_state(event)
        reading = self.read(self._watch_vehicle)
        if reading is None or reading.soc_percent is None or reading.estimated:
            return
        self._tell(reading)

    def _tell(self, reading: SocReading) -> None:
        """Ask for a new calculation on `reading`: always while one lacked a level, else when it moved."""
        assert reading.soc_percent is not None
        last = self._last_notified
        if (
            not self._notify_next
            and last is not None
            and abs(reading.soc_percent - last) < RECALCULATE_DELTA_PERCENT
        ):
            return
        self._notify_next = False
        self._drop_arrival()
        self._last_notified = reading.soc_percent
        if self._on_reading is not None:
            self._on_reading()


@callback
def _battery_state(event_data: EventStateChangedData) -> bool:
    """A sensor state that could be a state of charge (`await_reading`'s filter, run for every state)."""
    new = event_data["new_state"]
    return (
        new is not None
        and new.domain == "sensor"
        and new.attributes.get("device_class") == "battery"
    )
