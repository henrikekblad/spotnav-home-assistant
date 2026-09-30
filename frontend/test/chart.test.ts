// The chart's time model, geometry and series, including both DST transitions.

import { describe, expect, it } from "vitest";

import {
  axisTicks,
  chartNowAt,
  chartScale,
  chartSeries,
  currentMarkFor,
  gutterLabels,
  hitTargets,
  hitTestMarks,
  intervalIdentity,
  localMidnightAt,
  localDayLengthMs,
  markGeometry,
  nearestMark,
  nextIntervalBoundary,
  plannedBands,
  walkMarks,
  xForWallClock,
  yAt,
  zoneClock,
  type ChartHitTarget,
  type ChartMark,
} from "../src/chart";
import { decodeDashboard, type PriceInterval } from "../src/validate";
import { decoded, quarterHourRows, rawDashboard } from "./dashboard-fixtures";

const TZ = "Europe/Stockholm";
const SEPT = "2026-09-22";

function rowsFor(startIso: string, prices: readonly (number | null)[], day = SEPT, duration = 15): PriceInterval[] {
  const payload = rawDashboard();
  (payload.prices as Record<string, unknown>).intervals = quarterHourRows(startIso, prices, { day, duration });
  const result = decodeDashboard(payload);
  if (!result.ok || result.value.prices === null) {
    throw new Error("fixture did not decode");
  }
  return result.value.prices.intervals;
}

describe("the local-day model", () => {
  it("measures a normal day as 24 hours", () => {
    const midnight = localMidnightAt(Date.parse(`${SEPT}T12:00:00Z`), TZ);
    expect(localDayLengthMs(midnight, TZ)).toBe(24 * 3_600_000);
  });

  it("measures the spring-forward day as 23 hours and the autumn day as 25", () => {
    const spring = localMidnightAt(Date.parse("2026-03-29T12:00:00Z"), TZ);
    const autumn = localMidnightAt(Date.parse("2026-10-25T12:00:00Z"), TZ);
    expect(localDayLengthMs(spring, TZ)).toBe(23 * 3_600_000);
    expect(localDayLengthMs(autumn, TZ)).toBe(25 * 3_600_000);
    expect(zoneClock(TZ).dayKey(spring)).toBe("2026-03-29");
  });

  it("keeps both passes of a repeated autumn hour as records, at one visible x", () => {
    // 00:00Z is 02:00 local on the first pass; 01:00Z is 02:00 local on the second.
    const rows = [
      ...rowsFor("2026-10-25T00:00:00Z", [10, 11], "2026-10-25"),
      ...rowsFor("2026-10-25T01:00:00Z", [20, 21], "2026-10-25"),
    ];
    const series = chartSeries(rows, TZ, Date.parse("2026-10-25T00:00:00Z"));
    const repeated = series.marks.filter((mark) => mark.wallClock === "02:00");
    expect(repeated).toHaveLength(2);
    const [first, second] = repeated as [typeof repeated[0], typeof repeated[0]];
    // One wall clock, one shared axis: the second pass draws on top of the first pass's coordinate...
    expect(first.position).toBeCloseTo(second.position, 12);
    const scale = chartScale(320, 160, series.marks.map((mark) => mark.price));
    expect(xForWallClock(scale, first.position)).toBeCloseTo(xForWallClock(scale, second.position), 9);
    // ...and they are still two records: two instants, two offsets, chronological order.
    expect(first.startMs).toBeLessThan(second.startMs);
    expect(first.offset).not.toBe(second.offset);
    expect(intervalIdentity(first)).not.toBe(intervalIdentity(second));
    // The walk passes every record of the repeated hour, in instant order, even at one x.
    const inBetween = series.marks[1] as (typeof series.marks)[number];
    expect(walkMarks(series.marks, first, 1)).toBe(inBetween);
    expect(walkMarks(series.marks, inBetween, 1)).toBe(second);
    expect(walkMarks(series.marks, second, -1)).toBe(inBetween);
    // Each occurrence is current during its own elapsed hour, even though the two share one x: the
    // instant range decides, and the wall clock is never what is searched.
    expect(series.days[0]?.lengthMs).toBe(25 * 3_600_000);
    expect(currentMarkFor(series.marks, Date.parse("2026-10-25T00:05:00Z"))).toBe(first);
    expect(currentMarkFor(series.marks, Date.parse("2026-10-25T01:05:00Z"))).toBe(second);
    expect(intervalIdentity(currentMarkFor(series.marks, Date.parse("2026-10-25T00:05:00Z")))).not.toBe(
      intervalIdentity(currentMarkFor(series.marks, Date.parse("2026-10-25T01:05:00Z"))),
    );
  });

  it("invents nothing across the spring gap, and labels the axis at real wall clocks", () => {
    const rows = rowsFor("2026-03-28T23:00:00Z", [1, 2, 3, 4], "2026-03-29");
    const series = chartSeries(rows, TZ, Date.parse("2026-03-28T23:00:00Z"));
    expect(series.marks.map((mark) => mark.wallClock)).toEqual(["00:00", "00:15", "00:30", "00:45"]);
    expect(series.days[0]?.lengthMs).toBe(23 * 3_600_000);
    // The 23-hour day still draws the one five-tick sequence, and the tick instants really read the
    // wall clock they are labelled with -- not the elapsed-hour estimate.
    const clock = zoneClock(TZ);
    const ticks = axisTicks(series.days, clock);
    expect(ticks.map((tick) => tick.position)).toEqual([0, 0.25, 0.5, 0.75, 1]);
    const readings = ticks.map((tick) => clock.msOfDay(tick.instantMs) / 60_000);
    expect(readings).toEqual([0, 360, 720, 1080, 0]);
    // The hour the zone skipped is absent, and the interval that really covers an instant inside the
    // 23-hour day is still selected by its own range -- never by a wall-clock search.
    expect(chartNowAt(series.marks, Date.parse("2026-03-28T23:16:00Z")).mark).toBe(series.marks[1]);
    expect(chartNowAt(series.marks, Date.parse("2026-03-28T23:16:00Z")).mark?.wallClock).toBe("00:15");
    expect(series.days[0]?.role).toBe("today");
  });

  it("distinguishes today from tomorrow at one x, for a whole number of days", () => {
    const today = rowsFor("2026-09-22T04:00:00Z", [10, 30], SEPT);
    const tomorrow = rowsFor("2026-09-23T04:00:00Z", [1000, 3000], "2026-09-23");
    // The clock is inside the first date, so that date is today's series and the second is the coming
    // one -- the roles are the market clock's answer, and this is the ordinary case of them.
    const series = chartSeries([...today, ...tomorrow], TZ, Date.parse("2026-09-22T04:30:00Z"));
    expect(series.days).toHaveLength(2);
    expect(series.days.map((day) => day.role)).toEqual(["today", "future"]);
    const scale = chartScale(320, 160, series.marks.map((mark) => mark.price));
    // Four marks, two days, two wall clocks: each clock time is one x for both days.
    expect(series.marks).toHaveLength(4);
    const xOf = (mark: (typeof series.marks)[number]): number => xForWallClock(scale, mark.position);
    const todayFirst = series.marks[0] as (typeof series.marks)[number];
    const tomorrowFirst = series.marks[2] as (typeof series.marks)[number];
    expect(todayFirst.tomorrow).toBe(false);
    expect(tomorrowFirst.tomorrow).toBe(true);
    expect(todayFirst.dayKey).toBe(SEPT);
    expect(tomorrowFirst.dayKey).toBe("2026-09-23");
    expect(xOf(todayFirst)).toBeCloseTo(xOf(tomorrowFirst), 12);
    expect(intervalIdentity(todayFirst)).not.toBe(intervalIdentity(tomorrowFirst));
    // And the mapping itself is the accepted `minuteOfDay / 1440` over the full plot width.
    expect(xOf(todayFirst)).toBeCloseTo(
      scale.left + (6 * 60 / 1440) * (scale.right - scale.left),
      9,
    );
  });

  it("maps a wall-clock fraction onto the whole width, at any position", () => {
    const scale = chartScale(320, 160, [1]);
    expect(xForWallClock(scale, 0)).toBeCloseTo(scale.left, 9);
    expect(xForWallClock(scale, 1)).toBeCloseTo(scale.right, 9);
    expect(xForWallClock(scale, 0.25)).toBeCloseTo(scale.left + 0.25 * (scale.right - scale.left), 9);
    // Clamped, so a mark can never be drawn outside the plot box.
    expect(xForWallClock(scale, -1)).toBeCloseTo(scale.left, 9);
    expect(xForWallClock(scale, 2)).toBeCloseTo(scale.right, 9);
    // Monotone: a later wall clock is never drawn to the left of an earlier one.
    const xs = [0, 0.1, 0.5, 0.75, 1].map((position) => xForWallClock(scale, position));
    expect(xs).toEqual([...xs].sort((left, right) => left - right));
  });
});

describe("the drawn series", () => {
  it("draws quarter-hour prices as points at their own start", () => {
    const series = chartSeries(rowsFor("2026-09-22T04:00:00Z", [1, 2, 3, 4]), TZ, 0);
    expect(series.hourly).toBe(false);
    expect(series.marks.map((mark) => mark.wallClock)).toEqual(["06:00", "06:15", "06:30", "06:45"]);
    expect(series.marks[1]?.position).toBeGreaterThan(series.marks[0]?.position as number);
  });

  it("centres an hourly segment on its interval", () => {
    const rows = rowsFor("2026-09-22T04:00:00Z", [5, 6], SEPT, 60);
    const series = chartSeries(rows, TZ, 0);
    expect(series.hourly).toBe(true);
    expect(series.marks[0]?.wallClock).toBe("06:30");
    expect(series.marks[1]?.wallClock).toBe("07:30");
  });

  it("counts a missing price as missing and creates no zero mark", () => {
    const series = chartSeries(rowsFor("2026-09-22T04:00:00Z", [1, null, 3]), TZ, 0);
    expect(series.marks).toHaveLength(2);
    expect(series.missing).toBe(1);
    expect(series.marks.map((mark) => mark.price)).toEqual([1, 3]);
  });

  it("finds the current interval by half-open containment, across every accepted day", () => {
    const rows = rowsFor("2026-09-22T04:00:00Z", [1, 2, 3, 4]);
    const series = chartSeries(rows, TZ, Date.parse("2026-09-22T04:30:00Z"));
    expect(series.current?.startMs).toBe(Date.parse("2026-09-22T04:30:00Z"));
    const gap = chartSeries(rows, TZ, Date.parse("2026-09-22T05:30:00Z"));
    expect(gap.current).toBeNull();
    // A capture that still holds the local day the clock has left behind -- a plan horizon calculated
    // before local midnight reads exactly like this. The second date's own row contains `now`, so it is
    // current: containment decides, never the index of the date it is labelled with.
    const next = rowsFor("2026-09-23T04:00:00Z", [5, 6], "2026-09-23");
    const atNext = Date.parse("2026-09-23T04:00:00Z");
    const both = chartSeries([...rows, ...next], TZ, atNext);
    expect(both.current?.startMs).toBe(atNext);
    expect(both.current?.price).toBe(5);
    expect(currentMarkFor(both.marks, atNext)).toBe(both.current);
    // And the roles are the market clock's answer, not the rows' order: the date that contains the
    // instant is today's series, the date the clock has left is the subdued one.
    expect(both.days.map((day) => [day.key, day.role])).toEqual([
      [SEPT, "past"],
      ["2026-09-23", "today"],
    ]);
    expect(both.marks.filter((mark) => mark.dayKey === "2026-09-23").every((mark) => !mark.tomorrow)).toBe(true);
    expect(both.marks.filter((mark) => mark.dayKey === SEPT).every((mark) => mark.tomorrow)).toBe(true);
    // An expired interval is never promoted and a future one is never announced as now: the next row
    // is current while it really covers the instant, and after the capture's own end the answer is
    // honestly "no current interval".
    expect(currentMarkFor(both.marks, Date.parse("2026-09-23T04:20:00Z"))?.startMs).toBe(
      Date.parse("2026-09-23T04:15:00Z"),
    );
    expect(currentMarkFor(both.marks, Date.parse("2026-09-23T04:30:00Z"))).toBeNull();
    expect(currentMarkFor(both.marks, Date.parse("2026-09-24T04:00:00Z"))).toBeNull();
  });

  it("gives the now fact one identity per interval, stable inside it and different at the edge", () => {
    const rows = rowsFor("2026-09-22T04:00:00Z", [1, 2, 3, 4]);
    const series = chartSeries(rows, TZ, Date.parse("2026-09-22T04:15:00Z"));
    const inside = chartNowAt(series.marks, Date.parse("2026-09-22T04:15:00Z"));
    const stillInside = chartNowAt(series.marks, Date.parse("2026-09-22T04:29:59Z"));
    const nextInterval = chartNowAt(series.marks, Date.parse("2026-09-22T04:30:00Z"));
    expect(inside.identity).not.toBeNull();
    // Same interval, same identity: nothing to redraw, however often the clock is read.
    expect(stillInside.identity).toBe(inside.identity);
    expect(stillInside.mark).toBe(inside.mark);
    // The boundary instant belongs to the interval that starts there, so the identity changes.
    expect(nextInterval.identity).not.toBe(inside.identity);
    // No containing interval: no current mark, no identity, and no borrowed neighbour.
    expect(chartNowAt(series.marks, Date.parse("2026-09-22T09:00:00Z")).identity).toBeNull();
  });

  it("arms the clock for the end of the interval the clock is in, on any accepted day", () => {
    const rows = rowsFor("2026-09-22T04:00:00Z", [1, 2, 3, 4]);
    const series = chartSeries(rows, TZ, Date.parse("2026-09-22T04:30:00Z"));
    expect(nextIntervalBoundary(series.marks, Date.parse("2026-09-22T04:30:00Z"))).toBe(
      Date.parse("2026-09-22T04:45:00Z"),
    );
    // Before today's first interval, the appointment is its start; after the last end, there is none.
    expect(nextIntervalBoundary(series.marks, Date.parse("2026-09-22T03:00:00Z"))).toBe(
      Date.parse("2026-09-22T04:00:00Z"),
    );
    expect(nextIntervalBoundary(series.marks, Date.parse("2026-09-22T07:00:00Z"))).toBeNull();
    // A capture that spans local midnight must arm the *new* day's boundary: an appointment restricted
    // to the first date would stop for good at midnight, leaving the now fact pinned to an expired row
    // until some later read happened to arrive.
    const next = rowsFor("2026-09-23T04:00:00Z", [5, 6], "2026-09-23");
    const both = chartSeries([...rows, ...next], TZ, Date.parse("2026-09-23T04:05:00Z"));
    expect(nextIntervalBoundary(both.marks, Date.parse("2026-09-23T04:05:00Z"))).toBe(
      Date.parse("2026-09-23T04:15:00Z"),
    );
    // Nothing published at all is still no appointment, whichever day it would have been on.
    expect(nextIntervalBoundary(both.marks, Date.parse("2026-09-24T04:00:00Z"))).toBeNull();
  });

  it("computes each day's own mean, so tomorrow's comparison is its own", () => {
    const today = rowsFor("2026-09-22T04:00:00Z", [10, 30], SEPT);
    const tomorrow = rowsFor("2026-09-23T04:00:00Z", [1000, 3000], "2026-09-23");
    const series = chartSeries([...today, ...tomorrow], TZ, Date.parse("2026-09-22T04:30:00Z"));
    expect(series.days).toHaveLength(2);
    expect(series.days[0]?.mean).toBeCloseTo(20, 6);
    expect(series.days[1]?.mean).toBeCloseTo(2000, 6);
    expect(series.marks.filter((mark) => mark.tomorrow)).toHaveLength(2);
  });

  it("keeps zero and negative prices as real marks", () => {
    const series = chartSeries(rowsFor("2026-09-22T04:00:00Z", [0, -50, 100]), TZ, 0);
    expect(series.marks.map((mark) => mark.price)).toEqual([0, -50, 100]);
    expect(series.minValue).toBe(-50);
    expect(series.maxValue).toBe(100);
  });
});

describe("scale and gutter", () => {
  it("never makes the range narrower than one, and shows negative values below zero", () => {
    const flat = chartScale(320, 160, [5, 5]);
    expect(flat.maxValue - flat.minValue).toBeGreaterThanOrEqual(1);
    const negative = chartScale(320, 160, [-20, 10]);
    expect(negative.minValue).toBe(-20);
    const zeroCrossing = chartScale(320, 160, [-5, 5]);
    // y grows downward, so a higher price is drawn higher up the plot and zero sits between them.
    expect(yAt(zeroCrossing, 5)).toBeLessThan(yAt(zeroCrossing, 0));
    expect(yAt(zeroCrossing, 0)).toBeLessThan(yAt(zeroCrossing, -5));
  });

  it("gives three finite, non-duplicate labels on a normal range", () => {
    const labels = gutterLabels(chartScale(320, 160, [-20, 80]));
    expect(labels).toHaveLength(3);
    expect(new Set(labels.map((label) => label.label)).size).toBe(3);
    expect(labels.every((label) => Number.isFinite(label.value))).toBe(true);
  });

  it("keeps every gutter label inside the plot box", () => {
    const scale = chartScale(320, 160, [-20, 80]);
    for (const label of gutterLabels(scale)) {
      expect(label.y).toBeGreaterThanOrEqual(scale.top - 1e-6);
      expect(label.y).toBeLessThanOrEqual(scale.bottom + 1e-6);
    }
  });
});

describe("marks, hit testing and shading", () => {
  it("sizes every mark from one geometry, with no current-only enlargement", () => {
    const scale = chartScale(320, 160, [1, 2, 3]);
    const xs = Array.from({ length: 96 }, (_unused, index) => scale.left + index * ((scale.right - scale.left) / 96));
    const geometry = markGeometry(xs, scale, false);
    expect(geometry.normalRadius * 2).toBeLessThanOrEqual(geometry.spacing * 0.68 + 1e-9);
    // Tomorrow keeps its quieter, smaller point; there is no current size to be had.
    expect(geometry.tomorrowRadius).toBeLessThan(geometry.normalRadius);

    // A full local day of quarter-hours with the current instant inside it: the current target is the
    // same size as an ordinary one, and no drawn point reaches an adjacent centre.
    const rows = rowsFor(
      "2026-09-21T22:00:00Z",
      Array.from({ length: 96 }, (_unused, index) => 100 + index),
      SEPT,
    );
    const series = chartSeries(rows, TZ, Date.parse("2026-09-22T04:30:00Z"));
    const targets = hitTargets(series, scale, series.current);
    const current = targets.find((target) => target.current);
    const ordinary = targets.find((target) => !target.current && !target.mark.tomorrow);
    expect(current).toBeDefined();
    expect(ordinary).toBeDefined();
    expect(current?.radius).toBe(ordinary?.radius);
    const distinct = Array.from(new Set(targets.map((target) => target.x))).sort(
      (left, right) => left - right,
    );
    expect(distinct.length).toBeGreaterThan(1);
    for (let index = 1; index < distinct.length; index += 1) {
      const gap = (distinct[index] as number) - (distinct[index - 1] as number);
      expect((current?.radius ?? 0) * 2).toBeLessThanOrEqual(gap + 1e-9);
    }
  });

  it("agrees between the drawn target and the hit test", () => {
    const rows = rowsFor("2026-09-22T04:00:00Z", [1, 2, 3, 4]);
    const series = chartSeries(rows, TZ, 0);
    const scale = chartScale(320, 160, series.marks.map((mark) => mark.price));
    const targets = hitTargets(series, scale, series.current);
    const target = targets[2];
    expect(target).toBeDefined();
    if (target === undefined) {
      return;
    }
    expect(hitTestMarks(targets, { x: target.x, y: target.y })).toBe(target.mark);
    expect(hitTestMarks(targets, { x: target.x, y: target.y + 500 })).toBeNull();
    expect(nearestMark(targets, { x: target.x, y: target.y })).toBe(target.mark);
  });

  it("walks intervals chronologically and stops at the edges", () => {
    const series = chartSeries(rowsFor("2026-09-22T04:00:00Z", [1, 2, 3]), TZ, 0);
    const marks = series.marks;
    expect(walkMarks(marks, null, 1)).toBe(marks[0]);
    expect(walkMarks(marks, null, -1)).toBe(marks[2]);
    expect(walkMarks(marks, marks[0] as never, 1)).toBe(marks[1]);
    expect(walkMarks(marks, marks[2] as never, 1)).toBe(marks[2]);
  });

  it("shades installed runs, and a proposal only where it is not already installed", () => {
    const payload = rawDashboard();
    (payload.prices as Record<string, unknown>).intervals = quarterHourRows("2026-09-22T04:00:00Z", [1, 2, 3, 4], {
      flags: (index) => ({
        installed_planned: index < 2,
        proposal_planned: true,
      }),
    });
    const result = decodeDashboard(payload);
    expect(result.ok).toBe(true);
    if (!result.ok) {
      return;
    }
    const rows = result.value.prices?.intervals ?? [];
    const series = chartSeries(rows, TZ, 0);
    const bands = plannedBands(rows, series.days, TZ);
    expect(bands.installed).toHaveLength(1);
    expect(bands.installed[0]?.startMs).toBe(Date.parse("2026-09-22T04:00:00Z"));
    expect(bands.installed[0]?.endMs).toBe(Date.parse("2026-09-22T04:30:00Z"));
    // The two installed rows are not doubled in the proposal band; the other two are.
    expect(bands.proposal).toHaveLength(1);
    expect(bands.proposal[0]?.startMs).toBe(Date.parse("2026-09-22T04:30:00Z"));
  });

  it("lets a pointer and the keyboard reach either day where the two share an x", () => {
    // 06:00 local on both days, at different prices, so the two records share a coordinate.
    const today = rowsFor("2026-09-22T04:00:00Z", [10], SEPT);
    const tomorrow = rowsFor("2026-09-23T04:00:00Z", [90], "2026-09-23");
    const series = chartSeries([...today, ...tomorrow], TZ, 0);
    const scale = chartScale(320, 160, series.marks.map((mark) => mark.price));
    const targets = hitTargets(series, scale, null);
    const [todayTarget, tomorrowTarget] = targets as ChartHitTarget[];
    expect(todayTarget?.x).toBeCloseTo(tomorrowTarget?.x as number, 12);
    // A press on tomorrow's own ink selects tomorrow and reports its date and price...
    const atTomorrow = hitTestMarks(targets, {
      x: tomorrowTarget?.x as number,
      y: tomorrowTarget?.y as number,
    });
    expect(atTomorrow).toBe(tomorrowTarget?.mark);
    expect(atTomorrow?.dayKey).toBe("2026-09-23");
    expect(nearestMark(targets, { x: tomorrowTarget?.x as number, y: tomorrowTarget?.y as number })?.price).toBe(90);
    // ...and a press on today's selects today, with the earlier instant winning only an exact tie.
    const atToday = hitTestMarks(targets, { x: todayTarget?.x as number, y: todayTarget?.y as number });
    expect(atToday).toBe(todayTarget?.mark);
    expect(atToday?.dayKey).toBe(SEPT);
    expect(atToday?.price).toBe(10);
    // The keyboard walks both records chronologically, one day after the other.
    expect(walkMarks(series.marks, todayTarget?.mark as never, 1)).toBe(tomorrowTarget?.mark);
    expect(walkMarks(series.marks, tomorrowTarget?.mark as never, -1)).toBe(todayTarget?.mark);
  });

  it("does not stretch or fill a day that stops at its last published interval", () => {
    const series = chartSeries(rowsFor("2026-09-22T04:00:00Z", [1, 2], SEPT), TZ, 0);
    expect(series.marks.map((mark) => mark.wallClock)).toEqual(["06:00", "06:15"]);
    expect(series.marks.map((mark) => mark.position)).toEqual([6 * 60 / 1440, (6 * 60 + 15) / 1440]);
    // Nothing is invented after the last interval, and the axis still carries its five shared ticks:
    // an incomplete document changes the data, never the axis.
    expect(series.axis).toHaveLength(5);
    expect(series.missing).toBe(0);
  });

  it("does not mutate the rows it is given", () => {
    const rows = rowsFor("2026-09-22T04:00:00Z", [3, 1, 2]);
    const snapshot = JSON.stringify(rows);
    chartSeries(rows, TZ, 0);
    expect(JSON.stringify(rows)).toBe(snapshot);
  });

  it("draws nothing at all for an answer without rows", () => {
    const series = chartSeries([], TZ, 0);
    expect(series.marks).toEqual([]);
    expect(series.current).toBeNull();
    expect(series.axis).toEqual([]);
    expect(series.missing).toBe(0);
    expect(decoded().charger.charger_id).toBe("entry_a");
  });

  it("puts the plan bands on the same clock axis as the marks, with their own date", () => {
    const today = rowsFor("2026-09-22T04:00:00Z", [1, 2], SEPT);
    const tomorrow = rowsFor("2026-09-23T04:00:00Z", [3, 4], "2026-09-23");
    const rows = [...today, ...tomorrow];
    const series = chartSeries(rows, TZ, 0);
    const bands = plannedBands(rows, series.days, TZ);
    expect(bands.installed).toEqual([]);
    expect(bands.proposal).toEqual([]);

    // 04:00Z and 04:15Z are 06:00 and 06:15 local: fractions 360/1440 and 375/1440, on both days.
    const flagged = plannedBands(
      rows.map((row) => ({ ...row, installed_planned: true })),
      series.days,
      TZ,
    );
    expect(flagged.installed).toHaveLength(2);
    const [todayBand, tomorrowBand] = flagged.installed as [
      (typeof flagged.installed)[number],
      (typeof flagged.installed)[number],
    ];
    expect(todayBand.dayKey).toBe(SEPT);
    expect(tomorrowBand.dayKey).toBe("2026-09-23");
    expect(todayBand.dayIndex).toBe(0);
    expect(tomorrowBand.dayIndex).toBe(1);
    expect(todayBand.startPosition).toBeCloseTo(6 * 60 / 1440, 12);
    expect(todayBand.endPosition).toBeCloseTo((6 * 60 + 30) / 1440, 12);
    // The two days shade the very same x span, and stay two records.
    expect(tomorrowBand.startPosition).toBeCloseTo(todayBand.startPosition, 12);
    expect(tomorrowBand.endPosition).toBeCloseTo(todayBand.endPosition, 12);
  });

  it("gives both days one five-tick axis, whatever the day lengths are", () => {
    const today = rowsFor("2026-09-22T04:00:00Z", [1, 2], SEPT);
    const tomorrow = rowsFor("2026-09-23T04:00:00Z", [3, 4], "2026-09-23");
    const series = chartSeries([...today, ...tomorrow], TZ, 0);
    expect(series.axis).toHaveLength(5);
    expect(series.axis.map((tick) => tick.position)).toEqual([0, 0.25, 0.5, 0.75, 1]);
    expect(new Set(series.axis.map((tick) => tick.instantMs)).size).toBe(5);
    const scale = chartScale(320, 160, [1, 2]);
    const xOf = (position: number): number => xForWallClock(scale, position);
    const xs = series.axis.map((tick) => xOf(tick.position));
    expect(xs[0]).toBeCloseTo(scale.left, 9);
    expect(xs[4]).toBeCloseTo(scale.right, 9);
    expect(new Set(xs).size).toBe(5);
    // The right edge is the day's own end, so the tick reads the next local midnight.
    const clock = zoneClock(TZ);
    expect(clock.msOfDay(series.axis[4]?.instantMs as number)).toBe(0);
  });
});

// -------------------------------------------------------------- the rollover across local midnight

/** Local midnight in the fixture's market: 2026-09-22T22:00Z is 00:00 local on the 23rd. */
const MIDNIGHT_MS = Date.parse("2026-09-22T22:00:00Z");
const NEW_DAY = "2026-09-23";

/** Quarter-hour rows in a named market zone, with that zone in the payload the decoder checks against. */
function rowsIn(
  zone: string,
  startIso: string,
  prices: readonly (number | null)[],
  day: string,
  duration = 15,
): PriceInterval[] {
  const payload = rawDashboard({
    market: { ...(rawDashboard().market as Record<string, unknown>), timezone: zone },
  });
  (payload.prices as Record<string, unknown>).intervals = quarterHourRows(startIso, prices, { day, duration });
  const result = decodeDashboard(payload);
  if (!result.ok || result.value.prices === null) {
    throw new Error("fixture did not decode");
  }
  return result.value.prices.intervals;
}

/**
 * The day the clock leaves and the day it enters, as one accepted capture.
 *
 * This is what a plan horizon calculated before local midnight publishes, and what any read accepted
 * before it holds: 23:45 local on the old date, then the new date from 00:00 local on.
 */
function acrossMidnight(): PriceInterval[] {
  return [
    ...rowsFor("2026-09-22T21:45:00Z", [10], SEPT),
    ...rowsFor("2026-09-22T22:00:00Z", [20, 30, 40, 50], NEW_DAY),
  ];
}

describe("the rollover across local midnight", () => {
  it("changes the now identity exactly once, at local midnight", () => {
    const marks = chartSeries(acrossMidnight(), TZ, MIDNIGHT_MS - 60_000).marks;
    const before = chartNowAt(marks, MIDNIGHT_MS - 1);
    const atMidnight = chartNowAt(marks, MIDNIGHT_MS);
    const inside = chartNowAt(marks, MIDNIGHT_MS + 10 * 60_000);
    // The last instant of the old day is still its last quarter; midnight itself belongs to the new
    // day's first one, so the identity changes once and only there.
    expect(before.mark?.dayKey).toBe(SEPT);
    expect(before.mark?.wallClock).toBe("23:45");
    expect(atMidnight.mark?.dayKey).toBe(NEW_DAY);
    expect(atMidnight.mark?.wallClock).toBe("00:00");
    expect(atMidnight.identity).not.toBe(before.identity);
    // One change at the boundary and none inside the interval: the identity *is* the interval.
    expect(inside.identity).toBe(atMidnight.identity);
    expect(inside.mark).toBe(atMidnight.mark);
    // Midnight is the appointment, and the next one is the new interval's own end.
    expect(nextIntervalBoundary(marks, MIDNIGHT_MS - 1)).toBe(MIDNIGHT_MS);
    expect(nextIntervalBoundary(marks, MIDNIGHT_MS)).toBe(MIDNIGHT_MS + 15 * 60_000);
  });

  it("resolves the observed case: at 00:11 the accepted 00:00-00:15 interval is current", () => {
    const at = MIDNIGHT_MS + 11 * 60_000;
    const series = chartSeries(acrossMidnight(), TZ, at);
    expect(series.current?.dayKey).toBe(NEW_DAY);
    expect(series.current?.wallClock).toBe("00:00");
    expect(series.current?.price).toBe(20);
    // The former next-day series *is* today's, and the date the clock has left is the subdued one --
    // the roles follow the market clock, never the order the two dates arrived in.
    expect(series.days.map((day) => [day.key, day.role])).toEqual([
      [SEPT, "past"],
      [NEW_DAY, "today"],
    ]);
    expect(series.marks.filter((mark) => mark.dayKey === NEW_DAY).every((mark) => !mark.tomorrow)).toBe(true);
    expect(series.marks.filter((mark) => mark.dayKey === SEPT).every((mark) => mark.tomorrow)).toBe(true);
  });

  it("never promotes an expired row across a real gap in the new day", () => {
    // The new day's own capture begins at 00:15: the first quarter after midnight is genuinely absent,
    // and the expired 23:45 row must not be borrowed as if it still covered the instant.
    const rows = [
      ...rowsFor("2026-09-22T21:45:00Z", [10], SEPT),
      ...rowsFor("2026-09-22T22:15:00Z", [20, 30], NEW_DAY),
    ];
    const at = MIDNIGHT_MS + 5 * 60_000;
    const series = chartSeries(rows, TZ, at);
    expect(series.current).toBeNull();
    expect(chartNowAt(series.marks, at).identity).toBeNull();
    // The clock is still armed for the next real boundary, so the gap closes by itself.
    expect(nextIntervalBoundary(series.marks, at)).toBe(MIDNIGHT_MS + 15 * 60_000);
  });

  it("gives the boundary instant to the interval that starts there", () => {
    const quarter = MIDNIGHT_MS + 15 * 60_000;
    const current = currentMarkFor(chartSeries(acrossMidnight(), TZ, quarter).marks, quarter);
    expect(current?.startMs).toBe(quarter);
    expect(current?.wallClock).toBe("00:15");
    expect(current?.dayKey).toBe(NEW_DAY);
  });

  it("lets the market zone own the date transition, not the reader's zone", () => {
    // 2026-10-25T11:00Z is local midnight on the 26th in Auckland, and still 11:00 on the 25th in UTC.
    // One single row therefore reads two different ways, and only the market's own clock decides which
    // date is the day the now fact is in.
    const rows = rowsIn("Pacific/Auckland", "2026-10-25T11:00:00Z", [10], "2026-10-26", 60);
    const before = chartSeries(rows, "Pacific/Auckland", Date.parse("2026-10-25T10:59:00Z"));
    const market = chartSeries(rows, "Pacific/Auckland", Date.parse("2026-10-25T11:00:00Z"));
    expect(before.days[0]?.role).toBe("future");
    expect(before.current).toBeNull();
    expect(market.days[0]?.role).toBe("today");
    expect(market.current?.startMs).toBe(Date.parse("2026-10-25T11:00:00Z"));
    // Read against another zone the very same instant is still a coming day and no date is today's.
    const elsewhere = chartSeries(rows, "UTC", Date.parse("2026-10-25T11:00:00Z"));
    expect(elsewhere.days.some((day) => day.role === "today")).toBe(false);
  });

  it("refuses an ambiguous overlap instead of letting array order decide", () => {
    // The decoder already refuses overlapping rows, so this can only arrive through some future path --
    // and if it ever does, no order of the array may pick the price. The authority rule is the market
    // date of the now fact: the row whose own `day` is that date owns the instant.
    const marks = chartSeries(
      rowsFor("2026-09-22T04:00:00Z", [1, 2], SEPT),
      TZ,
      Date.parse("2026-09-22T04:10:00Z"),
    ).marks;
    const first = marks[0] as ChartMark;
    const at = first.startMs + 60_000;
    const expired = { ...first, dayKey: "2026-09-21", role: "past" as const, price: 99 };
    expect(currentMarkFor([expired, first], at)?.price).toBe(first.price);
    expect(currentMarkFor([first, expired], at)?.price).toBe(first.price);
    // Two rows of the *same* date: no authority can name one of them, so the answer is unavailable,
    // never whichever of the two happens to come first.
    const sameDate = { ...first, price: 42 };
    expect(currentMarkFor([sameDate, first], at)).toBeNull();
    expect(currentMarkFor([first, sameDate], at)).toBeNull();
  });
});
