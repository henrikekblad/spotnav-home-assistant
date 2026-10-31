"""The refresh manager's pure timing and state logic, driven directly.

No `hass`, no timers and no waiting: every case here is a function of an instant,
so the whole policy is testable in microseconds. The lifecycle tests that do need
`hass` are in `test_price_refresh.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.pricing.price_refresh import (
    BACKOFF_STEPS_SECONDS,
    HEALTHY_POLL_SECONDS,
    INVALID_POLL_SECONDS,
    JITTER_FRACTION,
    PUBLICATION_POLL_SECONDS,
    SETTLED_POLL_SECONDS,
    CadenceInput,
    backoff_delay,
    combined_state,
    in_publication_window,
    jittered,
    local_dates,
    next_local_midnight,
    refresh_interval,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
FIXTURES = Path(__file__).parent / "fixtures" / "relay"


def context(**overrides: object) -> CadenceInput:
    """A healthy, nothing-to-wait-for context, with the named fields replaced."""
    base = dict(
        now=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc),
        area_today=date(2026, 9, 22),
        area_tomorrow=date(2026, 9, 23),
        today_state="ready",
        tomorrow_state="ready",
        tomorrow_listed=True,
        tomorrow_ready=True,
        failing=False,
        backoff_step=0,
        invalid=False,
        in_publication_window=False,
    )
    base.update(overrides)
    return CadenceInput(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("zone", "instant", "today"),
    [
        # The area's own clock decides, not the HA host's and not UTC's.
        ("Europe/Stockholm", datetime(2026, 9, 22, 21, 59, tzinfo=timezone.utc), date(2026, 9, 22)),
        ("Europe/Stockholm", datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc), date(2026, 9, 23)),
        ("Europe/Berlin", datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc), date(2026, 9, 23)),
        ("Europe/Oslo", datetime(2026, 9, 22, 21, 59, tzinfo=timezone.utc), date(2026, 9, 22)),
        ("Europe/Helsinki", datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc), date(2026, 9, 23)),
    ],
)
def test_today_and_tomorrow_are_the_areas_local_dates(zone: str, instant: datetime, today: date) -> None:
    assert local_dates(instant, zone) == (today, today + timedelta(days=1))


def test_the_rollover_instant_is_each_kind_of_local_midnight():
    # An ordinary day: 22:00 UTC is midnight in Stockholm the next day.
    at = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert next_local_midnight(at, "Europe/Stockholm") == datetime(
        2026, 9, 23, 0, 0, tzinfo=STOCKHOLM
    )

    # Spring: the 23-hour day. Local midnight is 22:00 UTC the evening before.
    spring = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
    rollover = next_local_midnight(spring, "Europe/Stockholm")
    assert rollover.date() == date(2026, 3, 30)
    assert rollover.astimezone(timezone.utc) == datetime(2026, 3, 29, 22, 0, tzinfo=timezone.utc)
    # The day being rolled out of is 23 hours long, by instants.
    day_start = datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc)
    assert rollover.astimezone(timezone.utc) - day_start == timedelta(hours=23)

    # Autumn: the 25-hour day. Local midnight is 23:00 UTC.
    autumn = datetime(2025, 10, 26, 12, 0, tzinfo=timezone.utc)
    rollover = next_local_midnight(autumn, "Europe/Stockholm")
    assert rollover.date() == date(2025, 10, 27)
    assert rollover.astimezone(timezone.utc) == datetime(2025, 10, 26, 23, 0, tzinfo=timezone.utc)
    # The day being rolled out of is 25 hours long.
    assert rollover.astimezone(timezone.utc) - datetime(2025, 10, 25, 22, 0, tzinfo=timezone.utc) == timedelta(hours=25)


def test_the_publication_window_is_the_observed_midday_range_with_margin():
    """The default window (an area that states no time): 12:55-15:00 Brussels, half-open, in both offsets.

    The bounds are measured behaviour, not an ENTSO-E guarantee. Day-ahead publication is a
    **midday** process: every complete day document this repository holds was published between
    12:59:30 and 13:05:00 Brussels (2025-10-25, 2025-09-29, 2026-05-23, 2026-03-28 — CEST *and*
    CET), and one late delivery arrived around 13:30. Summer is CEST (+02:00), so 12:55 is 10:55 UTC and 15:00 is 13:00 UTC; winter is
    CET (+01:00), so the same wall-clock bounds are 11:55 UTC and 14:00 UTC.
    """
    day = date(2026, 9, 22)
    summer = lambda hour, minute, second=0: datetime(  # noqa: E731 - a readable table
        day.year, day.month, day.day, hour, minute, second, tzinfo=timezone.utc
    )

    # Summer: 12:55 CEST = 10:55 UTC, 15:00 CEST = 13:00 UTC. Impatient index polling must begin by 12:55 Brussels, so 12:54 is outside and 12:55 inside.
    assert in_publication_window(summer(10, 54, 59)) is False
    assert in_publication_window(summer(10, 55)) is True
    assert in_publication_window(summer(11, 0)) is True
    assert in_publication_window(summer(11, 30)) is True
    assert in_publication_window(summer(12, 59, 59)) is True
    assert in_publication_window(summer(13, 0)) is False

    # Winter: the same *wall clock* on either side of the DST boundary, one hour later in UTC.
    winter_day = date(2026, 1, 15)
    winter = lambda hour, minute, second=0: datetime(  # noqa: E731
        winter_day.year, winter_day.month, winter_day.day, hour, minute, second, tzinfo=timezone.utc
    )
    assert in_publication_window(winter(11, 54, 59)) is False
    assert in_publication_window(winter(11, 55)) is True
    assert in_publication_window(winter(12, 5)) is True
    assert in_publication_window(winter(13, 59, 59)) is True
    assert in_publication_window(winter(14, 0)) is False
    # The CET/CEST requirement, stated as itself: 12:55 and 15:00 Brussels decide on a summer day
    # and on a winter day alike, whatever the host's own zone believes.
    assert in_publication_window(summer(10, 55)) == in_publication_window(winter(11, 55)) is True
    assert in_publication_window(summer(13, 0)) == in_publication_window(winter(14, 0)) is False

    # The measurements the bounds were derived from are inside it: every complete document the
    # fixtures hold, in both offsets, plus a delayed midday delivery.
    for published in (
        "2025-10-25T12:59:30+02:00",
        "2025-09-29T13:00:00+02:00",
        "2026-05-23T12:59:59+02:00",
        "2026-03-28T13:05:00+01:00",
        "2026-09-26T13:30:00+02:00",
    ):
        assert in_publication_window(datetime.fromisoformat(published)) is True, published
    # A late-evening document is deliberately *outside* it, and so is an early **incomplete**
    # document at 11:59:30, which is why the window starts at 12:55 rather than at dawn.
    assert in_publication_window(datetime.fromisoformat("2026-09-21T20:15:50+02:00")) is False
    assert in_publication_window(datetime.fromisoformat("2026-09-21T11:59:30+02:00")) is False

    # The same instant seen from four host zones answers the same thing.
    instant = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    for zone in ("UTC", "Europe/Stockholm", "Europe/Helsinki", "Pacific/Auckland"):
        assert in_publication_window(instant.astimezone(ZoneInfo(zone))) is True


def test_every_cadence_and_backoff_step_is_the_documented_number():
    # Healthy, with tomorrow listed and in hand: the slow check.
    assert refresh_interval(context()) == timedelta(seconds=SETTLED_POLL_SECONDS)
    # Today ready, tomorrow not listed yet, outside the window: the ordinary one.
    assert refresh_interval(context(tomorrow_listed=False, tomorrow_ready=False)) == timedelta(
        seconds=HEALTHY_POLL_SECONDS
    )
    # Inside the window, waiting for tomorrow: impatient, but only for the index.
    assert refresh_interval(
        context(tomorrow_listed=False, tomorrow_ready=False, in_publication_window=True)
    ) == timedelta(seconds=PUBLICATION_POLL_SECONDS)
    # And the window does not make a *settled* area impatient.
    assert refresh_interval(context(in_publication_window=True)) == timedelta(seconds=SETTLED_POLL_SECONDS)

    # Backoff steps, in order, and then capped.
    for step, seconds in enumerate(BACKOFF_STEPS_SECONDS, start=1):
        assert backoff_delay(step) == timedelta(seconds=seconds)
        assert refresh_interval(context(failing=True, backoff_step=step)) == timedelta(seconds=seconds)
    assert backoff_delay(0) == timedelta(seconds=BACKOFF_STEPS_SECONDS[0])
    assert backoff_delay(len(BACKOFF_STEPS_SECONDS) + 5) == timedelta(seconds=BACKOFF_STEPS_SECONDS[-1])

    # Invalid data never hot-loops, at any step or under any state.
    for kwargs in (
        dict(invalid=True),
        dict(invalid=True, today_state="invalid"),
        dict(invalid=True, failing=True, backoff_step=1),
        dict(invalid=True, tomorrow_listed=False, in_publication_window=True),
    ):
        assert refresh_interval(context(**kwargs)) >= timedelta(seconds=INVALID_POLL_SECONDS)


def test_jitter_is_bounded_and_deterministic_from_an_injected_number():
    delay = timedelta(seconds=HEALTHY_POLL_SECONDS)

    assert jittered(delay, rand=0.5) == delay
    assert jittered(delay, rand=0.0) == timedelta(seconds=HEALTHY_POLL_SECONDS * (1 - JITTER_FRACTION))
    assert jittered(delay, rand=1.0) == timedelta(seconds=HEALTHY_POLL_SECONDS * (1 + JITTER_FRACTION))
    assert jittered(delay, rand=0.25) == jittered(delay, rand=0.25)

    # Inside the bounds for a thousand draws, and never zero or negative.
    for index in range(1000):
        spread = jittered(delay, rand=index / 1000)
        assert timedelta(seconds=HEALTHY_POLL_SECONDS * 0.9) <= spread <= timedelta(seconds=HEALTHY_POLL_SECONDS * 1.1)
        assert spread > timedelta(0)
    # Out-of-range inputs are clamped rather than trusted.
    assert jittered(delay, rand=-5.0) == jittered(delay, rand=0.0)
    assert jittered(delay, rand=+5.0) == jittered(delay, rand=1.0)


def test_the_combined_state_table_is_the_documented_one():
    """Every row of the precedence, including the `degraded` cases.

    `unavailable` is reserved for no usable *today* document; a usable today with an
    impaired horizon is `degraded`, whatever impaired it; today's own
    invalid/stale/incomplete keep their precedence above all of it.
    """
    usable = "ready"

    # Today unusable: three different facts, three different reasons.
    assert combined_state(
        today_state="unavailable", tomorrow_state="unavailable",
        today_authority="not_listed", tomorrow_authority="not_listed",
    ) == ("unavailable", "not_listed")
    assert combined_state(
        today_state="unavailable", tomorrow_state="unavailable",
        today_authority="unknown", tomorrow_authority="unknown",
    ) == ("unavailable", "index_unavailable")
    assert combined_state(
        today_state="unavailable", tomorrow_state="unavailable",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("unavailable", "source_unreachable")

    # Today's own states, in precedence order.
    assert combined_state(
        today_state="invalid", tomorrow_state="unavailable",
        today_authority="unknown", tomorrow_authority="unknown",
    ) == ("invalid", "invalid_contract")
    assert combined_state(
        today_state="loading", tomorrow_state="unavailable",
        today_authority="unknown", tomorrow_authority="unknown",
    ) == ("loading", "loading")
    assert combined_state(
        today_state="stale", tomorrow_state="ready",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("stale", "last_good_retained")
    assert combined_state(
        today_state="incomplete", tomorrow_state="ready",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("incomplete", "incomplete_day")

    # Today usable from here.
    assert combined_state(
        today_state=usable, tomorrow_state="ready",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("ready", "ready")
    assert combined_state(
        today_state=usable, tomorrow_state="unavailable",
        today_authority="listed", tomorrow_authority="not_listed",
    ) == ("waiting_for_tomorrow", "waiting_for_tomorrow")

    # A usable today with an impaired horizon: degraded, and never `unavailable`.
    assert combined_state(
        today_state=usable, tomorrow_state="unavailable",
        today_authority="listed", tomorrow_authority="unknown",
    ) == ("degraded", "index_unavailable")
    assert combined_state(
        today_state=usable, tomorrow_state="unavailable",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("degraded", "tomorrow_unavailable")
    assert combined_state(
        today_state=usable, tomorrow_state="stale",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("degraded", "tomorrow_stale")
    assert combined_state(
        today_state=usable, tomorrow_state="invalid",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("degraded", "tomorrow_invalid")
    assert combined_state(
        today_state=usable, tomorrow_state="incomplete",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("degraded", "tomorrow_incomplete")
    assert combined_state(
        today_state=usable, tomorrow_state="loading",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("degraded", "tomorrow_unavailable")

    # The spec's own case: today ready, a fresh index *lists* tomorrow, and
    # tomorrow's document never arrived. That is not "today is unavailable".
    state, reason = combined_state(
        today_state="ready", tomorrow_state="unavailable",
        today_authority="listed", tomorrow_authority="listed",
    )
    assert (state, reason) != ("unavailable", "source_unreachable")
    assert state == "degraded"

    # Today's own state still wins over an impaired horizon.
    assert combined_state(
        today_state="incomplete", tomorrow_state="invalid",
        today_authority="listed", tomorrow_authority="listed",
    ) == ("incomplete", "incomplete_day")


