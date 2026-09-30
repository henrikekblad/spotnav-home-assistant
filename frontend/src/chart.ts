// The chart's time model and geometry, as pure arithmetic.
//
// The rules match the other SpotNav clients; the cross-client fixture in
// `frontend/test/visual-fixtures.ts` names the values they agree on.
//
// Time model: one wall-clock axis, today over tomorrow.
//   * The plot has one x axis: the local wall clock of one day, 00:00 to 24:00 across the full width.
//     Today and tomorrow are overlaid on it.
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
// Every coordinate goes through `xForWallClock(scale, position)`: marks, hit targets, plan bands, ticks,
// the now line, the selection line and the pointer's inverse mapping.

import type { PriceInterval } from "./validate";

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
}

export interface ChartScale extends ChartMetrics {
  minValue: number;
  maxValue: number;
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

/** Metrics for the plan-card profile: denser text, no footer. */
export function chartMetrics(width: number, height: number): ChartMetrics {
  const w = Math.max(180, width);
  const h = Math.max(100, height);
  const scale = Math.min(Math.max(Math.min(w / 420, h / 220), MIN_SCALE), MAX_SCALE);
  const pad = 14 * scale;
  const headerTextSize = Math.min(HEADER_TEXT, h * HEADER_CAP);
  const axisTextSize = Math.min(AXIS_TEXT, h * AXIS_CAP);
  return {
    width: w,
    height: h,
    scale,
    pad,
    headerTextSize,
    axisTextSize,
    top: pad * 0.9,
    bottom: h - pad - axisTextSize * 1.35,
    left: pad + Math.max(axisTextSize * 2.5, w * 0.07),
    right: w - pad,
  };
}

export function chartScale(width: number, height: number, values: readonly number[]): ChartScale {
  const metrics = chartMetrics(width, height);
  const finite = values.filter((value) => Number.isFinite(value));
  return {
    ...metrics,
    minValue: Math.min(0, ...(finite.length > 0 ? finite : [0])),
    maxValue: Math.max(1, ...(finite.length > 0 ? finite : [1])),
  };
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

export function yAt(scale: ChartScale, value: number): number {
  const span = scale.maxValue - scale.minValue;
  const share = span <= 0 ? 0 : (value - scale.minValue) / span;
  return scale.bottom - share * (scale.bottom - scale.top);
}

export function gutterLabels(scale: ChartScale): Array<{ value: number; y: number; label: string }> {
  const candidates =
    scale.maxValue - scale.minValue < 1e-9
      ? [scale.minValue]
      : [scale.minValue, (scale.minValue + scale.maxValue) / 2, scale.maxValue];
  const seen = new Set<string>();
  const labels: Array<{ value: number; y: number; label: string }> = [];
  for (const value of candidates) {
    const label = String(Math.round(value));
    if (seen.has(label)) {
      continue;
    }
    seen.add(label);
    labels.push({ value, y: yAt(scale, value), label });
  }
  return labels;
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
}

export interface ChartSeries {
  marks: ChartMark[];
  days: ChartDay[];
  /** The one tick sequence both days share: five wall-clock positions. */
  axis: AxisTick[];
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

  const marks: ChartMark[] = [];
  for (const [dayIndex, day] of days.entries()) {
    const bucket = byDay.get(day.key) as PriceInterval[];
    for (const row of bucket) {
      const price = row.effective_price;
      if (price === null || !Number.isFinite(price)) {
        continue; // Missing, never zero.
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
        tomorrow: day.role !== "today",
        hourly,
        proposalPlanned: row.proposal_planned,
        installedPlanned: row.installed_planned,
        position: wallClockPosition(clock, drawnMs),
        wallClock: wallClockOf(drawnMs, clock),
        offset: clock.offsetLabel(drawnMs),
      });
    }
  }

  const prices = marks.map((mark) => mark.price);
  return {
    marks,
    days,
    axis: axisTicks(days, clock),
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
): ChartMarkGeometry {
  const panel = Math.max(1, scale.right - scale.left);
  const spacing = nearestCentreSpacing(xs, panel / (MINUTES_PER_DAY / 15));
  const preferred = Math.min(
    Math.max(preferredMarkRadius(scale, false), MIN_POINT_RADIUS),
    MAX_POINT_RADIUS,
  );
  const normalRadius = Math.min(preferred, (spacing * NORMAL_DIAMETER_FRACTION) / 2);
  const tomorrowPreferred = Math.min(
    Math.max(preferredMarkRadius(scale, true), MIN_POINT_RADIUS),
    MAX_POINT_RADIUS,
  );
  const tomorrowRadius = Math.min(tomorrowPreferred, normalRadius * TOMORROW_RADIUS_RATIO);
  const hourHalf = hourly ? panel / 24 : 0;
  return {
    spacing,
    normalRadius,
    tomorrowRadius,
    strokeHalf: hourHalf * STROKE_SHARE,
    tomorrowStrokeHalf: hourHalf * TOMORROW_STROKE_SHARE,
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
  const geometry = markGeometry(xs, scale, series.hourly);
  const panelWidth = Math.max(0, scale.right - scale.left);
  return series.marks.map((mark, index) => {
    const isCurrent = current !== null && mark.startMs === current.startMs;
    // An hourly segment's half length is its duration against the shared 24-hour axis.
    const halfLength = mark.hourly ? ((mark.durationMinutes * 60_000) / 2 / DAY_MS) * panelWidth : 0;
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
  dayKey: string;
  dayIndex: number;
  startMs: number;
  endMs: number;
  /** Wall-clock fractions of one day, same x function as the marks. An edge on local midnight is 1 (24:00), the day's own end. */
  startPosition: number;
  endPosition: number;
}

export interface PlanBands {
  installed: PlanBand[];
  proposal: PlanBand[];
}

/**
 * The wall-clock fraction of one band edge. An edge on local midnight is the end of the day it belongs
 * to (24:00) and is drawn at the right edge, so a run ending at day's end does not wrap to 00:00. Marks
 * never face this case, as no interval starts at 24:00.
 */
function bandPosition(instantMs: number, day: ChartDay, clock: ZoneClock): number {
  const position = wallClockPosition(clock, instantMs);
  return instantMs > day.startMs && position === 0 ? 1 : position;
}

function runsOf(
  rows: readonly PriceInterval[],
  days: readonly ChartDay[],
  clock: ZoneClock,
  flag: (row: PriceInterval) => boolean,
): PlanBand[] {
  const indexOfDay = new Map(days.map((day, index) => [day.key, index]));
  const bands: PlanBand[] = [];
  let open: PlanBand | null = null;
  for (const row of rows.slice().sort((left, right) => left.startMs - right.startMs)) {
    const dayIndex = indexOfDay.get(row.day);
    if (!flag(row) || dayIndex === undefined) {
      open = null;
      continue;
    }
    const day = days[dayIndex] as ChartDay;
    if (open !== null && open.dayIndex === dayIndex && open.endMs === row.startMs) {
      open.endMs = row.endMs;
      open.endPosition = bandPosition(row.endMs, day, clock);
      continue;
    }
    open = {
      dayKey: row.day,
      dayIndex,
      startMs: row.startMs,
      endMs: row.endMs,
      startPosition: bandPosition(row.startMs, day, clock),
      endPosition: bandPosition(row.endMs, day, clock),
    };
    bands.push(open);
  }
  return bands;
}

/**
 * The shaded spans, from the backend's half-open flags.
 *
 * A run never crosses a local day and is positioned on the shared wall-clock axis like the marks, while
 * `dayKey`/`dayIndex` keep its real date. `proposal` carries only what the installed schedule does not
 * already cover, so an applied proposal is not darkened twice.
 */
export function plannedBands(
  rows: readonly PriceInterval[],
  days: readonly ChartDay[],
  timeZone: string,
): PlanBands {
  const clock = zoneClock(timeZone);
  return {
    installed: runsOf(rows, days, clock, (row) => row.installed_planned),
    proposal: runsOf(rows, days, clock, (row) => row.proposal_planned && !row.installed_planned),
  };
}
