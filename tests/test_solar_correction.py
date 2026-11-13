"""The solar forecast correction: the factor rules and each guard rail."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.spotnav.site import solar_correction as sc
from custom_components.spotnav.site.solar_correction import (
    CURTAILED_EXPORT_LIMIT,
    CURTAILED_NEGATIVE_PRICE,
    CURTAILED_ZERO_EXPORT,
    FACTOR_MAX,
    FACTOR_MIN,
    HourMeta,
    HourSample,
    KIND_CLEAR,
    KIND_CLOUDY,
    SolarCorrection,
    cap_kwh,
    clipped_hours,
    curtailment,
    day_evidence,
    plateau_ceiling,
    samples_from_log,
    window_targets,
)
from custom_components.spotnav.site.sun_forecast_log import KIND_SUN, KIND_SUN_CORRECTED, SunForecastLog

TZ = "UTC"
TODAY = date(2026, 6, 20)


def _day(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


def _hours(day: date, forecast: float, actual: float, *, hours=range(9, 16), **flags) -> list[HourSample]:
    return [
        HourSample(datetime(day.year, day.month, day.day, hour, tzinfo=UTC), forecast, actual, **flags)
        for hour in hours
    ]


def _days(count: int, forecast: float, actual: float, **flags) -> list[HourSample]:
    return [sample for age in range(1, count + 1) for sample in _hours(_day(age), forecast, actual, **flags)]


# ---- the factor rules


def test_kind_of_hour_is_cloudy_from_sixty_percent() -> None:
    assert sc.kind_of(60) == KIND_CLOUDY
    assert sc.kind_of(59.9) == KIND_CLEAR
    assert sc.kind_of(None) == KIND_CLEAR


def test_recency_weights_fall_a_tenth_a_day_to_a_floor() -> None:
    assert sc.day_weight(1) == 1.0
    assert sc.day_weight(2) == pytest.approx(0.9)
    assert sc.day_weight(7) == pytest.approx(0.4)
    assert sc.day_weight(30) == pytest.approx(0.3)


def test_factor_moves_a_fifth_of_the_way_per_day() -> None:
    evidence = day_evidence(_days(7, 2.0, 1.5), TZ)
    state = SolarCorrection()

    assert state.nightly(evidence, TODAY, curtailed=None)

    assert state.factors[KIND_CLEAR] == pytest.approx(1.0 + 0.2 * (0.75 - 1.0))
    assert state.factors[KIND_CLOUDY] == 1.0  # no cloudy hours at all
    assert state.days == {KIND_CLEAR: 7, KIND_CLOUDY: 0}


def test_the_nightly_update_runs_once_a_day() -> None:
    evidence = day_evidence(_days(7, 2.0, 1.5), TZ)
    state = SolarCorrection()
    state.nightly(evidence, TODAY, curtailed=None)
    once = state.factors[KIND_CLEAR]

    assert not state.nightly(evidence, TODAY, curtailed=None)
    assert state.factors[KIND_CLEAR] == once
    assert state.nightly(evidence, TODAY + timedelta(days=1), curtailed=None)
    assert state.factors[KIND_CLEAR] < once


def test_the_factor_is_clamped() -> None:
    low = SolarCorrection()
    high = SolarCorrection()
    for offset in range(60):
        today = TODAY + timedelta(days=offset)
        low_samples = [s for age in range(1, 8) for s in _hours(today - timedelta(days=age), 2.0, 0.3)]
        high_samples = [s for age in range(1, 8) for s in _hours(today - timedelta(days=age), 0.5, 3.0)]
        low.nightly(day_evidence(low_samples, TZ), today, curtailed=None)
        high.nightly(day_evidence(high_samples, TZ), today, curtailed=None)

    assert low.factors[KIND_CLEAR] == pytest.approx(FACTOR_MIN, abs=1e-3)
    assert low.factors[KIND_CLEAR] >= FACTOR_MIN
    assert high.factors[KIND_CLEAR] == pytest.approx(FACTOR_MAX, abs=1e-3)
    assert high.factors[KIND_CLEAR] <= FACTOR_MAX


def test_only_hours_with_a_meaningful_forecast_count() -> None:
    samples = _days(7, 2.0, 2.0) + _days(7, 0.2, 5.0, hours=range(6, 8))
    evidence = day_evidence(samples, TZ)

    assert evidence[_day(1)].hours[KIND_CLEAR] == 7
    assert window_targets(evidence, TODAY)[KIND_CLEAR][0] == pytest.approx(1.0)


def test_a_kind_needs_three_days_before_it_moves() -> None:
    state = SolarCorrection()
    state.nightly(day_evidence(_days(2, 2.0, 1.0), TZ), TODAY, curtailed=None)

    assert state.factors[KIND_CLEAR] == 1.0
    assert state.status == sc.STATUS_LEARNING


def test_cloudy_and_clear_hours_learn_apart() -> None:
    samples = _days(7, 2.0, 2.0) + _days(7, 1.0, 0.5, hours=range(16, 18), cloudy=True)
    state = SolarCorrection()
    state.nightly(day_evidence(samples, TZ), TODAY, curtailed=None)

    assert state.factors[KIND_CLEAR] == pytest.approx(1.0)
    assert state.factors[KIND_CLOUDY] == pytest.approx(0.9)
    assert state.status == sc.STATUS_SHADOW


def test_recent_days_weigh_more() -> None:
    samples = _hours(_day(1), 2.0, 2.0) + _hours(_day(2), 2.0, 2.0) + _hours(_day(7), 2.0, 0.5)
    ratio, days = window_targets(day_evidence(samples, TZ), TODAY)[KIND_CLEAR]

    assert days == 3
    # Weights 1.0, 0.9 and 0.4: (2 + 1.8 + 0.2) / (2 + 1.8 + 0.8).
    assert ratio == pytest.approx(4.0 / 4.6)


def test_days_outside_the_window_are_left_out() -> None:
    samples = _days(7, 2.0, 2.0) + _hours(_day(8), 2.0, 0.0) + _hours(TODAY, 2.0, 0.0)
    ratio, days = window_targets(day_evidence(samples, TZ), TODAY)[KIND_CLEAR]

    assert days == 7
    assert ratio == pytest.approx(1.0)


# ---- guard rails


def test_a_down_day_is_skipped() -> None:
    samples = _days(7, 2.0, 2.0)
    samples = [s for s in samples if s.start.date() != _day(1)] + _hours(_day(1), 2.0, 0.1)
    evidence = day_evidence(samples, TZ)

    assert evidence[_day(1)].down
    ratio, days = window_targets(evidence, TODAY)[KIND_CLEAR]
    assert days == 6
    assert ratio == pytest.approx(1.0)


def test_a_clipped_hour_is_imputed_with_the_forecast() -> None:
    clipped = _days(7, 5.0, 4.0, clipped=True)
    evidence = day_evidence(clipped, TZ)

    assert evidence[_day(1)].imputed == 7
    assert window_targets(evidence, TODAY)[KIND_CLEAR][0] == pytest.approx(1.0)


def test_a_clipped_hour_above_its_forecast_is_kept() -> None:
    evidence = day_evidence(_days(7, 3.0, 4.0, clipped=True), TZ)

    assert evidence[_day(1)].imputed == 0
    assert window_targets(evidence, TODAY)[KIND_CLEAR][0] == pytest.approx(4.0 / 3.0)


def _flat_day(day: date, level: float, *, slots: int, start_hour: int = 11) -> dict[datetime, float]:
    first = datetime(day.year, day.month, day.day, start_hour, tzinfo=UTC)
    series = {first - timedelta(minutes=5 * (i + 1)): level * (0.9 - 0.05 * i) for i in range(6)}
    series.update({first + timedelta(minutes=5 * i): level for i in range(slots)})
    series.update({first + timedelta(minutes=5 * (slots + i)): level * (0.9 - 0.05 * i) for i in range(6)})
    return series


def test_a_ceiling_needs_a_long_flat_run_on_two_days() -> None:
    two_days = {**_flat_day(_day(1), 4.6, slots=12), **_flat_day(_day(2), 4.6, slots=12)}
    one_day = _flat_day(_day(1), 4.6, slots=12)
    short_runs = {**_flat_day(_day(1), 4.6, slots=4), **_flat_day(_day(2), 4.6, slots=4)}

    assert plateau_ceiling(two_days, min_kw=0.5) == pytest.approx(4.6)
    assert plateau_ceiling(one_day, min_kw=0.5) is None
    assert plateau_ceiling(short_runs, min_kw=0.5) is None
    assert plateau_ceiling({}, min_kw=0.5) is None


def test_a_gap_breaks_a_plateau() -> None:
    series = {**_flat_day(_day(1), 4.6, slots=12), **_flat_day(_day(2), 4.6, slots=12)}
    for day in (_day(1), _day(2)):
        del series[datetime(day.year, day.month, day.day, 11, 30, tzinfo=UTC)]

    assert plateau_ceiling(series, min_kw=0.5) is None


def test_clipped_hours_are_those_at_the_ceiling() -> None:
    series = _flat_day(_day(1), 4.6, slots=12)

    assert clipped_hours(series, 4.6) == {datetime(_day(1).year, _day(1).month, _day(1).day, 11, tzinfo=UTC)}
    assert clipped_hours(series, None) == set()


def test_the_corrected_forecast_is_capped_by_what_was_observed() -> None:
    state = SolarCorrection(factors={KIND_CLEAR: 2.0, KIND_CLOUDY: 2.0})
    hour = datetime(2026, 6, 21, 12, tzinfo=UTC)

    assert state.correct({hour: 3.0}, observed_max_kwh=4.0, array_kwp=None)[hour] == pytest.approx(4.0)
    # The raw forecast is never cut by the observed maximum (a brighter season), only not raised beyond it.
    assert state.correct({hour: 5.0}, observed_max_kwh=4.0, array_kwp=None)[hour] == pytest.approx(5.0)
    # Nothing observed yet: no raise at all.
    assert state.correct({hour: 3.0}, observed_max_kwh=None, array_kwp=None)[hour] == pytest.approx(3.0)


def test_a_lowering_factor_is_not_capped() -> None:
    state = SolarCorrection(factors={KIND_CLEAR: 0.5, KIND_CLOUDY: 1.0})
    hour = datetime(2026, 6, 21, 12, tzinfo=UTC)

    assert state.correct({hour: 3.0}, observed_max_kwh=1.0, array_kwp=6.0)[hour] == pytest.approx(1.5)


def test_sixteen_kilowatts_on_a_six_kilowatt_array_cannot_happen() -> None:
    """Predbat #6049: a calibrated line reached 16.2 kW on a 6.02 kWp array."""
    state = SolarCorrection(factors={KIND_CLEAR: FACTOR_MAX, KIND_CLOUDY: FACTOR_MAX})
    hours = {datetime(2026, 6, 21, hour, tzinfo=UTC): kwh for hour, kwh in zip(range(8, 17), (1, 3, 5, 7, 8.1, 7, 5, 3, 1))}

    with_array = state.correct(hours, observed_max_kwh=5.9, array_kwp=6.02)
    without_array = state.correct(hours, observed_max_kwh=5.9, array_kwp=None)

    assert max(with_array.values()) <= 6.02
    # Without the array's size, never above the larger of the raw forecast and the best hour seen.
    assert all(without_array[h] <= max(raw, 5.9) for h, raw in hours.items())
    assert max(without_array.values()) == pytest.approx(8.1)


def test_cap_is_never_below_zero() -> None:
    assert cap_kwh(-1.0, observed_max_kwh=None, array_kwp=None) == 0.0


def test_negative_price_hours_are_never_learned_from() -> None:
    samples = _days(7, 2.0, 2.0) + _days(7, 2.0, 0.0, hours=range(16, 18), negative_price=True)
    evidence = day_evidence(samples, TZ)

    assert evidence[_day(1)].negative_hours == 2
    assert window_targets(evidence, TODAY)[KIND_CLEAR][0] == pytest.approx(1.0)


def test_visible_negative_price_curtailment_turns_it_off() -> None:
    samples = _days(7, 2.0, 2.0) + _hours(_day(2), 2.0, 0.2, hours=range(16, 18), negative_price=True)
    evidence = day_evidence(samples, TZ)

    assert evidence[_day(2)].negative_curtailed
    assert curtailment(evidence, TODAY, export_slots=None, solar_kwh=None, export_kwh=None) == CURTAILED_NEGATIVE_PRICE


def test_a_negative_price_hour_that_produced_as_usual_is_no_curtailment() -> None:
    samples = _days(7, 2.0, 1.8) + _hours(_day(2), 2.0, 1.7, hours=range(16, 18), negative_price=True)

    assert curtailment(day_evidence(samples, TZ), TODAY, export_slots=None, solar_kwh=None, export_kwh=None) is None


def test_an_export_limit_turns_it_off() -> None:
    export = {**_flat_day(_day(1), 3.0, slots=12), **_flat_day(_day(3), 3.0, slots=12)}

    assert curtailment({}, TODAY, export_slots=export, solar_kwh=None, export_kwh=None) == CURTAILED_EXPORT_LIMIT


def test_a_zero_export_site_is_off() -> None:
    assert curtailment({}, TODAY, export_slots={}, solar_kwh=80.0, export_kwh=0.2) == CURTAILED_ZERO_EXPORT
    assert curtailment({}, TODAY, export_slots={}, solar_kwh=80.0, export_kwh=20.0) is None
    # Too little sun to tell, or no export measured: not judged.
    assert curtailment({}, TODAY, export_slots={}, solar_kwh=5.0, export_kwh=0.0) is None
    assert curtailment({}, TODAY, export_slots=None, solar_kwh=80.0, export_kwh=None) is None


def test_a_curtailed_site_keeps_its_factors_and_passes_the_raw_forecast() -> None:
    state = SolarCorrection(factors={KIND_CLEAR: 0.8, KIND_CLOUDY: 1.0})
    state.nightly(day_evidence(_days(7, 2.0, 1.0), TZ), TODAY, curtailed=CURTAILED_ZERO_EXPORT)
    hour = datetime(2026, 6, 21, 12, tzinfo=UTC)

    assert state.factors[KIND_CLEAR] == 0.8
    assert state.status == sc.STATUS_OFF_CURTAILED
    assert state.correct({hour: 3.0}, observed_max_kwh=5.0, array_kwp=None)[hour] == 3.0


def test_it_never_learns_from_its_own_corrected_series() -> None:
    log = SunForecastLog(tz=TZ)
    made = datetime(2026, 6, 12, 0, tzinfo=UTC)
    hours = {datetime(2026, 6, d, h, tzinfo=UTC): 2.0 for d in range(13, 20) for h in range(9, 16)}
    log.record_forecast(KIND_SUN, hours, made)
    log.record_forecast(KIND_SUN_CORRECTED, dict.fromkeys(hours, 9.0), made)
    for start in hours:
        log.record_actual(KIND_SUN, start, 2.0)
        log.record_actual(KIND_SUN_CORRECTED, start, 2.0)

    samples = samples_from_log(log.hours(KIND_SUN), {}, set())
    state = SolarCorrection()
    state.nightly(day_evidence(samples, TZ), TODAY, curtailed=None)

    assert {s.forecast_kwh for s in samples} == {2.0}
    assert state.factors[KIND_CLEAR] == pytest.approx(1.0)


def test_samples_carry_the_kept_facts() -> None:
    log = SunForecastLog(tz=TZ)
    start = datetime(2026, 6, 19, 12, tzinfo=UTC)
    log.record_forecast(KIND_SUN, {start: 2.0}, start - timedelta(hours=10))
    log.record_actual(KIND_SUN, start, 1.0)
    meta = {start: HourMeta(cloud_pct=80, negative_price=True)}

    (sample,) = samples_from_log(log.hours(KIND_SUN), meta, {start})

    assert sample.cloudy and sample.clipped and sample.negative_price


def test_an_hour_keeps_its_first_cloud_cover_and_takes_its_price_later() -> None:
    state = SolarCorrection()
    start = datetime(2026, 6, 21, 12, tzinfo=UTC)
    state.note_hour(start, cloud_pct=90, negative_price=None)
    state.note_hour(start, cloud_pct=10, negative_price=True)

    assert state.meta[start] == HourMeta(90, True)
    corrected = SolarCorrection(factors={KIND_CLEAR: 1.0, KIND_CLOUDY: 0.5}, meta=state.meta)
    assert corrected.correct({start: 2.0}, observed_max_kwh=None, array_kwp=None)[start] == 1.0


def test_the_state_survives_a_round_trip_and_bad_storage() -> None:
    state = SolarCorrection(factors={KIND_CLEAR: 0.8, KIND_CLOUDY: 1.3})
    state.note_hour(datetime(2026, 6, 21, 12, tzinfo=UTC), cloud_pct=70.0, negative_price=False)
    state.nightly(day_evidence(_days(7, 2.0, 1.0), TZ), TODAY, curtailed=None)

    again = SolarCorrection.from_dict(json.loads(json.dumps(state.as_dict())))

    assert again.as_dict() == state.as_dict()
    broken = SolarCorrection.from_dict(
        {"factors": {KIND_CLEAR: 99, KIND_CLOUDY: "x"}, "updated_on": "nope", "curtailed": "bogus", "meta": [[1, 2]]}
    )
    assert broken.factors == {KIND_CLEAR: FACTOR_MAX, KIND_CLOUDY: 1.0}
    assert broken.updated_on is None and broken.curtailed is None and broken.meta == {}
    assert SolarCorrection.from_dict(None).factors == {KIND_CLEAR: 1.0, KIND_CLOUDY: 1.0}


def test_meta_is_pruned_with_the_log() -> None:
    state = SolarCorrection()
    old = datetime(2026, 6, 1, 12, tzinfo=UTC)
    state.note_hour(old, cloud_pct=None, negative_price=None)
    state.prune(datetime(2026, 6, 20, tzinfo=UTC))

    assert state.meta == {}
