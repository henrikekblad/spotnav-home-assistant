"""Price wait: what to do when the prices a plan needs are not published yet.

Applies the slack principle of `planning/hybrid_plan.py` to time instead of sun:

    publication_at = next expected day-ahead publication for the missing day, plus a margin
    wait_capacity  = hours from max(publication_at, now) to the deadline x max_charge_kw x margin
    must_buy_now   = max(0, need - wait_capacity)

* wait: `must_buy_now == 0`; everything still fits after the publication, so nothing is planned.
* buy_now: only the part that cannot wait is bought, in the cheapest *known* intervals before
  the publication; the rest is planned against real prices once they arrive.
* guarantee: the latest safe start has arrived and prices are still missing; the remainder is
  charged at once at unknown prices. The deadline is kept, only the price is given up.

Pure: no Home Assistant, no clock, no storage. The publication instant is built in the market's
zone and converted to UTC before any subtraction, so clock-change days cannot shift it. Comparisons
carry a small epsilon so a need exactly equal to the wait capacity waits instead of buying a sliver.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Final, Literal
from zoneinfo import ZoneInfo


#: Fraction of the charger's maximum rate the deadline must still be reachable at (same derating as hybrid).
FEASIBILITY_MARGIN: Final = 0.8

#: Day-ahead auctions clear on one European clock (prices appear around 13:00 Brussels time,
#: see `price_refresh.PUBLICATION_WINDOW_START`), so one constant in that zone serves every area.
PUBLICATION_ZONE: Final = "Europe/Brussels"
PUBLICATION_LOCAL_TIME: Final = time(13, 0)

#: How long after the expected instant a publication is still "on time" for planning purposes.
PUBLICATION_MARGIN: Final = timedelta(minutes=45)

#: A plan starts on the next quarter-hour boundary, so the guarantee is due one slot before the latest safe start.
START_LAG: Final = timedelta(minutes=15)

#: Energy comparisons tolerate this much float noise (kWh).
EPSILON_KWH: Final = 1e-6
#: Instant comparisons tolerate this much (seconds).
EPSILON_SECONDS: Final = 1e-3

Action = Literal["wait", "buy_now", "guarantee"]


@dataclass(frozen=True, slots=True)
class KnownInterval:
    """One interval whose price is published: start, end (aware) and any comparable price."""

    start: datetime
    end: datetime
    price: float


@dataclass(frozen=True, slots=True)
class PriceWaitDecision:
    """The decision, and every number behind it (for a caller that has to explain it)."""

    action: Action
    #: The expected publication instant (UTC, margin included); reported even when overdue.
    publication_at: datetime
    #: The energy that cannot wait (`buy_now`), or the whole need (`guarantee`); 0 for `wait`.
    must_buy_kwh: float
    #: `deadline - need / (max_charge_kw x margin)`: when waiting stops being safe.
    latest_safe_start: datetime
    #: `latest_safe_start` less one slot (`START_LAG`): when the guarantee is due and a caller should look again.
    act_by: datetime
    #: `buy_now` only: known intervals ending before the publication and the deadline; the planner picks the cheapest.
    eligible: tuple[KnownInterval, ...] = ()
    #: `buy_now` only: where the purchase window ends.
    window_end: datetime | None = None


def expected_publication_at(
    missing_day: date,
    *,
    margin: timedelta = PUBLICATION_MARGIN,
    zone: str = PUBLICATION_ZONE,
    local_time: time = PUBLICATION_LOCAL_TIME,
) -> datetime:
    """When `missing_day` (local date of the first instant with no price) is expected, as aware UTC.

    The previous day at the publication time in the market's zone, converted to UTC before the margin is added.
    """
    previous = missing_day - timedelta(days=1)
    local = datetime.combine(previous, local_time, tzinfo=ZoneInfo(zone))
    return local.astimezone(timezone.utc) + margin


def latest_safe_start(
    deadline: datetime, need_kwh: float, max_charge_kw: float, margin: float = FEASIBILITY_MARGIN
) -> datetime:
    """The last instant charging `need_kwh` at the derated rate still ends by `deadline`."""
    hours = need_kwh / (max_charge_kw * margin)
    return deadline.astimezone(timezone.utc) - timedelta(hours=hours)


def decide(
    *,
    now: datetime,
    deadline: datetime,
    need_kwh: float,
    max_charge_kw: float,
    known: Sequence[KnownInterval],
    publication_at: datetime,
    margin: float = FEASIBILITY_MARGIN,
    start_lag: timedelta = START_LAG,
) -> PriceWaitDecision:
    """Wait, buy what cannot wait, or charge the remainder now (see module docstring).

    `known` are the published intervals from now up to the gap, in any order. If `publication_at`
    has passed, the wait is measured from `now`, so `must_buy` grows until the known intervals
    can no longer hold it, which triggers the guarantee.
    """
    if need_kwh <= EPSILON_KWH:
        raise ValueError("need_kwh must be positive: there is nothing to wait for otherwise")
    if max_charge_kw <= 0 or not 0 < margin <= 1:
        raise ValueError("max_charge_kw and margin must be positive (margin at most 1)")
    now_utc = now.astimezone(timezone.utc)
    deadline_utc = deadline.astimezone(timezone.utc)
    publication_utc = publication_at.astimezone(timezone.utc)
    safe_start = latest_safe_start(deadline_utc, need_kwh, max_charge_kw, margin)
    act_by = safe_start - start_lag

    def guarantee() -> PriceWaitDecision:
        return PriceWaitDecision(
            action="guarantee",
            publication_at=publication_utc,
            must_buy_kwh=need_kwh,
            latest_safe_start=safe_start,
            act_by=act_by,
        )

    if (deadline_utc - now_utc).total_seconds() <= EPSILON_SECONDS:
        return guarantee()

    # Earliest the missing prices can be used: not before expected, not before the next slot.
    effective = max(publication_utc, now_utc + start_lag)
    after_hours = max(0.0, (deadline_utc - effective).total_seconds() / 3600)
    wait_capacity = after_hours * max_charge_kw * margin
    must_buy = need_kwh - wait_capacity
    if must_buy <= EPSILON_KWH:
        return PriceWaitDecision(
            action="wait",
            publication_at=publication_utc,
            must_buy_kwh=0.0,
            latest_safe_start=safe_start,
            act_by=act_by,
        )

    window_end = min(effective, deadline_utc)
    eligible = tuple(
        sorted(
            (
                interval
                for interval in known
                if interval.start.astimezone(timezone.utc) >= now_utc - timedelta(seconds=EPSILON_SECONDS)
                and interval.end.astimezone(timezone.utc) <= window_end
            ),
            key=lambda interval: interval.start,
        )
    )
    capacity = sum(
        (interval.end - interval.start).total_seconds() / 3600 for interval in eligible
    ) * max_charge_kw
    if capacity + EPSILON_KWH < must_buy:
        # Known prices cannot hold what must be bought; picking "cheapest" would miss the deadline.
        return guarantee()
    return PriceWaitDecision(
        action="buy_now",
        publication_at=publication_utc,
        must_buy_kwh=must_buy,
        latest_safe_start=safe_start,
        act_by=act_by,
        eligible=eligible,
        window_end=window_end,
    )
