// The chart's time model and geometry, as pure arithmetic.
//
// The rules match the other SpotNav clients; the cross-client fixture in
// `frontend/test/visual-fixtures.ts` names the values they agree on.
//
// Time model. The x axis is one `TimeAxis` with two implementations, chosen by a display preference of
// this browser (`ChartAxisMode`):
//   * "day" (the default), the wall-clock overlay: the local wall clock of one day, 00:00 to 24:00
//     across the full width, today and tomorrow overlaid on it (`wallClockAxis`). The rules below are
//     this axis's.
//   * "from_now", the real-time range: from the start of the current hour to the end of the last
//     published day, placed by instant (`rangeAxis`). Only when a coming day is published; otherwise
//     "from_now" is exactly "day". On the range nothing is overlaid: no mark is subdued and no band is
//     hatched, a run across midnight is one band, and what ended before the axis starts is not drawn.
//   * Rows are grouped by the backend's `day`; a mark's x is the fraction of its own local wall clock.
//   * A date's role (`past`, `today`, `future`) is read from the market's clock at the injected now
//     instant, never from a day's position in the rows: an accepted capture can still hold the day the
//     clock has just left. `dayIndex` is an ordering fact only; the day the now fact is in is drawn as
//     today's series, everything else is subdued.
//   * A mark keeps its full identity (date, instant, offset, role). Equal x does not mean equal record;
//     selection, readout and keyboard walk work on the instant, never the coordinate.
//   * An autumn repeated hour keeps both occurrences, offset-disambiguated; a spring missing hour has no
//     rows and so no marks.
//   * 23-, 24- and 25-hour days are neither stretched nor filled; the axis keeps its five fixed ticks.
//
// Every instant becomes a position through the series' `time.position`, and every position an x through
// `xForWallClock(scale, position)`: marks, hit targets, plan bands, ticks, the now line, the selection
// line and the pointer's inverse mapping.

import type { PriceInterval } from "./validate";
import { CHART_DENSITY, type ChartDensityStyle } from "./visual-styles";

const MINUTES_PER_DAY = 1440;
const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;

export interface ZoneClock {
  timeZone: string;
  msOfDay(instantMs: number): number;
  dayKey(instantMs: number): string;
  offsetLabel(instantMs: number): string;
}

const clocks = new Map<string, ZoneClock>();

/**
 * A clock for an IANA zone; the zone is assumed valid (the decoder rejects invalid ones).
 *
 * An empty zone means the backend named none: geometry falls back to UTC, a fixed choice that is not the
 * browser's zone, so drawn days never rearrange by reader location. Labels are then unavailable.
 */
export function zoneClock(timeZone: string): ZoneClock {
  const requested = timeZone === "" ? "UTC" : timeZone;
  const existing = clocks.get(requested);
  if (existing !== undefined) {
    return existing;
  }
  const formatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: requested,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const partsOf = (instantMs: number): Record<string, string> => {
    const parts: Record<string, string> = {};
    for (const part of formatter.formatToParts(new Date(instantMs))) {
      parts[part.type] = part.value;
    }
    return parts;
  };
  const offsetFormatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: requested,
    timeZoneName: "shortOffset",
    hour: "2-digit",
  });
  const clock: ZoneClock = {
    timeZone: requested,
    msOfDay(instantMs: number): number {
      const parts = partsOf(instantMs);
      return (
        (Number(parts.hour) % 24) * 3_600_000 +
        Number(parts.minute) * 60_000 +
        Number(parts.second) * 1_000
      );
    },
    dayKey(instantMs: number): string {
      const parts = partsOf(instantMs);
      return `${parts.year}-${parts.month}-${parts.day}`;
    },
    offsetLabel(instantMs: number): string {
      const parts = offsetFormatter.formatToParts(new Date(instantMs));
      return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
    },
  };
  clocks.set(requested, clock);
  return clock;
}

export function localDayKey(instantMs: number, timeZone: string): string {
  return zoneClock(timeZone).dayKey(instantMs);
}

export function msOfDay(instantMs: number, timeZone: string): number {
  return zoneClock(timeZone).msOfDay(instantMs);
}

/**
 * The instant at which the local day containing `instantMs` begins.
 *
 * The wall-clock estimate is corrected by testing the hour either side and keeping the candidate whose
 * local reading is closest to midnight, so 23- and 25-hour days work without a timezone library.
 */
export function localMidnightAt(instantMs: number, timeZone: string): number {
  const clock = zoneClock(timeZone);
  const estimate = instantMs - clock.msOfDay(instantMs);
  let best = estimate;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const candidate of [estimate - HOUR_MS, estimate, estimate + HOUR_MS]) {
    const reading = clock.msOfDay(candidate);
    const distance = Math.min(reading, 24 * HOUR_MS - reading);
    if (distance < bestDistance) {
      best = candidate;
      bestDistance = distance;
    }
  }
  return best;
}

export function localDayLengthMs(dayStartMs: number, timeZone: string): number {
  const next = localMidnightAt(dayStartMs + 28 * HOUR_MS, timeZone);
  return next - dayStartMs;
}


export interface ChartMetrics {
  width: number;
  height: number;
  scale: number;
  pad: number;
  headerTextSize: number;
  axisTextSize: number;
  top: number;
  bottom: number;
  left: number;
  right: number;
  /** How dense the chart is drawn (`CHART_DENSITY`): the ordinary card, or the `chart_detail` option. */
  density: ChartDensityStyle;
}

export interface ChartScale extends ChartMetrics {
  minValue: number;
  maxValue: number;
  /** The price axis's step (minor unit) when the density asks for stepped labels; the scale is widened to whole steps. */
  priceStep?: number;
}

export interface ChartPoint {
  x: number;
  y: number;
}

const MIN_SCALE = 0.72;
const MAX_SCALE = 1.45;
const HEADER_TEXT = 15;
const AXIS_TEXT = 12;
const HEADER_CAP = 0.115;
const AXIS_CAP = 0.082;

/**
 * Metrics for the plan-card profile: denser text, no footer. `axisRows` is the lines of text under the
 * plot: two when a detailed range axis names a new day under its hour label.
 */
export function chartMetrics(
  width: number,
  height: number,
  density: ChartDensityStyle = CHART_DENSITY.normal,
  axisRows = 1,
): ChartMetrics {
  const w = Math.max(180, width);
  const h = Math.max(100, height);
  const scale = Math.min(Math.max(Math.min(w / 420, h / 220), MIN_SCALE), MAX_SCALE);
  const pad = 14 * scale;
  const headerTextSize = Math.min(HEADER_TEXT, h * HEADER_CAP);
  const textShare = density.axisFontPx / CHART_DENSITY.normal.axisFontPx;
  const axisTextSize = Math.min(AXIS_TEXT, h * AXIS_CAP) * textShare;
  return {
    width: w,
    height: h,
    scale,
    pad,
    headerTextSize,
    axisTextSize,
    top: pad * 0.9,
    bottom: h - pad - axisTextSize * (1.35 + (Math.max(1, axisRows) - 1) * 1.2),
    left: pad + Math.max(axisTextSize * 2.5, w * 0.07),
    right: w - pad,
    density,
  };
}

export function chartScale(
  width: number,
  height: number,
  values: readonly number[],
  density: ChartDensityStyle = CHART_DENSITY.normal,
  axisRows = 1,
): ChartScale {
  const metrics = chartMetrics(width, height, density, axisRows);
  const finite = values.filter((value) => Number.isFinite(value));
  const minValue = Math.min(0, ...(finite.length > 0 ? finite : [0]));
  const maxValue = Math.max(1, ...(finite.length > 0 ? finite : [1]));
  if (density.priceSteps <= 0) {
    return { ...metrics, minValue, maxValue };
  }
  const priceStep = niceStep(minValue, maxValue, density.priceSteps);
  return {
    ...metrics,
    minValue: Math.floor(minValue / priceStep + 1e-9) * priceStep,
    maxValue: Math.ceil(maxValue / priceStep - 1e-9) * priceStep,
    priceStep,
  };
}

/**
 * A nice step (1, 2 or 5 times a power of ten) that divides the span from `min` to `max`, widened to whole
 * steps, into about `aim` steps: the closest count to `aim`, never fewer than `aim - 1`.
 */
export function niceStep(min: number, max: number, aim: number): number {
  const span = Math.max(max - min, 1e-9);
  const magnitude = 10 ** Math.floor(Math.log10(span / aim));
  let best = magnitude;
  let bestScore = Number.POSITIVE_INFINITY;
  for (const power of [magnitude / 10, magnitude, magnitude * 10]) {
    for (const factor of [1, 2, 5]) {
      const step = power * factor;
      const count = Math.ceil(max / step - 1e-9) - Math.floor(min / step + 1e-9);
      if (count < aim - 1) {
        continue;
      }
      const score = Math.abs(count - aim);
      if (score < bestScore || (score === bestScore && step > best)) {
        best = step;
        bestScore = score;
      }
    }
  }
  return best;
}

/**
 * The one x mapping: a wall-clock fraction of one day across the whole plot width.
 *
 * `position` is `msOfDay` over 24 hours (0 is 00:00 at the left edge, 1 is 24:00 at the right), for today
 * and tomorrow alike. It is the only x function in the card.
 */
export function xForWallClock(scale: ChartMetrics, position: number): number {
  const share = Math.min(Math.max(position, 0), 1);
  return scale.left + share * (scale.right - scale.left);
}

export function wallClockPosition(clock: ZoneClock, instantMs: number): number {
  return clock.msOfDay(instantMs) / DAY_MS;
}

/**
 * How the x axis reads time: the wall-clock overlay of one day ("day", the default) or real time from
 * the current hour to the end of the last published day ("from_now"). A display preference of this
 * browser, never a charger setting.
 */
export type ChartAxisMode = "day" | "from_now";

export const CHART_AXIS_MODES: readonly ChartAxisMode[] = ["day", "from_now"];

export const DEFAULT_CHART_AXIS: ChartAxisMode = "day";

/**
 * The one time mapping, with two implementations (`wallClockAxis`, `rangeAxis`). Everything that turns
 * an instant into a position on the plot asks this object; nothing else knows which axis is drawn.
 */
export interface TimeAxis {
  /** `wall_clock`: one day 00-24, other days overlaid on it. `range`: real time from `startMs` to `endMs`. */
  kind: "wall_clock" | "range";
  /** Other days are drawn over the now-fact day: their marks subdued, their plan bands hatched. */
  overlay: boolean;
  /** The instants at the left and right edges; `null` on the wall-clock axis, which has one per day. */
  startMs: number | null;
  endMs: number | null;
  /** The time the full plot width stands for: 24 hours on the wall-clock axis, end minus start on the range. */
  spanMs: number;
  /**
   * The x fraction (0 left edge, 1 right edge) of an instant. `edge: "end"` reads the instant as the end of
   * a span: on the wall-clock axis an end on local midnight is that day's 24:00, not the next 00:00.
   */
  position(instantMs: number, edge?: "start" | "end"): number;
  /** Whether the half-open span `[startMs, endMs)` has any part on the axis. */
  shows(startMs: number, endMs: number): boolean;
}

/** The wall-clock overlay: an instant's place is its local time of day, whichever day it is on. */
export function wallClockAxis(clock: ZoneClock): TimeAxis {
  return {
    kind: "wall_clock",
    overlay: true,
    startMs: null,
    endMs: null,
    spanMs: DAY_MS,
    position(instantMs, edge = "start") {
      const position = wallClockPosition(clock, instantMs);
      return edge === "end" && position === 0 ? 1 : position;
    },
    shows: () => true,
  };
}

/** The real-time range: an instant's place is its share of the time from `startMs` to `endMs`. */
export function rangeAxis(startMs: number, endMs: number): TimeAxis {
  const spanMs = Math.max(1, endMs - startMs);
  return {
    kind: "range",
    overlay: false,
    startMs,
    endMs,
    spanMs,
    position: (instantMs) => (instantMs - startMs) / spanMs,
    shows: (from, to) => to > startMs && from < endMs,
  };
}

/** The instant the local hour containing `instantMs` began (zones with half-hour offsets included). */
export function localHourStart(instantMs: number, clock: ZoneClock): number {
  return instantMs - (clock.msOfDay(instantMs) % HOUR_MS);
}

/**
 * The axis the chart draws for a mode. "from_now" is the range from the start of the current hour to the
 * end of the last published day, but only while a coming day is published (a day after the now-fact day
 * with at least one row); without one it is the wall-clock axis, so the two modes then draw the same.
 */
export function timeAxisFor(
  mode: ChartAxisMode,
  days: readonly ChartDay[],
  clock: ZoneClock,
  nowMs: number,
): TimeAxis {
  const last = days[days.length - 1];
  if (mode !== "from_now" || last === undefined || !days.some((day) => day.role === "future" && day.slots > 0)) {
    return wallClockAxis(clock);
  }
  const startMs = localHourStart(nowMs, clock);
  const endMs = last.startMs + last.lengthMs;
  return endMs > startMs ? rangeAxis(startMs, endMs) : wallClockAxis(clock);
}

export function yAt(scale: ChartScale, value: number): number {
  const span = scale.maxValue - scale.minValue;
  const share = span <= 0 ? 0 : (value - scale.minValue) / span;
  return scale.bottom - share * (scale.bottom - scale.top);
}

/**
 * The price axis's labels: the bottom, middle and top of the scale. `format` writes one value (given in the
 * minor unit) for the axis's span; without it, whole minor units.
 */
export function gutterLabels(
  scale: ChartScale,
  format: (value: number, span: number) => string = (value) => String(Math.round(value)),
): Array<{ value: number; y: number; label: string }> {
  const candidates =
    scale.maxValue - scale.minValue < 1e-9
      ? [scale.minValue]
      : scale.priceStep !== undefined
        ? steppedValues(scale.minValue, scale.maxValue, scale.priceStep)
        : [scale.minValue, (scale.minValue + scale.maxValue) / 2, scale.maxValue];
  const seen = new Set<string>();
  const labels: Array<{ value: number; y: number; label: string }> = [];
  for (const value of candidates) {
    const label = format(value, scale.maxValue - scale.minValue);
    if (seen.has(label)) {
      continue;
    }
    seen.add(label);
    labels.push({ value, y: yAt(scale, value), label });
  }
  return labels;
}

/** Every whole step from `min` to `max` (both on steps), rounded so float steps never read 0.30000000000000004. */
function steppedValues(min: number, max: number, step: number): number[] {
  const first = Math.round(min / step);
  const last = Math.round(max / step);
  const values: number[] = [];
  for (let index = first; index <= last; index += 1) {
    values.push(Number((index * step).toPrecision(12)));
  }
  return values;
}

export function hourMarkers(): number[] {
  return [0, 6, 12, 18, 24].map((hour) => hour / 24);
}


/**
 * A date's part in the picture, decided in the market's timezone from the injected now instant: the day
 * the now fact is in, a day the capture has left behind, or a coming day.
 *
 * Presentation only, never a reason to deny an instant: an interval is current because its half-open
 * instant range contains `now`, whatever role its date reads.
 */
export type ChartDayRole = "past" | "today" | "future";

function dayRoleOf(dayKey: string, todayKey: string): ChartDayRole {
  if (dayKey === todayKey) {
    return "today";
  }
  return dayKey < todayKey ? "past" : "future";
}

export interface ChartDay {
  key: string;
  role: ChartDayRole;
  startMs: number;
  lengthMs: number;
  slots: number;
  mean: number | null;
  min: number | null;
  max: number | null;
}

/**
 * One drawn interval.
 *
 * `position` is where it is drawn (wall-clock fraction of one day). Two records may share a `position`
 * (the two passes of a repeated hour, or today's and tomorrow's 06:15) without being the same record;
 * identity is `startMs` plus the day and offset metadata, never the coordinate.
 */
export interface ChartMark {
  startMs: number;
  endMs: number;
  durationMinutes: number;
  price: number;
  dayKey: string;
  dayIndex: number;
  role: ChartDayRole;
  /**
   * The subdued treatment: this interval is not part of the market-local day the now fact is in. Never a
   * reason to deny an interval: `startMs`/`endMs` decide currency.
   */
  tomorrow: boolean;
  hourly: boolean;
  proposalPlanned: boolean;
  installedPlanned: boolean;
  position: number;
  wallClock: string;
  offset: string;
}

export interface AxisTick {
  position: number;
  instantMs: number;
  /**
   * A local midnight inside the range axis, where one day gives way to the next: drawn as a divider and
   * labelled with the new day (weekday and date) rather than its time. Never set on the wall-clock axis
   * or on an edge.
   */
  dayStart?: true;
}

export interface ChartSeries {
  marks: ChartMark[];
  days: ChartDay[];
  /**
   * The ticks: on the wall-clock axis the one sequence both days share (five wall-clock positions); on the
   * range every local 00/06/12/18 inside it plus the right edge.
   */
  axis: AxisTick[];
  /**
   * A tick at every local hour the axis shows (the left edge and each hour start inside, not the right
   * edge), with the midnight inside the range as a `dayStart`: the labels of the `chart_detail` option.
   */
  hours: AxisTick[];
  /** The time mapping every position in this series came from. */
  time: TimeAxis;
  hourly: boolean;
  minValue: number;
  maxValue: number;
  current: ChartMark | null;
  missing: number;
}

function wallClockOf(instantMs: number, clock: ZoneClock): string {
  const dayMs = clock.msOfDay(instantMs);
  const hours = Math.floor(dayMs / HOUR_MS);
  const minutes = Math.floor((dayMs % HOUR_MS) / 60_000);
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}

/**
 * The drawn series, from the decoder's rows.
 *
 * Rows are grouped by their own `day`, ordered by instant, and positioned by local wall clock on the one
 * shared axis. Nothing is averaged, resampled, extrapolated or invented: a row with no effective price
 * counts as missing and draws nothing (never a zero mark), and a day that stops at noon simply stops.
 */
export function chartSeries(
  rows: readonly PriceInterval[],
  timeZone: string,
  nowMs: number,
  mode: ChartAxisMode = DEFAULT_CHART_AXIS,
): ChartSeries {
  const clock = zoneClock(timeZone);
  // The one authority for a date's role: the market's local day at the now instant, never inferred from row order or the host's zone.
  const todayKey = localDayKey(nowMs, timeZone);
  const ordered = rows
    .slice()
    .sort((left, right) => left.startMs - right.startMs || left.endMs - right.endMs);

  const byDay = new Map<string, PriceInterval[]>();
  const dayOrder: string[] = [];
  for (const row of ordered) {
    const bucket = byDay.get(row.day);
    if (bucket === undefined) {
      byDay.set(row.day, [row]);
      dayOrder.push(row.day);
    } else {
      bucket.push(row);
    }
  }

  const days: ChartDay[] = [];
  for (const key of dayOrder) {
    const bucket = byDay.get(key) as PriceInterval[];
    const startMs = localMidnightAt((bucket[0] as PriceInterval).startMs, timeZone);
    const prices = bucket
      .map((row) => row.effective_price)
      .filter((price): price is number => price !== null && Number.isFinite(price));
    days.push({
      key,
      role: dayRoleOf(key, todayKey),
      startMs,
      lengthMs: Math.max(1, localDayLengthMs(startMs, timeZone)),
      slots: bucket.length,
      mean: prices.length === 0 ? null : prices.reduce((total, price) => total + price, 0) / prices.length,
      min: prices.length === 0 ? null : Math.min(...prices),
      max: prices.length === 0 ? null : Math.max(...prices),
    });
  }

  const time = timeAxisFor(mode, days, clock, nowMs);
  const marks: ChartMark[] = [];
  for (const [dayIndex, day] of days.entries()) {
    const bucket = byDay.get(day.key) as PriceInterval[];
    for (const row of bucket) {
      const price = row.effective_price;
      if (price === null || !Number.isFinite(price)) {
        continue; // Missing, never zero.
      }
      if (!time.shows(row.startMs, row.endMs)) {
        continue; // Over before the range axis starts: not drawn.
      }
      const hourly = row.duration_minutes >= 60;
      const drawnMs = hourly ? row.startMs + row.duration_minutes * 60_000 / 2 : row.startMs;
      marks.push({
        startMs: row.startMs,
        endMs: row.endMs,
        durationMinutes: row.duration_minutes,
        price,
        dayKey: row.day,
        dayIndex,
        role: day.role,
        tomorrow: time.overlay && day.role !== "today",
        hourly,
        proposalPlanned: row.proposal_planned,
        installedPlanned: row.installed_planned,
        position: time.position(drawnMs),
        wallClock: wallClockOf(drawnMs, clock),
        offset: clock.offsetLabel(drawnMs),
      });
    }
  }

  const prices = marks.map((mark) => mark.price);
  return {
    marks,
    days,
    axis: time.kind === "range" ? rangeTicks(time, days, clock) : axisTicks(days, clock),
    hours: hourTicks(time, days, clock),
    time,
    hourly: marks.length > 0 && marks.every((mark) => mark.hourly),
    minValue: Math.min(0, ...(prices.length > 0 ? prices : [0])),
    maxValue: Math.max(1, ...(prices.length > 0 ? prices : [1])),
    current: currentMarkFor(marks, nowMs),
    missing: rows.length - marks.length,
  };
}

/**
 * The published interval containing `nowMs`, searched across every accepted day by instant.
 *
 * An interval's truth is its half-open range `[startMs, endMs)` in the market's clock, whatever date the
 * rows label it with. A capture still holding the day the clock has left neither hides the covering
 * interval nor promotes an expired one.
 *
 * The result is unavailable when no interval covers `now`, or when a malformed capture offers several
 * containing intervals and the authority rule cannot name one. The nearest interval is never borrowed, an
 * expired one never promoted, a future one never announced as "now"; the boundary instant belongs to the
 * interval starting there.
 *
 * The authority rule for overlap mirrors the backend's (`planning_slots`): the interval whose date is the
 * market-local day of the now fact owns the instant. If that leaves several or none, the answer is `null`
 * rather than resolved by array order.
 */
export function currentMarkFor(marks: readonly ChartMark[], nowMs: number): ChartMark | null {
  const containing = marks.filter((mark) => mark.startMs <= nowMs && nowMs < mark.endMs);
  if (containing.length <= 1) {
    return containing[0] ?? null;
  }
  const onTheNowFactDay = containing.filter((mark) => mark.role === "today");
  return onTheNowFactDay.length === 1 ? (onTheNowFactDay[0] as ChartMark) : null;
}

/**
 * A record's identity, as the request carries it: its day and instant, never its x. Equal wall-clock x
 * (a repeated hour, or the same time tomorrow) still gives different identities.
 */
export function intervalIdentity(mark: ChartMark | null): string | null {
  return mark === null ? null : `${mark.dayKey}@${mark.startMs}`;
}

export interface ChartNow {
  mark: ChartMark | null;
  identity: string | null;
}

/**
 * The now fact at an instant. `now` is a parameter all the way down (geometry never reads the clock),
 * and the identity changes only when the containing interval changes.
 */
export function chartNowAt(marks: readonly ChartMark[], nowMs: number): ChartNow {
  const mark = currentMarkFor(marks, nowMs);
  return { mark, identity: intervalIdentity(mark) };
}

/**
 * When the now fact can next change: the next interval boundary of any accepted day (an end for the
 * current interval, or the first start when none is current). `null` means nothing published is ahead.
 *
 * Both days are considered because the day the clock just entered may be the one the rows labelled last.
 */
export function nextIntervalBoundary(marks: readonly ChartMark[], nowMs: number): number | null {
  let earliest: number | null = null;
  for (const mark of marks) {
    // A future interval arms its start; the interval the clock is inside arms its end.
    const candidate = mark.startMs > nowMs ? mark.startMs : mark.endMs > nowMs ? mark.endMs : null;
    if (candidate === null) {
      continue;
    }
    if (earliest === null || candidate < earliest) {
      earliest = candidate;
    }
  }
  return earliest;
}

/**
 * The shared axis: five ticks at fixed 00:00, 06:00, 12:00, 18:00 and 24:00, whichever days are
 * published, so 23-, 24- and 25-hour days draw the same axis. The instant is carried so the view can name
 * the tick in the reader's zone and language; this module formats nothing. The right edge is the first
 * day's end instant, the next midnight.
 */
export function axisTicks(days: readonly ChartDay[], clock: ZoneClock): AxisTick[] {
  const first = days[0];
  if (first === undefined) {
    return [];
  }
  return hourMarkers().map((position) =>
    position >= 1
      ? { position: 1, instantMs: first.startMs + first.lengthMs }
      : { position, instantMs: instantAtWallClock(days, position * MINUTES_PER_DAY, clock) },
  );
}

/**
 * The range axis's ticks: every local 00:00, 06:00, 12:00 and 18:00 of the published days that falls
 * inside the range (each found by instant, so a 23- or 25-hour day keeps its ticks on those wall clocks),
 * then the right edge. A midnight strictly inside is a `dayStart`; the left edge, the current hour, has no
 * tick of its own.
 */
export function rangeTicks(axis: TimeAxis, days: readonly ChartDay[], clock: ZoneClock): AxisTick[] {
  const startMs = axis.startMs ?? 0;
  const endMs = axis.endMs ?? startMs;
  const ticks: AxisTick[] = [];
  for (const day of days) {
    for (const hour of [0, 6, 12, 18]) {
      const instantMs = hour === 0 ? day.startMs : instantAtWallClock([day], hour * 60, clock);
      if (instantMs < startMs || instantMs >= endMs) {
        continue;
      }
      const tick: AxisTick = { position: axis.position(instantMs), instantMs };
      if (hour === 0 && instantMs > startMs) {
        tick.dayStart = true;
      }
      ticks.push(tick);
    }
  }
  ticks.push({ position: 1, instantMs: endMs });
  return ticks;
}

const HOURS_OF_DAY = Array.from({ length: 24 }, (_, hour) => hour);

/**
 * A tick at every local hour on the axis, for the `chart_detail` labels. On the wall-clock axis the 24
 * wall-clock hours 00 to 23 (found by instant on the first day, like `axisTicks`); on the range every
 * local hour start of the published days inside it, a midnight strictly inside marked `dayStart`. A
 * spring day's missing hour falls on its neighbour's instant and is kept once.
 */
export function hourTicks(axis: TimeAxis, days: readonly ChartDay[], clock: ZoneClock): AxisTick[] {
  const first = days[0];
  if (first === undefined) {
    return [];
  }
  if (axis.kind !== "range") {
    const seen = new Set<number>();
    const ticks: AxisTick[] = [];
    for (const hour of HOURS_OF_DAY) {
      const instantMs = hour === 0 ? first.startMs : instantAtWallClock(days, hour * 60, clock);
      if (!seen.has(instantMs)) {
        seen.add(instantMs);
        ticks.push({ position: hour / 24, instantMs });
      }
    }
    return ticks;
  }
  const startMs = axis.startMs ?? 0;
  const endMs = axis.endMs ?? startMs;
  const seen = new Set<number>();
  const ticks: AxisTick[] = [];
  for (const day of days) {
    for (const hour of HOURS_OF_DAY) {
      const instantMs = hour === 0 ? day.startMs : instantAtWallClock([day], hour * 60, clock);
      if (instantMs < startMs || instantMs >= endMs || seen.has(instantMs)) {
        continue;
      }
      seen.add(instantMs);
      const tick: AxisTick = { position: axis.position(instantMs), instantMs };
      if (hour === 0 && instantMs > startMs) {
        tick.dayStart = true;
      }
      ticks.push(tick);
    }
  }
  return ticks;
}

/**
 * An instant that reads `minuteOfDay` on the wall clock of one of these days.
 *
 * Corrected like `localMidnightAt`: test the hour either side and keep the candidate closest to the
 * wanted minute, so the 06:00 and 18:00 ticks stay on 06:00 and 18:00 on 23- or 25-hour days.
 */
function instantAtWallClock(
  days: readonly ChartDay[],
  minuteOfDay: number,
  clock: ZoneClock,
): number {
  const first = days[0] as ChartDay;
  let best = first.startMs + minuteOfDay * 60_000;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const day of days) {
    const estimate = day.startMs + minuteOfDay * 60_000;
    for (const candidate of [estimate - HOUR_MS, estimate, estimate + HOUR_MS]) {
      const reading = clock.msOfDay(candidate) / 60_000;
      const gap = Math.abs(reading - minuteOfDay);
      const distance = Math.min(gap, MINUTES_PER_DAY - gap);
      if (distance < bestDistance) {
        best = candidate;
        bestDistance = distance;
      }
    }
  }
  return best;
}


export const HIT_SLOP = 20;

const NORMAL_DIAMETER_FRACTION = 0.68;
const TOMORROW_RADIUS_RATIO = 0.8;
const MIN_POINT_RADIUS = 2.2;
const MAX_POINT_RADIUS = 6;
const STROKE_SHARE = 0.36;
const TOMORROW_STROKE_SHARE = 0.29;

export interface ChartMarkGeometry {
  spacing: number;
  normalRadius: number;
  tomorrowRadius: number;
  strokeHalf: number;
  tomorrowStrokeHalf: number;
}

export function preferredMarkRadius(scale: ChartMetrics, tomorrow: boolean): number {
  return tomorrow
    ? Math.max(3.2 * scale.scale, scale.height * 0.0052)
    : Math.max(4 * scale.scale, scale.height * 0.0062);
}

export function nearestCentreSpacing(xs: readonly number[], fallback: number): number {
  const sorted = Array.from(new Set(xs)).sort((left, right) => left - right);
  let smallest = Number.POSITIVE_INFINITY;
  for (let index = 1; index < sorted.length; index += 1) {
    const gap = (sorted[index] as number) - (sorted[index - 1] as number);
    if (gap > 0) {
      smallest = Math.min(smallest, gap);
    }
  }
  return Number.isFinite(smallest) ? smallest : fallback;
}

/**
 * Every size a mark is drawn with.
 *
 * The ordinary point is capped at 0.68 of the centre spacing, which keeps 96 quarter-hours a series
 * rather than a smear; current and selected marks use that same radius (an enlarged current marker
 * covers its neighbours). Tomorrow has its own quieter, slightly smaller point; hourly segments keep one
 * thickness, drawn at the marks' own duration.
 */
export function markGeometry(
  xs: readonly number[],
  scale: ChartMetrics,
  hourly: boolean,
  spanMs: number = DAY_MS,
): ChartMarkGeometry {
  const panel = Math.max(1, scale.right - scale.left);
  const spacing = nearestCentreSpacing(xs, panel * (15 * 60_000 / spanMs));
  const preferred = Math.min(
    Math.max(preferredMarkRadius(scale, false), MIN_POINT_RADIUS),
    MAX_POINT_RADIUS,
  );
  // The density's mark scale (smaller for `chart_detail`) applies after the spacing cap, so it always shows.
  const markScale = scale.density.markScale;
  const normalRadius = Math.min(preferred, (spacing * NORMAL_DIAMETER_FRACTION) / 2) * markScale;
  const tomorrowPreferred = Math.min(
    Math.max(preferredMarkRadius(scale, true), MIN_POINT_RADIUS),
    MAX_POINT_RADIUS,
  );
  const tomorrowRadius = Math.min(tomorrowPreferred * markScale, normalRadius * TOMORROW_RADIUS_RATIO);
  const hourHalf = hourly ? panel * (HOUR_MS / spanMs) : 0;
  return {
    spacing,
    normalRadius,
    tomorrowRadius,
    strokeHalf: hourHalf * STROKE_SHARE * markScale,
    tomorrowStrokeHalf: hourHalf * TOMORROW_STROKE_SHARE * markScale,
  };
}

export interface ChartHitTarget {
  mark: ChartMark;
  x: number;
  y: number;
  radius: number;
  /** Whether this target is the current interval's mark. A label, not a size; the now line is drawn at this target's `x`. */
  current: boolean;
  halfLength: number;
  halfThickness: number;
}

/**
 * The drawn targets: every x from the one wall-clock mapping, every size from `markGeometry`.
 *
 * Both days' marks are included, even pairs sharing an x, because a target is a record. Hit-testing uses
 * these targets and the now line sits at the current target's own x (also the x the readout reports).
 */
export function hitTargets(
  series: ChartSeries,
  scale: ChartScale,
  current: ChartMark | null,
): ChartHitTarget[] {
  const xs = series.marks.map((mark) => xForWallClock(scale, mark.position));
  const spanMs = series.time.spanMs;
  const geometry = markGeometry(xs, scale, series.hourly, spanMs);
  const panelWidth = Math.max(0, scale.right - scale.left);
  return series.marks.map((mark, index) => {
    const isCurrent = current !== null && mark.startMs === current.startMs;
    // An hourly segment's half length is its duration against the time the axis spans.
    const halfLength = mark.hourly ? ((mark.durationMinutes * 60_000) / 2 / spanMs) * panelWidth : 0;
    return {
      mark,
      x: xs[index] as number,
      y: yAt(scale, mark.price),
      radius: mark.hourly ? 0 : mark.tomorrow ? geometry.tomorrowRadius : geometry.normalRadius,
      current: isCurrent,
      halfLength,
      halfThickness: mark.tomorrow ? geometry.tomorrowStrokeHalf : geometry.strokeHalf,
    };
  });
}

export function distanceToInk(target: ChartHitTarget, point: ChartPoint): number {
  const dx = Math.abs(point.x - target.x);
  const dy = Math.abs(point.y - target.y);
  let distance =
    target.radius > 0
      ? Math.max(0, Math.hypot(dx, dy) - target.radius)
      : Number.POSITIVE_INFINITY;
  if (target.mark.hourly) {
    distance = Math.min(
      distance,
      Math.hypot(Math.max(0, dx - target.halfLength), Math.max(0, dy - target.halfThickness)),
    );
  }
  return distance;
}

function chooseMark(
  targets: readonly ChartHitTarget[],
  point: ChartPoint,
  limit: number,
): ChartMark | null {
  let best: ChartHitTarget | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const target of targets) {
    const distance = distanceToInk(target, point);
    if (distance > limit) {
      continue;
    }
    const better =
      distance < bestDistance ||
      (distance === bestDistance && best !== null && target.mark.startMs < best.mark.startMs);
    if (better) {
      best = target;
      bestDistance = distance;
    }
  }
  return best?.mark ?? null;
}

export function nearestMark(targets: readonly ChartHitTarget[], point: ChartPoint): ChartMark | null {
  return chooseMark(targets, point, Number.POSITIVE_INFINITY);
}

export function hitTestMarks(
  targets: readonly ChartHitTarget[],
  point: ChartPoint,
  slop: number = HIT_SLOP,
): ChartMark | null {
  return chooseMark(targets, point, slop);
}

export function walkMarks(
  marks: readonly ChartMark[],
  current: ChartMark | null,
  step: number,
): ChartMark | null {
  if (marks.length === 0) {
    return null;
  }
  const edge = (): ChartMark =>
    step >= 0 ? (marks[0] as ChartMark) : (marks[marks.length - 1] as ChartMark);
  if (current === null) {
    return edge();
  }
  const index = marks.findIndex((mark) => mark.startMs === current.startMs);
  if (index < 0) {
    return edge();
  }
  return marks[Math.min(Math.max(index + step, 0), marks.length - 1)] as ChartMark;
}

export interface PlanBand {
  /** The date of the run's first drawn interval. On the range axis a run may go on into the next date. */
  dayKey: string;
  dayIndex: number;
  /** The drawn span: on the range axis clipped to the axis, so a run under way starts at the axis's left edge. */
  startMs: number;
  endMs: number;
  /**
   * Positions from the series' time mapping, same function as the marks. On the wall-clock axis an edge
   * on local midnight is 1 (24:00), the day's own end.
   */
  startPosition: number;
  endPosition: number;
  /**
   * Drawn hatched rather than solid: on the wall-clock overlay, the run is not on the market-local day the
   * now fact is in, the same rule as a mark's `tomorrow`. On the shared axis tomorrow's 00:00-05:30 sits
   * left of the now line, and without this it reads as a plan already over. Never set on the range axis,
   * where every run sits at its own time.
   */
  tomorrow: boolean;
}

export interface PlanBands {
  installed: PlanBand[];
  proposal: PlanBand[];
}

/**
 * The runs of flagged rows. On the wall-clock overlay a run never crosses a local day; on the range it
 * goes on across midnight while the rows are contiguous, skips what is over before the axis starts and is
 * clipped to the axis.
 */
function runsOf(
  rows: readonly PriceInterval[],
  days: readonly ChartDay[],
  time: TimeAxis,
  flag: (row: PriceInterval) => boolean,
): PlanBand[] {
  const indexOfDay = new Map(days.map((day, index) => [day.key, index]));
  const lower = time.startMs ?? Number.NEGATIVE_INFINITY;
  const upper = time.endMs ?? Number.POSITIVE_INFINITY;
  const bands: PlanBand[] = [];
  let open: PlanBand | null = null;
  for (const row of rows.slice().sort((left, right) => left.startMs - right.startMs)) {
    const dayIndex = indexOfDay.get(row.day);
    if (!flag(row) || dayIndex === undefined || !time.shows(row.startMs, row.endMs)) {
      open = null;
      continue;
    }
    const day = days[dayIndex] as ChartDay;
    const startMs = Math.max(row.startMs, lower);
    const endMs = Math.min(row.endMs, upper);
    if (open !== null && (!time.overlay || open.dayIndex === dayIndex) && open.endMs === startMs) {
      open.endMs = endMs;
      open.endPosition = time.position(endMs, "end");
      continue;
    }
    open = {
      dayKey: row.day,
      dayIndex,
      startMs,
      endMs,
      startPosition: time.position(startMs, "start"),
      endPosition: time.position(endMs, "end"),
      tomorrow: time.overlay && day.role !== "today",
    };
    bands.push(open);
  }
  return bands;
}

/**
 * A proposal queued beside the installed plan until the charging window under way ends (Home
 * Assistant's `proposal_pending`). `running` is that window, the installed period that holds now, or
 * `null` when none does.
 */
export interface PendingProposal {
  running: { startMs: number; endMs: number } | null;
}

/**
 * The shaded spans, from the backend's half-open flags.
 *
 * Runs are positioned by the series' time mapping like the marks (`time`; the wall-clock overlay when
 * omitted), while `dayKey`/`dayIndex` keep the real date. `proposal` carries only what the installed schedule does not
 * already cover, so an applied proposal is not darkened twice.
 *
 * With a proposal pending, the installed plan is shown only for the window under way, and everything
 * after that window's end comes from the proposal alone: the installed plan's later windows are the
 * ones the proposal replaces, so they are not drawn.
 */
export function plannedBands(
  rows: readonly PriceInterval[],
  days: readonly ChartDay[],
  timeZone: string,
  pending: PendingProposal | null = null,
  time: TimeAxis = wallClockAxis(zoneClock(timeZone)),
): PlanBands {
  if (pending === null) {
    return {
      installed: runsOf(rows, days, time, (row) => row.installed_planned),
      proposal: runsOf(rows, days, time, (row) => row.proposal_planned && !row.installed_planned),
    };
  }
  const running = pending.running;
  const inRunning = (row: PriceInterval): boolean =>
    running !== null && row.installed_planned && row.startMs < running.endMs && running.startMs < row.endMs;
  const afterRunning = running === null ? Number.NEGATIVE_INFINITY : running.endMs;
  return {
    installed: runsOf(rows, days, time, inRunning),
    proposal: runsOf(rows, days, time, (row) => row.proposal_planned && row.startMs >= afterRunning),
  };
}
