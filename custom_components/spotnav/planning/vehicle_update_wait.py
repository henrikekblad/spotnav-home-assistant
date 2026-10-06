"""After a target charge, wait for the car's new level instead of planning the same need again.

A car's cloud often reports nothing new for a long while after a charge. Where the state of charge
cannot be carried forward by the energy register (`soc_estimate.resolve_soc`: no register then, a register
that started again, no baseline), the planner would see the reading from before the charge and plan the
same need again. `decide_vehicle_update_wait` says when not to: pure, facts in, a decision out.

The evidence is only *measured* energy: what the charger's recorded sessions counted from its energy
register or from SpotNav's integration of a smart plug's power (`sessions.model.SOURCE_REGISTER`,
`SOURCE_INTEGRATED`). A session estimated from the current the charger was asked for, or the plan's own
periods times its power, says what was meant to happen, not what did, and counts for nothing; with no
measurement at all nothing changes from planning as before.

It waits while all of these hold:

* the reading is a reading (not an estimate, which already counts the energy) with a known age;
* the charge has ended (not charging, no window of the plan still ahead) and the car is not known unplugged;
* the charger knows when the car was plugged in, and the measured energy of the sessions of that plug-in
  that began at or after the reading, for this car (a session that recorded no car only when the charger
  has this one car), is at least the need worked out from it. Without a known plug-in, energy cannot be
  told to be this car's, and nothing changes;
* no departure is near: with one, it waits only until the need, if the reading were true, would just still
  fit at the charger's power (with `DEPARTURE_MARGIN_S` to spare), and that instant is `replan_at`.

A newer reading, an unplug or that instant ends the wait; the planner then plans from what it reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from ..sessions.model import ChargeSession, SOURCE_INTEGRATED, SOURCE_REGISTER

#: Where a session's energy was measured, not estimated.
MEASURED_SOURCES: Final = (SOURCE_REGISTER, SOURCE_INTEGRATED)

#: Measured energy this close below the need still covers it (meter rounding).
NEED_TOLERANCE_KWH: Final = 0.05

#: Time kept in hand before a departure: the wait ends this long before the need would no longer fit.
DEPARTURE_MARGIN_S: Final = 30 * 60.0

WaitReason = Literal[
    "waiting",
    "estimated",
    "no_reading_age",
    "charging",
    "window_ahead",
    "unplugged",
    "no_plug_in",
    "no_measurement",
    "short_of_need",
    "departure_close",
]


@dataclass(frozen=True, slots=True)
class MeasuredDelivery:
    """The measured energy of the sessions that count, when the first of them began, and the last one's
    id (one charge to ask the car about once)."""

    kwh: float
    first_start: datetime
    last_session_id: str


@dataclass(frozen=True, slots=True)
class WaitDecision:
    wait: bool
    reason: WaitReason
    #: While waiting for a departure: when to plan again regardless.
    replan_at: datetime | None = None
    delivery: MeasuredDelivery | None = None


def measured_delivery_since(
    sessions: Iterable[ChargeSession],
    *,
    read_at: datetime,
    plugged_in_at: datetime,
    vehicle_id: str | None,
    only_vehicle: bool = False,
) -> MeasuredDelivery | None:
    """The measured energy of the sessions that began at or after both `read_at` and the plug-in, for
    `vehicle_id` (a session that recorded no vehicle only when `only_vehicle`: the charger has this one
    car); `None` when there is none."""
    counted = sorted(
        (
            session
            for session in sessions
            if session.energy_source in MEASURED_SOURCES
            and session.source is None
            and session.energy_kwh > 0
            and session.start >= read_at
            and session.start >= plugged_in_at
            and (
                (vehicle_id is not None and session.vehicle_id == vehicle_id)
                or (session.vehicle_id is None and only_vehicle)
            )
        ),
        key=lambda session: session.start,
    )
    if not counted:
        return None
    return MeasuredDelivery(
        kwh=sum(session.energy_kwh for session in counted),
        first_start=counted[0].start,
        last_session_id=counted[-1].id,
    )


def decide_vehicle_update_wait(
    *,
    need_kwh: float,
    reading_age_s: float | None,
    estimated: bool,
    sessions: Iterable[ChargeSession],
    plugged_in_at: datetime | None,
    connected: bool | None,
    charging: bool,
    window_ahead: bool,
    departure_at: datetime | None,
    power_kw: float | None,
    vehicle_id: str | None,
    now: datetime,
    only_vehicle: bool = False,
) -> WaitDecision:
    """Whether to wait for the car's new level rather than plan `need_kwh` (wall energy) again."""
    if estimated:
        return WaitDecision(False, "estimated")
    if reading_age_s is None:
        return WaitDecision(False, "no_reading_age")
    if charging:
        return WaitDecision(False, "charging")
    if window_ahead:
        return WaitDecision(False, "window_ahead")
    if connected is False:
        return WaitDecision(False, "unplugged")
    if plugged_in_at is None:
        return WaitDecision(False, "no_plug_in")
    read_at = now - timedelta(seconds=max(0.0, reading_age_s))
    delivery = measured_delivery_since(
        sessions, read_at=read_at, plugged_in_at=plugged_in_at, vehicle_id=vehicle_id, only_vehicle=only_vehicle
    )
    if delivery is None:
        return WaitDecision(False, "no_measurement")
    if delivery.kwh + NEED_TOLERANCE_KWH < need_kwh:
        return WaitDecision(False, "short_of_need", delivery=delivery)
    if departure_at is None:
        return WaitDecision(True, "waiting", delivery=delivery)
    if power_kw is None or not power_kw > 0:
        # How long the need would take is unknown: the departure's safety wins.
        return WaitDecision(False, "departure_close", delivery=delivery)
    latest = departure_at - timedelta(hours=need_kwh / power_kw, seconds=DEPARTURE_MARGIN_S)
    if now >= latest:
        return WaitDecision(False, "departure_close", delivery=delivery)
    return WaitDecision(True, "waiting", replan_at=latest, delivery=delivery)
